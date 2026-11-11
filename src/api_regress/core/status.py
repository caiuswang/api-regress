"""请求状态判定：有序的 (谓词, 状态, 提示) 表，第一条匹配生效。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from api_regress.core.models import Side
from api_regress.core.samples import Sample

RequestStatus = Literal["ok", "error", "inconclusive", "unsupported"]
REQUEST_STATUSES: tuple[RequestStatus, ...] = ("ok", "error", "inconclusive", "unsupported")

_AUTH_OR_LIMIT = frozenset({401, 403, 429})
_SIDE_NAME = {"baseline": "基线", "candidate": "候选"}


@dataclass(frozen=True)
class Facts:
    """判定所需的事实。base_success：配了 success 且对基线求值了时为 True/False，没有求值为 None。"""

    method: str
    base: Sample
    cand: Sample
    base_json: bool = False
    cand_json: bool = False
    base_success: bool | None = None
    snapshot_problem: str | None = None

    def sides(self) -> tuple[tuple[Side, Sample], ...]:
        return (("baseline", self.base), ("candidate", self.cand))

    @property
    def body_less(self) -> bool:
        """HEAD 或 204：没有 body 可比。"""
        return self.method == "HEAD" or 204 in (self.base.status, self.cand.status)


def _first(f: Facts, predicate: Callable[[Sample], bool]) -> tuple[Side, Sample] | None:
    return next(((side, s) for side, s in f.sides() if predicate(s)), None)


def _failed(f: Facts) -> tuple[Side, Sample] | None:
    return _first(f, lambda s: s.failed)


def _auth(f: Facts) -> tuple[Side, Sample] | None:
    return _first(f, lambda s: s.status in _AUTH_OR_LIMIT)


def _error_hint(f: Facts) -> str:
    found = _failed(f)
    assert found is not None
    side, sample = found
    return f"{_SIDE_NAME[side]}请求失败：{sample.error}（已重试 {sample.retries} 次）"


def _auth_hint(f: Facts) -> str:
    found = _auth(f)
    assert found is not None
    side, sample = found
    return f"{_SIDE_NAME[side]}返回 {sample.status}，检查{_SIDE_NAME[side]}的 token 或限流"


Rule = tuple[Callable[[Facts], bool], RequestStatus, Callable[[Facts], str | None]]

STATUS_RULES: tuple[Rule, ...] = (
    (lambda f: _failed(f) is not None, "error", _error_hint),
    (lambda f: f.snapshot_problem is not None, "inconclusive", lambda f: f"{f.snapshot_problem}，请 record 或 accept"),
    (lambda f: _auth(f) is not None, "inconclusive", _auth_hint),
    (lambda f: (f.base.status or 0) >= 500, "inconclusive", lambda f: f"基线返回 {f.base.status}，基线不可用"),
    (lambda f: f.base_success is False, "inconclusive", lambda f: "基线不满足 success，基线业务失败"),
    (
        lambda f: not f.base_json and not f.cand_json and not f.body_less,
        "unsupported",
        lambda f: "两侧都不是 JSON，只比较状态码",
    ),
    (lambda f: True, "ok", lambda f: None),
)


def judge(facts: Facts) -> tuple[RequestStatus, str | None]:
    """返回 (状态, 提示)。"""
    for predicate, status, hint in STATUS_RULES:
        if predicate(facts):
            return status, hint(facts)
    raise AssertionError("STATUS_RULES 的最后一条总是匹配")
