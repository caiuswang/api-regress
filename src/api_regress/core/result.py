"""`result.json` 的数据模型（对外契约，改动要同步 JSON Schema）和退出码。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from api_regress.core.models import Finding, Level, Suggestion, WarningItem
from api_regress.core.samples import Sample
from api_regress.core.status import RequestStatus, SnapshotProblem

SCHEMA_VERSION = 1

FailOn = Literal["breaking", "suspicious", "info", "never"]
FAIL_ON_LEVELS: dict[FailOn, frozenset[Level]] = {
    "breaking": frozenset({"breaking"}),
    "suspicious": frozenset({"breaking", "suspicious"}),
    "info": frozenset({"breaking", "suspicious", "info"}),
    "never": frozenset(),
}
LEVELS: tuple[Level, ...] = ("breaking", "suspicious", "info")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_SUITE_ERROR = 2
EXIT_INTERNAL_ERROR = 3
EXIT_INCOMPLETE = 4


class SampleInfo(BaseModel):
    """一侧的一次请求。不含请求头；`error` 是网络错误或超时的原因。"""

    url: str
    status: int | None
    elapsed_ms: float | None = None
    retries: int = 0
    content_type: str | None = None
    error: str | None = None

    @classmethod
    def of(cls, sample: Sample) -> SampleInfo:
        return cls(
            url=sample.url,
            status=sample.status,
            elapsed_ms=sample.elapsed_ms,
            retries=sample.retries,
            content_type=sample.content_type,
            error=sample.error,
        )


class RequestResult(BaseModel):
    """一条请求的结果。`baseline` 有 0 到 2 个 sample（快照里没有这条请求时为空），只有第 1 个参与对比。

    `snapshot_problem`：compare 时快照缺失或指纹不一致。`candidate_success`：候选的 success 结果，没有求值为 None。
    这两项给 accept 判断能不能接受候选响应。"""

    name: str
    method: str
    status: RequestStatus
    reason: str | None = None
    fingerprint: str
    snapshot_problem: SnapshotProblem | None = None
    candidate_success: bool | None = None
    baseline: list[SampleInfo] = []
    candidate: SampleInfo | None = None
    findings: list[Finding] = []
    warnings: list[WarningItem] = []
    suggestions: list[Suggestion] = []


class UnmatchedRule(BaseModel):
    """本次未命中的规则：在本次运行的请求的原始 body 里一个路径都没匹配到。request 为 None 表示 defaults 里的规则。"""

    scope: Literal["defaults", "request"]
    index: int
    request: str | None = None
    path: str


class Summary(BaseModel):
    requests: int = 0
    ok: int = 0
    error: int = 0
    inconclusive: int = 0
    unsupported: int = 0
    breaking: int = 0
    suspicious: int = 0
    info: int = 0
    warnings: int = 0

    @classmethod
    def of(cls, requests: Iterable[RequestResult]) -> Summary:
        summary = cls()
        for req in requests:
            summary.requests += 1
            setattr(summary, req.status, getattr(summary, req.status) + 1)
            summary.warnings += len(req.warnings)
            for finding in req.findings:
                setattr(summary, finding.level, getattr(summary, finding.level) + 1)
        return summary


class RunResult(BaseModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    tool_version: str
    command: Literal["run", "compare"]
    suite: str
    started_at: datetime
    finished_at: datetime
    fail_on: FailOn
    cancelled: bool = False
    exit_code: int
    exit_reason: str
    summary: Summary
    requests: list[RequestResult]
    unmatched_rules: list[UnmatchedRule] = []


# ---------- 退出码：按 2 > 3 > 1 > 4 > 0 排列的谓词表，取第一个满足的 ----------


@dataclass(frozen=True)
class ExitState:
    suite_error: bool = False
    internal_error: bool = False
    failing: int = 0
    fail_on: FailOn = "breaking"
    incomplete: int = 0
    incomplete_text: str = "条请求是 error 或 inconclusive"


ExitRule = tuple[int, Callable[[ExitState], bool], Callable[[ExitState], str]]

EXIT_RULES: tuple[ExitRule, ...] = (
    (EXIT_SUITE_ERROR, lambda s: s.suite_error, lambda s: "suite 或命令行参数有问题"),
    (EXIT_INTERNAL_ERROR, lambda s: s.internal_error, lambda s: "工具自身出错"),
    (EXIT_FINDINGS, lambda s: s.failing > 0, lambda s: f"存在 {s.failing} 条达到 {s.fail_on} 级别的 finding"),
    (EXIT_INCOMPLETE, lambda s: s.incomplete > 0, lambda s: f"{s.incomplete} {s.incomplete_text}"),
    (EXIT_OK, lambda s: True, lambda s: "全部通过"),
)


def exit_code(state: ExitState) -> tuple[int, str]:
    for code, predicate, reason in EXIT_RULES:
        if predicate(state):
            return code, reason(state)
    raise AssertionError("EXIT_RULES 的最后一条总是匹配")


def run_exit_state(requests: Iterable[RequestResult], fail_on: FailOn) -> ExitState:
    levels = FAIL_ON_LEVELS[fail_on]
    requests = list(requests)
    failing = sum(1 for req in requests for f in req.findings if f.level in levels)
    incomplete = sum(1 for req in requests if req.status in ("error", "inconclusive"))
    return ExitState(failing=failing, fail_on=fail_on, incomplete=incomplete)

