"""对外的编排入口。cli 和以后的界面都只调用这里。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from api_regress import __version__
from api_regress.core import accept as acceptance, expr, rundir, snapshot
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
from api_regress.core.executor import (
    CancelToken,
    Executed,
    Executor,
    Mode,
    OnEvent,
    ProgressEvent,
    RequestDone,
    RequestStarted,
    Targets,
)
from api_regress.core.flow import Plan
from api_regress.core.models import SIDES, Side, WarningItem
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
from api_regress.core.rules import RuleSet, static_warnings
from api_regress.core.samples import SampleFormatError, fingerprint

__all__ = [
    "AcceptOptions", "AcceptReport", "CancelToken", "CompareOptions", "OnEvent", "ProgressEvent", "RecordOptions",
    "RecordReport", "Rejected", "RequestDone", "RequestStarted", "RunOptions", "RunReport", "ValidationReport",
    "accept", "accept_sync", "compare", "compare_sync", "record", "record_sync", "run", "run_sync",
    "validate_suite", "validate_suite_sync",
]  # fmt: skip

log = logging.getLogger(__name__)

T = TypeVar("T")

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


# ---------- 共用 ----------


class CommandReport(BaseModel):
    """各命令返回值的公共部分。suite 或参数有问题时 errors 列出问题。"""

    source: str
    exit_code: int
    exit_reason: str
    errors: list[ConfigIssue] = []


class _Stop(Exception):
    """提前结束一个命令：带着已经算好的返回值。"""

    def __init__(self, report: CommandReport):
        self.report = report


def _now() -> datetime:
    return datetime.now().astimezone()


def _usage_issue(source: str, location: str, message: str) -> ConfigIssue:
    return ConfigIssue(source=source, location=location, message=message)


def _finish_log(command: str, code: int, reason: str) -> None:
    log.info("%s 结束：退出码 %d（%s）", command, code, reason)


def _suite_error(command: str, source: str, issues: list[ConfigIssue], report: type[CommandReport]) -> _Stop:
    code, reason = exit_code(ExitState(suite_error=True))
    for issue in issues:
        log.error("%s", issue)
    _finish_log(command, code, f"{reason}，{len(issues)} 个错误")
    return _Stop(report(source=source, exit_code=code, exit_reason=reason, errors=issues))


def _version_error(command: str, source: str, exc: Exception, report: type[CommandReport]) -> _Stop:
    return _suite_error(command, source, [_usage_issue(source, "--dir", str(exc))], report)


def _internal_error(command: str, source: str, detail: str, report: type[CommandReport]) -> _Stop:
    code, reason = exit_code(ExitState(internal_error=True))
    reason = f"{reason}：{detail}"
    log.error("%s", detail)
    _finish_log(command, code, reason)
    return _Stop(report(source=source, exit_code=code, exit_reason=reason))


async def _load(command: str, path: str | Path, sides: Collection[Side], env: Mapping[str, str] | None,
                report: type[CommandReport]) -> LoadedSuite:  # fmt: skip
    try:
        return await asyncio.to_thread(load_suite, path, sides, env)
    except ConfigError as exc:
        raise _suite_error(command, str(path), exc.issues, report) from None


def _check_usage(command: str, source: str, names: list[str], only: list[str], overrides: dict[str, str | None],
                 report: type[CommandReport]) -> None:  # fmt: skip
    """--only 的 name 必须存在；覆盖地址必须合法。"""
    issues = [_usage_issue(source, "--only", f"没有名为 {name!r} 的请求") for name in only if name not in names]
    for flag, url in overrides.items():
        problem = None if url is None else base_url_problem(url)
        if problem:
            issues.append(_usage_issue(source, flag, problem))
    if issues:
        raise _suite_error(command, source, issues, report)


def _snapshot_step(command: str, source: str, report: type[CommandReport], step: Callable[[], T]) -> T:
    """执行一步快照操作，把异常换成退出码：版本不兼容 2，其他（损坏、不能用、IO 错误）3。"""
    try:
        return step()
    except snapshot.SnapshotVersionError as exc:
        raise _version_error(command, source, exc, report) from None
    except OSError as exc:
        raise _internal_error(command, source, str(exc), report) from None


def _open_snapshot(command: str, source: str, root: Path, clean: bool, report: type[CommandReport]
                   ) -> tuple[Path, snapshot.Manifest | None]:  # fmt: skip
    """record / accept：处理残留，检查快照能不能写；返回规范化的路径和现有 manifest。"""

    def step() -> tuple[Path, snapshot.Manifest | None]:
        normalized = snapshot.recover(root)
        return normalized, snapshot.check_writable(normalized, clean)

    return _snapshot_step(command, source, report, step)


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


def _internal(executor: Executor, executed: list[Executed]) -> bool:
    return executor.callback_errors > 0 or any(item.internal_error for item in executed)


# ---------- run / compare ----------


class RunOptions(BaseModel):
    out: Path | None = None
    fail_on: FailOn = "breaking"
    baseline_url: str | None = None
    candidate_url: str | None = None
    only: list[str] = []
    log_file: Path | None = None
    """日志文件。放在 run 目录里时不算作无关文件。"""


class CompareOptions(RunOptions):
    """compare：基线读快照，baseline_url 不生效。"""

    snapshot: Path


class RunReport(CommandReport):
    """run / compare 的返回值。suite 有问题时 result 为 None。"""

    result: RunResult | None = None
    out_dir: str | None = None


def _unmatched(suite: Suite, executed: list[Executed]) -> list[UnmatchedRule]:
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


def _write_run_dir(out: Path, executed: list[Executed], result: RunResult, ignore: list[Path]) -> None:
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


def _ignored(options: RunOptions) -> list[Path]:
    return [options.log_file] if options.log_file else []


def _finish(command: Mode, source: str, options: RunOptions, out: Path, executed: list[Executed], started: datetime,
            suite: Suite, internal: bool) -> RunReport:  # fmt: skip
    requests = [item.result for item in executed]
    code, reason = exit_code(replace(run_exit_state(requests, options.fail_on), internal_error=internal))
    result = RunResult(
        tool_version=__version__, command=command, suite=source, started_at=started, finished_at=_now(),
        fail_on=options.fail_on, cancelled=any(item.cancelled for item in executed), exit_code=code,
        exit_reason=reason, summary=Summary.of(requests), requests=requests,
        unmatched_rules=_unmatched(suite, executed),
    )  # fmt: skip
    _log_summary(result)
    try:
        _write_run_dir(out, executed, result, _ignored(options))
    except OSError as exc:
        report = _internal_error(command, source, f"无法写入 run 目录 {out}（{exc}）", RunReport).report
        assert isinstance(report, RunReport)
        report.result = result.model_copy(update={"exit_code": report.exit_code, "exit_reason": report.exit_reason})
        return report
    _finish_log(command, code, reason)
    return RunReport(source=source, exit_code=code, exit_reason=reason, result=result, out_dir=str(out))


def _compare_snapshot(source: str, options: CompareOptions, names: list[str]) -> snapshot.Snapshot:
    """compare：只读地加载快照。"""
    return _snapshot_step(
        "compare", source, RunReport, lambda: snapshot.load(snapshot.readable(options.snapshot), names)
    )


async def _paired(command: Mode, path: str | Path, options: RunOptions, env: Mapping[str, str] | None,
                  on_event: OnEvent | None, cancel_event: CancelToken | None,
                  transport: httpx.AsyncBaseTransport | None) -> RunReport:  # fmt: skip
    source, started = str(path), _now()
    sides: tuple[Side, ...] = SIDES if command == "run" else ("candidate",)
    loaded = await _load(command, path, sides, env, RunReport)
    overrides = {"--candidate-url": options.candidate_url}
    if command == "run":
        overrides["--baseline-url"] = options.baseline_url
    _check_usage(command, source, [r.name for r in loaded.suite.requests], options.only, overrides, RunReport)
    plans = _plans(loaded, options.only)
    snap = None
    if isinstance(options, CompareOptions):
        snap = _compare_snapshot(source, options, [plan.name for plan in plans])
    out = options.out or rundir.default_out_dir(started)
    try:
        rundir.check(out, _ignored(options))
    except OSError as exc:
        raise _internal_error(command, source, str(exc), RunReport) from None
    log.info("%s 开始：%d 条请求，run 目录 %s", command, len(plans), out)
    targets = Targets(options.baseline_url if command == "run" else None, options.candidate_url, snap)
    executor = Executor(loaded.suite, command, targets, on_event, cancel_event)
    executed = await executor.run_all(plans, transport)
    return _finish(command, source, options, out, executed, started, loaded.suite, _internal(executor, executed))


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
    try:
        return await _paired("run", path, options or RunOptions(), env, on_event, cancel_event, transport)
    except _Stop as stop:
        assert isinstance(stop.report, RunReport)
        return stop.report


def run_sync(path: str | Path, options: RunOptions | None = None, **kwargs: Any) -> RunReport:
    return asyncio.run(run(path, options, **kwargs))


async def compare(
    path: str | Path,
    options: CompareOptions,
    *,
    env: Mapping[str, str] | None = None,
    on_event: OnEvent | None = None,
    cancel_event: CancelToken | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunReport:
    """基线读快照，候选实时请求 1 次，然后逐条对比。只需要 candidate 的环境变量。

    快照缺失或指纹不一致的请求是 inconclusive，但候选照样请求并写进 run 目录，随后可以 accept。"""
    try:
        return await _paired("compare", path, options, env, on_event, cancel_event, transport)
    except _Stop as stop:
        assert isinstance(stop.report, RunReport)
        return stop.report


def compare_sync(path: str | Path, options: CompareOptions, **kwargs: Any) -> RunReport:
    return asyncio.run(compare(path, options, **kwargs))


# ---------- record ----------

_NOT_WRITTEN = "条请求没有写入快照"


class RecordOptions(BaseModel):
    snapshot: Path
    clean: bool = False
    """先清空快照：新快照只包含本次写入的请求。"""
    baseline_url: str | None = None
    only: list[str] = []


class RecordReport(CommandReport):
    """record 的返回值。requests 是每条请求只对基线判定的结果（含不稳定字段建议），written 是写入的请求名。"""

    snapshot: str | None = None
    requests: list[RequestResult] = []
    written: list[str] = []
    cancelled: bool = False

    @property
    def not_written(self) -> list[RequestResult]:
        return [r for r in self.requests if r.name not in self.written]


def _record_entries(executed: list[Executed]) -> list[snapshot.NewEntry]:
    return [
        snapshot.NewEntry(item.plan.name, item.plan.fingerprint, item.keep, "record", item.plan.request.compare_headers)
        for item in executed
        if item.keep
    ]


def _save(command: str, source: str, root: Path, entries: list[snapshot.NewEntry], base: snapshot.Manifest | None,
          report: type[CommandReport]) -> None:  # fmt: skip
    _snapshot_step(command, source, report, lambda: snapshot.write(root, entries, base, _now()))


async def _record(path: str | Path, options: RecordOptions, env: Mapping[str, str] | None, on_event: OnEvent | None,
                  cancel_event: CancelToken | None, transport: httpx.AsyncBaseTransport | None) -> RecordReport:  # fmt: skip
    source = str(path)
    loaded = await _load("record", path, ("baseline",), env, RecordReport)
    names = [r.name for r in loaded.suite.requests]
    _check_usage("record", source, names, options.only, {"--baseline-url": options.baseline_url}, RecordReport)
    root, base = _open_snapshot("record", source, options.snapshot, options.clean, RecordReport)
    plans = _plans(loaded, options.only)
    log.info("record 开始：%d 条请求，快照 %s%s", len(plans), root, "（--clean）" if options.clean else "")
    executor = Executor(loaded.suite, "record", Targets(baseline_url=options.baseline_url), on_event, cancel_event)
    executed = await executor.run_all(plans, transport)
    entries = _record_entries(executed)
    if entries:
        _save("record", source, root, entries, base, RecordReport)
    elif options.clean:
        log.warning("一条请求都没有写入，--clean 不生效，快照 %s 保持不变", root)
    requests = [item.result for item in executed]
    state = ExitState(
        internal_error=_internal(executor, executed), incomplete=len(requests) - len(entries), incomplete_text=_NOT_WRITTEN
    )
    code, reason = exit_code(state)
    _finish_log("record", code, reason)
    return RecordReport(
        source=source, exit_code=code, exit_reason=reason, snapshot=str(options.snapshot), requests=requests,
        written=[e.name for e in entries], cancelled=any(item.cancelled for item in executed),
    )  # fmt: skip


async def record(
    path: str | Path,
    options: RecordOptions,
    *,
    env: Mapping[str, str] | None = None,
    on_event: OnEvent | None = None,
    cancel_event: CancelToken | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RecordReport:
    """只请求基线（每条串行 2 次），按请求合并写入快照。只需要 baseline 的环境变量。

    error、401/403/429、5xx、不满足 success 的请求不写入，保留旧 sample。"""
    try:
        return await _record(path, options, env, on_event, cancel_event, transport)
    except _Stop as stop:
        assert isinstance(stop.report, RecordReport)
        return stop.report


def record_sync(path: str | Path, options: RecordOptions, **kwargs: Any) -> RecordReport:
    return asyncio.run(record(path, options, **kwargs))


# ---------- accept ----------


class AcceptOptions(BaseModel):
    snapshot: Path
    only: list[str] = []


class Rejected(BaseModel):
    name: str
    reason: str


class AcceptReport(CommandReport):
    """accept 的返回值。source 是 run 目录。"""

    snapshot: str | None = None
    accepted: list[str] = []
    rejected: list[Rejected] = []


def _run_dir_result(run_dir: Path) -> RunResult:
    try:
        return rundir.read_result(run_dir)
    except OSError as exc:
        raise _internal_error("accept", str(run_dir), str(exc), AcceptReport) from None


def _judge_accept(run_dir: Path, req: RequestResult) -> tuple[snapshot.NewEntry | None, str | None, bool]:
    """返回 (要写入的条目, 拒绝原因, run 目录是否损坏)。"""
    try:
        side = rundir.read_side(run_dir, "candidate", req.name)
    except SampleFormatError as exc:
        return None, f"run 目录里的候选响应损坏：{exc}", True
    samples = side.samples if side is not None else []
    if side is not None and (side.name != req.name or len(samples) != 1):
        return None, "run 目录里的候选响应损坏：name 不对或 sample 数不是 1", True
    reason = acceptance.rejection(req, samples[0] if samples else None)
    if reason is not None or side is None:
        return None, reason, False
    return snapshot.NewEntry(req.name, side.fingerprint, samples, "accept", list(samples[0].headers)), None, False


def accept_sync(run_dir: str | Path, options: AcceptOptions) -> AcceptReport:
    """把 run 目录里的候选响应按请求合并进快照（快照不存在时新建）。不读 suite。"""
    try:
        return _accept(Path(run_dir), options)
    except _Stop as stop:
        assert isinstance(stop.report, AcceptReport)
        return stop.report


def _accept(run_dir: Path, options: AcceptOptions) -> AcceptReport:
    source = str(run_dir)
    result = _run_dir_result(run_dir)
    names = [r.name for r in result.requests]
    _check_usage("accept", source, names, options.only, {}, AcceptReport)
    root, base = _open_snapshot("accept", source, options.snapshot, False, AcceptReport)
    entries: list[snapshot.NewEntry] = []
    rejected: list[Rejected] = []
    broken = False
    for req in result.requests:
        if options.only and req.name not in options.only:
            continue
        entry, reason, bad = _judge_accept(run_dir, req)
        broken = broken or bad
        if entry is not None:
            entries.append(entry)
        else:
            log.info("%s：拒绝（%s）", req.name, reason)
            rejected.append(Rejected(name=req.name, reason=reason or ""))
    if entries:
        _save("accept", source, root, entries, base, AcceptReport)
    code, reason = exit_code(ExitState(internal_error=broken, incomplete=len(rejected), incomplete_text=_NOT_WRITTEN))
    _finish_log("accept", code, reason)
    return AcceptReport(
        source=source, exit_code=code, exit_reason=reason, snapshot=str(options.snapshot), accepted=[e.name for e in entries],
        rejected=rejected,
    )  # fmt: skip


async def accept(run_dir: str | Path, options: AcceptOptions) -> AcceptReport:
    return await asyncio.to_thread(accept_sync, run_dir, options)
