"""并发执行每条请求：并发名额、取消、进度事件、单条请求的意外异常。run / compare / record 共用。

三种模式只在「基线从哪来、要不要候选」上不同，见 `Executor._MODES`：
- run：基线实时串行 2 次，候选 1 次，两侧同时进行
- compare：基线读快照，候选 1 次
- record：只有基线，串行 2 次
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel

from api_regress.core.config import Suite
from api_regress.core.flow import Evaluation, Plan, evaluate, evaluate_baseline
from api_regress.core.models import Side
from api_regress.core.result import RequestResult
from api_regress.core.rules import CompiledRule, unmatched_rules
from api_regress.core.runner import Call, Runner, make_call, new_client
from api_regress.core.samples import Sample
from api_regress.core.snapshot import Snapshot
from api_regress.core.status import RequestStatus

log = logging.getLogger(__name__)

Mode = Literal["run", "compare", "record"]


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


@dataclass
class Executed:
    """一条请求跑完的结果。missed 是本条请求里没命中的规则，None 表示不参与统计（没有 JSON body）。

    base_samples 是写进 run 目录 baseline/ 的 sample；keep 是 record 要写进快照的 sample。"""

    plan: Plan
    result: RequestResult
    base_samples: list[Sample]
    cand: Sample | None = None
    missed: list[CompiledRule] | None = None
    keep: list[Sample] | None = None
    cancelled: bool = False
    internal_error: bool = False


@dataclass(frozen=True)
class Targets:
    """覆盖地址和快照。compare 必须给 snapshot。"""

    baseline_url: str | None = None
    candidate_url: str | None = None
    snapshot: Snapshot | None = None


class Executor:
    def __init__(
        self, suite: Suite, mode: Mode, targets: Targets, on_event: OnEvent | None, cancel: CancelToken | None
    ):
        self.suite = suite
        self.targets = targets
        self.on_event = on_event or (lambda event: None)
        self.cancel = cancel
        self.callback_errors = 0
        modes: dict[Mode, Callable[[Runner, Plan], Awaitable[Executed]]] = {
            "run": self._run, "compare": self._compare, "record": self._record,
        }  # fmt: skip
        self._work = modes[mode]

    async def run_all(self, plans: list[Plan], transport: httpx.AsyncBaseTransport | None) -> list[Executed]:
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

    async def _one(self, gate: asyncio.Semaphore, runner: Runner, plan: Plan) -> Executed:
        async with gate:
            if self.cancel is not None and self.cancel.is_set():
                return _cancelled(plan)
            self._emit(RequestStarted(name=plan.name))
            try:
                item = await self._work(runner, plan)
            except Exception as exc:  # 单条请求出错不影响其他请求
                item = _crashed(plan, exc)
            self._emit(RequestDone(name=plan.name, status=item.result.status, findings=len(item.result.findings)))
            return item

    def _call(self, plan: Plan, side: Side) -> Call:
        override = self.targets.baseline_url if side == "baseline" else self.targets.candidate_url
        return make_call(self.suite, plan.request, side, override)

    @staticmethod
    async def _baseline(runner: Runner, call: Call) -> list[Sample]:
        """基线串行请求 2 次；第 1 次失败就不再请求第 2 次。"""
        first = await runner.fetch(call)
        return [first] if first.failed else [first, await runner.fetch(call)]

    async def _run(self, runner: Runner, plan: Plan) -> Executed:
        base_samples, cand = await asyncio.gather(
            self._baseline(runner, self._call(plan, "baseline")), runner.fetch(self._call(plan, "candidate"))
        )
        return _paired(plan, evaluate(plan, base_samples, cand), base_samples, cand)

    async def _compare(self, runner: Runner, plan: Plan) -> Executed:
        assert self.targets.snapshot is not None
        cand = await runner.fetch(self._call(plan, "candidate"))
        base_samples, problem = self.targets.snapshot.baseline(plan.name, plan.fingerprint)
        return _paired(plan, evaluate(plan, base_samples, cand, problem), base_samples, cand)

    async def _record(self, runner: Runner, plan: Plan) -> Executed:
        samples = await self._baseline(runner, self._call(plan, "baseline"))
        evaluation = evaluate_baseline(plan, samples)
        return Executed(plan, evaluation.result, samples, keep=evaluation.keep)


def _paired(plan: Plan, evaluation: Evaluation, base_samples: list[Sample], cand: Sample) -> Executed:
    missed = unmatched_rules(plan.rules.rules, evaluation.bodies) if evaluation.bodies else None
    return Executed(plan, evaluation.result, base_samples, cand, missed)


def _error_result(plan: Plan, reason: str) -> RequestResult:
    return RequestResult(
        name=plan.name, method=plan.request.method, status="error", reason=reason, fingerprint=plan.fingerprint
    )


def _cancelled(plan: Plan) -> Executed:
    log.info("%s：error（运行已取消）", plan.name)
    return Executed(plan, _error_result(plan, "运行已取消，没有发出请求"), [], cancelled=True)


def _crashed(plan: Plan, exc: Exception) -> Executed:
    """只记异常类型和调用栈，不记异常消息：消息里可能带着请求的内容。"""
    name = type(exc).__name__
    log.error("%s：内部错误 %s\n%s", plan.name, name, "".join(traceback.format_tb(exc.__traceback__)))
    return Executed(plan, _error_result(plan, f"工具内部错误（{name}），详见日志"), [], internal_error=True)
