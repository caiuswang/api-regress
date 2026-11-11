"""单条请求的对比流程：拿到两侧的 sample 之后，按固定顺序判定和比较。

1. 判定请求状态（status.judge），只有 ok 才继续（unsupported 只比状态码）
2. 状态码不同：status_changed，就此结束
3. 候选的 success：不满足记 business_error，跳过 body 和 asserts
4. compare_headers
5. body（两侧都是 JSON 才比；一侧是 JSON 记 body_kind_changed）
6. asserts
7. 不稳定字段建议（基线两个 sample 都可用时）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from api_regress.core import expr
from api_regress.core.config import Assert, Request
from api_regress.core.diff import diff_json
from api_regress.core.diff.context import CompareMode
from api_regress.core.diff.unstable import suggest_unstable
from api_regress.core.jsonload import LoadedBody, load_body, media_type
from api_regress.core.models import ABSENT, Finding, FindingType, Level, Side, Suggestion, WarningCode, WarningItem, finding_id
from api_regress.core.result import RequestResult, SampleInfo
from api_regress.core.rules import RuleSet
from api_regress.core.samples import CONTENT_TYPE, Sample
from api_regress.core.status import Facts, RequestStatus, SnapshotProblem, judge

log = logging.getLogger(__name__)

_SIDE_NAME = {"baseline": "基线", "candidate": "候选"}


@dataclass
class Plan:
    """一条请求的对比配置（编译好的规则和表达式）。"""

    request: Request
    fingerprint: str
    rules: RuleSet
    compare: CompareMode = "structure"
    success: expr.CompiledExpr | None = None
    asserts: list[tuple[Assert, expr.CompiledExpr]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.request.name


@dataclass
class Loaded:
    sample: Sample
    body: LoadedBody

    @property
    def is_json(self) -> bool:
        return self.body.is_json


@dataclass
class Evaluation:
    """流程的输出：结果，以及解析出来的 JSON body（给「本次未命中的规则」用）。"""

    result: RequestResult
    bodies: list[Any] = field(default_factory=list)
    keep: list[Sample] = field(default_factory=list)
    """record 时要写进快照的 sample。"""


class _Flow:
    """cand 为 None 时（record）只看基线：self.cand 指向基线。"""

    def __init__(self, plan: Plan, base_samples: list[Sample], cand: Sample | None):
        self.plan = plan
        self.method = plan.request.method
        self.base_samples = base_samples
        self.base = self._load(base_samples[0], "baseline")
        self.cand = self.base if cand is None else self._load(cand, "candidate")
        self.findings: list[Finding] = []
        self.suggestions: list[Suggestion] = []
        self.warnings: list[WarningItem] = list(self.base.body.warnings)
        if cand is not None:
            self.warnings += self.cand.body.warnings
        self.cand_success: bool | None = None
        self._cand_evaluated = False

    @property
    def body_less(self) -> bool:
        return Facts(self.method, self.base.sample, self.cand.sample).body_less

    def _load(self, sample: Sample, side: Side) -> Loaded:
        if sample.failed:
            return Loaded(sample, LoadedBody(is_json=False))
        return Loaded(sample, load_body(sample.body, sample.content_type, request=self.plan.name, side=side))

    # ---------- 收集 ----------

    def report(self, kind: FindingType, level: Level, path: str, base: Any, cand: Any, message: str | None = None) -> None:
        self.findings.append(
            Finding(type=kind, level=level, path=path, generalized_path=path, base_value=base, cand_value=cand, message=message)
        )

    def warn(self, code: WarningCode, message: str) -> None:
        self.warnings.append(WarningItem(code=code, message=message, request=self.plan.name))

    # ---------- success ----------

    def _success_applies(self, loaded: Loaded) -> bool:
        """只对 JSON 响应求值；HEAD、204、非 JSON 跳过。"""
        return self.plan.success is not None and self.method != "HEAD" and loaded.sample.status != 204 and loaded.is_json

    def success(self, loaded: Loaded, side: Side, warn: bool = True) -> bool | None:
        """没有求值时返回 None；求值出错视为不满足，并产生 success_eval_error（warn 为假时不产生）。"""
        if not self._success_applies(loaded):
            return None
        assert self.plan.success is not None and loaded.sample.status is not None
        outcome = expr.eval_success(self.plan.success, loaded.sample.status, loaded.sample.headers, loaded.body.value)
        if outcome.error is not None and warn:
            self.warn("success_eval_error", f"{_SIDE_NAME[side]}的 success 求值出错，视为不满足：{outcome.error}")
        return outcome.passed

    # ---------- 各步骤 ----------

    def status_changed(self) -> bool:
        b, c = self.base.sample.status, self.cand.sample.status
        if b != c:
            self.report("status_changed", "breaking", "$status", b, c)
        return b != c

    def both_non_2xx(self) -> None:
        status = self.base.sample.status or 0
        if self.plan.success is None and not 200 <= status < 300:
            self.warn("both_non_2xx", f"两侧都返回 {status}，都没有成功，测试数据可能已失效")

    def candidate_success(self, warn: bool = True) -> bool | None:
        """候选的 success 只求值一次（出错的 warning 只给一条）。accept 靠它判断候选能不能当基线。"""
        if not self._cand_evaluated:
            self._cand_evaluated = True
            self.cand_success = None if self.cand.sample.failed else self.success(self.cand, "candidate", warn)
        return self.cand_success

    def business_error(self) -> bool:
        if self.candidate_success() is not False:
            return False
        self.report("business_error", "breaking", "$success", True, False, "基线满足 success，候选不满足")
        return True

    def headers(self) -> None:
        for name in self.plan.request.compare_headers:
            key = name.lower()
            b, c = self.base.sample.headers.get(key), self.cand.sample.headers.get(key)
            found = _header_finding(key, b, c)
            if found is not None:
                kind, level = found
                self.report(kind, level, f"$header.{name}", ABSENT if b is None else b, ABSENT if c is None else c)

    def body(self) -> None:
        if self.base.is_json and self.cand.is_json:
            diff = diff_json(self.base.body.value, self.cand.body.value, self.plan.rules, self.plan.compare, self.plan.name)
            self.findings += diff.findings
            self.warnings += diff.warnings
        elif self.base.is_json != self.cand.is_json:
            b, c = (_body_kind(x) for x in (self.base, self.cand))
            self.report("body_kind_changed", "breaking", "$body", b, c, "一侧是 JSON，另一侧不是")

    def asserts(self) -> None:
        if not (self.base.is_json and self.cand.is_json):
            return
        base = _side_view(self.base)
        cand = _side_view(self.cand)
        for i, (spec, compiled) in enumerate(self.plan.asserts):
            outcome = expr.eval_assert(compiled, base, cand)
            label = spec.message or spec.expr
            if outcome.error is not None:
                self.report("assert_error", "suspicious", f"$assert[{i}]", None, None, f"{label}：{outcome.error}")
            elif not outcome.passed:
                self.report("assert_failed", spec.level, f"$assert[{i}]", None, None, label)

    def second_sample(self) -> Loaded | None:
        """基线的第 2 个 sample：失败或不满足 success 时不出建议，产生 warning。"""
        if len(self.base_samples) < 2:
            return None
        second = self._load(self.base_samples[1], "baseline")
        problem = _second_problem(second, self.base.is_json) or (
            "不满足 success" if self.success(second, "baseline") is False else None
        )
        if problem is None:
            return second
        self.warn("second_sample_unavailable", f"基线第 2 次请求{problem}，本次没有不稳定字段建议")
        return None


def _header_finding(key: str, base: str | None, cand: str | None) -> tuple[FindingType, Level] | None:
    """候选缺失：breaking；值变化：suspicious；Content-Type 只看媒体类型，变了算 breaking，只改 charset 不算。"""
    if base == cand or (base is None and cand is None):
        return None
    if cand is None:
        return "header_missing", "breaking"
    if key == CONTENT_TYPE:
        return None if media_type(base) == media_type(cand) else ("header_changed", "breaking")
    return "header_changed", "suspicious"


def _body_kind(loaded: Loaded) -> str:
    return "json" if loaded.is_json else "non-json"


def _side_view(loaded: Loaded) -> expr.SideView:
    assert loaded.sample.status is not None
    return expr.SideView(loaded.sample.status, loaded.sample.headers, loaded.body.value)


def _second_problem(second: Loaded, first_is_json: bool) -> str | None:
    if second.sample.failed:
        return f"失败（{second.sample.error}）"
    if first_is_json and not second.is_json:
        return "的响应不是 JSON"
    return None


def _compare(flow: _Flow, status: RequestStatus) -> list[Any]:
    """ok 之后的步骤。返回参与「本次未命中的规则」检测的 body。"""
    if flow.status_changed():
        # 流程就此结束，不报 success 相关的 warning；但 accept 仍要知道候选满不满足 success
        flow.candidate_success(warn=False)
        return []
    if status != "ok":
        return []
    flow.both_non_2xx()
    business = flow.business_error()
    flow.headers()
    if not flow.body_less and not business:
        flow.body()
        flow.asserts()
    second = flow.second_sample()
    if second is not None and second.is_json and flow.base.is_json:
        return _suggest(flow, second)
    return []


def _suggest(flow: _Flow, second: Loaded) -> list[Any]:
    flow.suggestions = suggest_unstable(flow.base.body.value, second.body.value, flow.plan.rules)
    return [second.body.value]


# 快照里没有这条请求时，基线用这个占位：status.judge 的「快照问题」排在网络错误之后、其他规则之前，
# 有 snapshot_problem 时占位 sample 不会影响判定（它既不失败，也不会走到 401/5xx/success 那几条）。
_NO_BASELINE = Sample(url="", status=200)


def _result(flow: _Flow, status: RequestStatus, reason: str | None, base_samples: list[Sample], cand: Sample | None) -> RequestResult:
    return RequestResult(
        name=flow.plan.name,
        method=flow.method,
        status=status,
        reason=reason,
        fingerprint=flow.plan.fingerprint,
        baseline=[SampleInfo.of(s) for s in base_samples],
        candidate=None if cand is None else SampleInfo.of(cand),
        findings=flow.findings,
        warnings=flow.warnings,
        suggestions=flow.suggestions,
        candidate_success=flow.cand_success,
    )


def _log(flow: _Flow, status: RequestStatus, reason: str | None) -> None:
    log.info("%s：%s，%d 条 finding，%d 条 warning%s", flow.plan.name, status, len(flow.findings), len(flow.warnings),
             f"（{reason}）" if reason else "")  # fmt: skip


def _facts(flow: _Flow, cand: Sample, snapshot_problem: SnapshotProblem | None) -> Facts:
    base_success = None if flow.base.sample.failed or cand.failed else flow.success(flow.base, "baseline")
    return Facts(
        method=flow.method,
        base=flow.base.sample,
        cand=cand,
        base_json=flow.base.is_json,
        cand_json=flow.cand.is_json,
        base_success=base_success,
        snapshot_problem=snapshot_problem,
    )


def evaluate(
    plan: Plan, base_samples: list[Sample], cand: Sample, snapshot_problem: SnapshotProblem | None = None
) -> Evaluation:
    """base_samples 有 1 到 2 个，只有第 1 个和候选对比，第 2 个只用来计算不稳定字段建议。

    compare 时 base_samples 来自快照：快照里没有这条请求时为空，这时必须给出 snapshot_problem。"""
    assert base_samples or snapshot_problem is not None
    flow = _Flow(plan, base_samples or [_NO_BASELINE], cand)
    status, reason = judge(_facts(flow, cand, snapshot_problem))
    extra = _compare(flow, status) if status in ("ok", "unsupported") else []
    if status == "inconclusive" and snapshot_problem is not None:
        flow.candidate_success()  # accept 要据此判断能不能接受候选
    for finding in flow.findings:
        finding.finding_id = finding_id(plan.name, finding.type, finding.path)
    result = _result(flow, status, reason, base_samples, cand)
    result.snapshot_problem = snapshot_problem
    _log(flow, status, reason)
    bodies = [x.body.value for x in (flow.base, flow.cand) if x.is_json] + extra
    return Evaluation(result=result, bodies=bodies)


def evaluate_baseline(plan: Plan, base_samples: list[Sample]) -> Evaluation:
    """record 用：只有基线。按请求状态的规则只对基线判定（两侧都看成基线），ok / unsupported 才写入快照。

    写入的 sample 放在 Evaluation.keep：第 2 个不可用（失败、不是 JSON、不满足 success）时只留第 1 个。"""
    flow = _Flow(plan, base_samples, None)
    first = flow.base
    base_success = None if first.sample.failed else flow.success(first, "baseline")
    facts = Facts(
        method=flow.method, base=first.sample, cand=first.sample, base_json=first.is_json, cand_json=first.is_json,
        base_success=base_success,
    )  # fmt: skip
    status, reason = judge(facts)
    keep: list[Sample] = []
    if status in ("ok", "unsupported"):
        second = flow.second_sample()
        keep = [first.sample] + ([second.sample] if second is not None else [])
        if second is not None and second.is_json and first.is_json:
            _suggest(flow, second)
    _log(flow, status, reason)
    return Evaluation(result=_result(flow, status, reason, base_samples, None), keep=keep)
