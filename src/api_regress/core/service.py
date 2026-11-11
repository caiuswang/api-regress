"""对外的编排入口。cli 和以后的界面都只调用这里。"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from api_regress import __version__
from api_regress.core import expr, rundir
from api_regress.core.config import (
    ConfigError,
    ConfigIssue,
    LoadedSuite,
    MissingVar,
    Suite,
    UnverifiedSuite,
    base_url_problem,
    load_suite,
)
from api_regress.core.flow import Plan, evaluate
from api_regress.core.models import SIDES, WarningItem
from api_regress.core.result import (
    ExitState,
    FailOn,
    RequestResult,
    RunResult,
    Summary,
    UnmatchedRule,
    exit_code,
    run_exit_state,
)
from api_regress.core.rules import CompiledRule, RuleSet, static_warnings, unmatched_rules
from api_regress.core.runner import Call, Runner, make_call, new_client
from api_regress.core.samples import Sample, fingerprint
from api_regress.core.status import RequestStatus

log = logging.getLogger(__name__)

_SIDE_LABEL = {"baseline": "baseline", "candidate": "candidate", "shared": "baseline 和 candidate"}


class ValidationReport(BaseModel):
    source: str
    ok: bool
    request_count: int = 0
    errors: list[ConfigIssue] = []
    warnings: list[WarningItem] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 2


def _missing_var_warnings(missing_vars: list[MissingVar]) -> list[WarningItem]:
    order = {"baseline": 0, "candidate": 1, "shared": 2}
    return [
        WarningItem(
            code="env_var_missing",
            message=f"{_SIDE_LABEL[m.side]} 缺少环境变量 {m.name}",
            location=m.location,
            line=m.line,
        )
        for m in sorted(missing_vars, key=lambda m: order[m.side])
    ]


def _unverified_warnings(exc: UnverifiedSuite) -> list[WarningItem]:
    fields = [
        WarningItem(code="env_var_missing", message=f.message, location=f.location, line=f.line) for f in exc.fields
    ]
    note = WarningItem(code="env_var_missing", message="有字段的值来自缺失的环境变量，本次跳过了规则检查")
    return [*_missing_var_warnings(exc.missing), *fields, note]


def _report(source: str, request_count: int, warnings: list[WarningItem]) -> ValidationReport:
    for w in warnings:
        log.warning("[%s] %s", w.code, w.message)
    return ValidationReport(source=source, ok=True, request_count=request_count, warnings=warnings)


def validate_suite_sync(path: str | Path, env: Mapping[str, str] | None = None) -> ValidationReport:
    """静态检查 suite：语法、字段、路径；缺少的环境变量只作为 warning。"""
    source = str(path)
    try:
        loaded = load_suite(path, sides=(), env=env)
    except ConfigError as exc:
        log.info("suite 校验失败：%s，%d 个错误", source, len(exc.issues))
        return ValidationReport(source=source, ok=False, errors=exc.issues)
    except UnverifiedSuite as exc:
        return _report(source, exc.request_count, _unverified_warnings(exc))
    warnings = _missing_var_warnings(loaded.missing_vars) + static_warnings(loaded.suite, loaded.line_of)
    return _report(source, len(loaded.suite.requests), warnings)


async def validate_suite(path: str | Path, env: Mapping[str, str] | None = None) -> ValidationReport:
    return await asyncio.to_thread(validate_suite_sync, path, env)


# ---------- run ----------


class RunOptions(BaseModel):
    out: Path | None = None
    fail_on: FailOn = "breaking"
    baseline_url: str | None = None
    candidate_url: str | None = None
    only: list[str] = []
    log_file: Path | None = None
    """日志文件。放在 run 目录里时不算作无关文件。"""


class RequestStarted(BaseModel):
    event: Literal["request_started"] = "request_started"
    name: str


class RequestDone(BaseModel):
    event: Literal["request_done"] = "request_done"
    name: str
    status: RequestStatus
    findings: int


ProgressEvent = RequestStarted | RequestDone
OnEvent = Callable[[ProgressEvent], None]


class CancelToken(Protocol):
    """threading.Event 或 asyncio.Event 都可以。已经开始的请求会跑完，还没开始的记为 error。"""

    def is_set(self) -> bool: ...


class RunReport(BaseModel):
    """一次 run 的返回值。suite 有问题时 result 为 None，errors 列出问题。"""

    source: str
    exit_code: int
    exit_reason: str
    errors: list[ConfigIssue] = []
    result: RunResult | None = None
    out_dir: str | None = None


def _usage_issue(source: str, location: str, message: str) -> ConfigIssue:
    return ConfigIssue(source=source, location=location, message=message)


def _check_options(source: str, suite_names: list[str], options: RunOptions) -> list[ConfigIssue]:
    issues = [
        _usage_issue(source, "--only", f"suite 里没有名为 {name!r} 的请求")
        for name in options.only
        if name not in suite_names
    ]
    for flag, url in (("--baseline-url", options.baseline_url), ("--candidate-url", options.candidate_url)):
        problem = None if url is None else base_url_problem(url)
        if problem:
            issues.append(_usage_issue(source, flag, problem))
    return issues


def _compile_optional(source: str | None) -> expr.CompiledExpr | None:
    return None if source is None else expr.compile_expr(source, "success")


def _plans(loaded: LoadedSuite, only: list[str]) -> list[Plan]:
    suite = loaded.suite
    return [
        Plan(
            request=req,
            fingerprint=fingerprint(loaded.raw, i),
            rules=RuleSet.for_request(suite, req, i),
            compare=suite.defaults.compare,
            success=_compile_optional(suite.success_of(req)),
            asserts=[(a, expr.compile_expr(a.expr, "assert")) for a in req.asserts],
        )
        for i, req in enumerate(suite.requests)
        if not only or req.name in only
    ]


@dataclass
class _Executed:
    """一条请求跑完的结果。missed 是本条请求里没命中的规则，None 表示不参与统计（没有 JSON body）。"""

    plan: Plan
    result: RequestResult
    base_samples: list[Sample]
    cand: Sample | None = None
    missed: list[CompiledRule] | None = None
    cancelled: bool = False
    internal_error: bool = False


class _Executor:
    def __init__(self, suite: Suite, options: RunOptions, on_event: OnEvent | None, cancel: CancelToken | None):
        self.suite = suite
        self.options = options
        self.on_event = on_event or (lambda event: None)
        self.cancel = cancel
        self.callback_errors = 0

    async def run_all(self, plans: list[Plan], transport: httpx.AsyncBaseTransport | None) -> list[_Executed]:
        defaults = self.suite.defaults
        # 同时在跑的请求数受 gate 限制：拿到名额后才检查取消、发 RequestStarted，取消才能对排队的请求生效
        gate = asyncio.Semaphore(defaults.concurrency)
        async with new_client(defaults.timeout, transport) as client:
            runner = Runner(client, defaults.concurrency, defaults.retries, defaults.timeout)
            return list(await asyncio.gather(*(self._one(gate, runner, plan) for plan in plans)))

    def _emit(self, event: ProgressEvent) -> None:
        """回调出错不能中断运行；记为工具自身出错（退出码 3）。"""
        try:
            self.on_event(event)
        except Exception as exc:
            self.callback_errors += 1
            log.error("进度回调出错：%s\n%s", type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))

    async def _baseline(self, runner: Runner, call: Call) -> list[Sample]:
        """基线串行请求 2 次；第 1 次失败就不再请求第 2 次。"""
        first = await runner.fetch(call)
        return [first] if first.failed else [first, await runner.fetch(call)]

    async def _one(self, gate: asyncio.Semaphore, runner: Runner, plan: Plan) -> _Executed:
        async with gate:
            if self.cancel is not None and self.cancel.is_set():
                return _cancelled(plan)
            self._emit(RequestStarted(name=plan.name))
            try:
                item = await self._execute(runner, plan)
            except Exception as exc:  # 单条请求出错不影响其他请求
                item = _crashed(plan, exc)
            self._emit(RequestDone(name=plan.name, status=item.result.status, findings=len(item.result.findings)))
            return item

    async def _execute(self, runner: Runner, plan: Plan) -> _Executed:
        base_call = make_call(self.suite, plan.request, "baseline", self.options.baseline_url)
        cand_call = make_call(self.suite, plan.request, "candidate", self.options.candidate_url)
        base_samples, cand = await asyncio.gather(self._baseline(runner, base_call), runner.fetch(cand_call))
        evaluation = evaluate(plan, base_samples, cand)
        missed = unmatched_rules(plan.rules.rules, evaluation.bodies) if evaluation.bodies else None
        return _Executed(plan, evaluation.result, base_samples, cand, missed)


def _error_result(plan: Plan, reason: str) -> RequestResult:
    return RequestResult(
        name=plan.name, method=plan.request.method, status="error", reason=reason, fingerprint=plan.fingerprint
    )


def _cancelled(plan: Plan) -> _Executed:
    log.info("%s：error（运行已取消）", plan.name)
    return _Executed(plan, _error_result(plan, "运行已取消，没有发出请求"), [], cancelled=True)


def _crashed(plan: Plan, exc: Exception) -> _Executed:
    """只记异常类型和调用栈，不记异常消息：消息里可能带着请求的内容。"""
    name = type(exc).__name__
    log.error("%s：内部错误 %s\n%s", plan.name, name, "".join(traceback.format_tb(exc.__traceback__)))
    return _Executed(plan, _error_result(plan, f"工具内部错误（{name}），详见日志"), [], internal_error=True)


def _unmatched(suite: Suite, executed: list[_Executed]) -> list[UnmatchedRule]:
    """defaults 的规则在所有请求里都没命中才算；只统计至少有一侧是 JSON body 的请求。"""
    defaults_missed: set[int] | None = None
    out: list[UnmatchedRule] = []
    for item in executed:
        if item.missed is None:
            continue
        missed_defaults = {r.ref.index for r in item.missed if r.ref.scope == "defaults"}
        defaults_missed = missed_defaults if defaults_missed is None else defaults_missed & missed_defaults
        out += [
            UnmatchedRule(scope="request", index=r.ref.index, request=item.plan.name, path=r.rule.path)
            for r in item.missed
            if r.ref.scope == "request"
        ]
    head = [
        UnmatchedRule(scope="defaults", index=i, path=suite.defaults.rules[i].path) for i in sorted(defaults_missed or ())
    ]
    return head + out


def _write_run_dir(out: Path, executed: list[_Executed], result: RunResult, ignore: list[Path]) -> None:
    rundir.prepare(out, ignore)
    for item in executed:
        name, fp, keep = item.plan.name, item.plan.fingerprint, item.plan.request.compare_headers
        if item.base_samples:
            log.info("写入 %s", rundir.write_side(out, "baseline", name, fp, item.base_samples, keep))
        if item.cand is not None:
            log.info("写入 %s", rundir.write_side(out, "candidate", name, fp, [item.cand], keep))
    rundir.write_result(out, result)


def _log_summary(result: RunResult) -> None:
    s = result.summary
    log.info(
        "汇总：请求 %d | ok %d | error %d | inconclusive %d | unsupported %d | breaking %d | suspicious %d | info %d | warning %d",
        s.requests, s.ok, s.error, s.inconclusive, s.unsupported, s.breaking, s.suspicious, s.info, s.warnings,
    )  # fmt: skip


def _finish(
    source: str, options: RunOptions, out: Path, executed: list[_Executed], started: datetime, suite: Suite,
    callback_errors: int,
) -> RunReport:  # fmt: skip
    requests = [item.result for item in executed]
    internal = callback_errors > 0 or any(item.internal_error for item in executed)
    code, reason = exit_code(replace(run_exit_state(requests, options.fail_on), internal_error=internal))
    result = RunResult(
        tool_version=__version__, command="run", suite=source, started_at=started, finished_at=_now(),
        fail_on=options.fail_on, cancelled=any(item.cancelled for item in executed), exit_code=code,
        exit_reason=reason, summary=Summary.of(requests), requests=requests,
        unmatched_rules=_unmatched(suite, executed),
    )  # fmt: skip
    _log_summary(result)
    try:
        _write_run_dir(out, executed, result, _ignored(options))
    except OSError as exc:
        log.error("无法写入 run 目录 %s：%s", out, exc)
        report = _internal_error(source, f"无法写入 run 目录 {out}（{exc}）")
        report.result = result.model_copy(update={"exit_code": report.exit_code, "exit_reason": report.exit_reason})
        return report
    log.info("run 结束：退出码 %d（%s）", code, reason)
    return RunReport(source=source, exit_code=code, exit_reason=reason, result=result, out_dir=str(out))


def _now() -> datetime:
    return datetime.now().astimezone()


def _ignored(options: RunOptions) -> list[Path]:
    return [options.log_file] if options.log_file else []


def _internal_error(source: str, detail: str) -> RunReport:
    code, reason = exit_code(ExitState(internal_error=True))
    reason = f"{reason}：{detail}"
    log.info("run 结束：退出码 %d（%s）", code, reason)
    return RunReport(source=source, exit_code=code, exit_reason=reason)


def _suite_error(source: str, issues: list[ConfigIssue]) -> RunReport:
    code, reason = exit_code(ExitState(suite_error=True))
    for issue in issues:
        log.error("%s", issue)
    log.info("run 结束：退出码 %d（%s，%d 个错误）", code, reason, len(issues))
    return RunReport(source=source, exit_code=code, exit_reason=reason, errors=issues)


async def run(
    path: str | Path,
    options: RunOptions | None = None,
    *,
    env: Mapping[str, str] | None = None,
    on_event: OnEvent | None = None,
    cancel_event: CancelToken | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunReport:
    """两侧都实时请求：基线串行 2 次、候选 1 次，然后逐条对比，写 run 目录和 result.json。"""
    options = options or RunOptions()
    source = str(path)
    started = _now()
    try:
        loaded = await asyncio.to_thread(load_suite, path, SIDES, env)
    except ConfigError as exc:
        return _suite_error(source, exc.issues)
    issues = _check_options(source, [r.name for r in loaded.suite.requests], options)
    if issues:
        return _suite_error(source, issues)
    out = options.out or rundir.default_out_dir(started)
    try:
        rundir.check(out, _ignored(options))
    except OSError as exc:
        log.error("%s", exc)
        return _internal_error(source, str(exc))
    plans = _plans(loaded, options.only)
    log.info("run 开始：%d 条请求，run 目录 %s", len(plans), out)
    executor = _Executor(loaded.suite, options, on_event, cancel_event)
    executed = await executor.run_all(plans, transport)
    return _finish(source, options, out, executed, started, loaded.suite, executor.callback_errors)


def run_sync(path: str | Path, options: RunOptions | None = None, **kwargs: Any) -> RunReport:
    return asyncio.run(run(path, options, **kwargs))
