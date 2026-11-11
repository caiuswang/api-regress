"""accept 的判定：被接受的候选响应会成为新的基线，所以除了看请求状态，还按 record 的标准检查候选本身。

有序的 (谓词, 原因) 表，第一条匹配的就是拒绝原因；都不匹配则接受。判定只用 run 目录里的信息，不读 suite。
"""

from __future__ import annotations

from collections.abc import Callable

from api_regress.core.result import RequestResult
from api_regress.core.samples import Sample
from api_regress.core.status import AUTH_OR_LIMIT

AcceptRule = tuple[Callable[[RequestResult, Sample | None], bool], Callable[[RequestResult, Sample | None], str]]


def _cand_status(cand: Sample | None) -> int:
    assert cand is not None and cand.status is not None
    return cand.status


def _business_failed(req: RequestResult) -> bool:
    return req.candidate_success is False or any(f.type == "business_error" for f in req.findings)


ACCEPT_RULES: tuple[AcceptRule, ...] = (
    (lambda r, c: r.status == "error", lambda r, c: f"请求是 error（{r.reason}）"),
    (
        lambda r, c: r.status == "inconclusive" and r.snapshot_problem is None,
        lambda r, c: f"请求是 inconclusive（{r.reason}）",
    ),
    (lambda r, c: c is None or c.failed, lambda r, c: "run 目录里没有可用的候选响应"),
    (
        lambda r, c: _cand_status(c) in AUTH_OR_LIMIT,
        lambda r, c: f"候选返回 {_cand_status(c)}，检查候选的 token 或限流",
    ),
    (lambda r, c: _cand_status(c) >= 500, lambda r, c: f"候选返回 {_cand_status(c)}，不能作为基线"),
    (lambda r, c: _business_failed(r), lambda r, c: "候选不满足 success"),
)


def rejection(req: RequestResult, cand: Sample | None) -> str | None:
    """拒绝原因；可以接受时返回 None。cand 是 run 目录里的候选 sample（没有时为 None）。"""
    for predicate, reason in ACCEPT_RULES:
        if predicate(req, cand):
            return reason(req, cand)
    return None
