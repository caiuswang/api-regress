"""不稳定字段建议：按 `compare: values` 比较基线的两个 sample，两次不同的字段建议 ignore。

只作为建议，不会自动忽略。数组元素的增减（数据量变化）不计入；schema 数组内部不比较值，
所以那里的值变化也不会出现；已经被 ignore 的路径对比引擎本来就不看。
"""

from __future__ import annotations

from typing import Any

import yaml

from api_regress.core.diff.walker import diff_json
from api_regress.core.models import Finding, FindingType, Suggestion
from api_regress.core.rules import RuleSet

_PRESENCE: frozenset[FindingType] = frozenset({"field_removed", "field_added"})
_NOT_COUNTED: frozenset[FindingType] = frozenset({"element_missing", "element_added", "array_length_changed"})


def _reason(finding: Finding) -> str:
    return "只在一次基线响应里出现" if finding.type in _PRESENCE else "基线两次的值不同"


def ignore_snippet(path: str) -> str:
    """一条 ignore 规则的 YAML（列表项），由 yaml.safe_dump 生成，`**.x` 这类路径会自动加引号。"""
    return yaml.safe_dump([{"path": path, "ignore": True}], allow_unicode=True, sort_keys=False)


def suggest_unstable(first: Any, second: Any, rules: RuleSet) -> list[Suggestion]:
    """first / second 是基线两个 sample 解析后的 body。同一个泛化路径只建议一次。"""
    result = diff_json(first, second, rules, compare="values")
    out: dict[str, Suggestion] = {}
    for finding in result.findings:
        path = finding.generalized_path
        if finding.type in _NOT_COUNTED or path in out:
            continue
        out[path] = Suggestion(path=path, reason=_reason(finding), yaml=ignore_snippet(path))
    return list(out.values())
