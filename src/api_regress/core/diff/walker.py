"""body 对比入口：按 JSON 类型分发，只报最高层节点（spec §7.4、§7.5）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from api_regress.core import paths
from api_regress.core.diff import arrays
from api_regress.core.diff.context import FIELD, CompareMode, DiffContext, Path, Presence
from api_regress.core.diff.values import SCALARS, json_type, scalar_equal, scale_of
from api_regress.core.models import ABSENT, Finding, WarningItem
from api_regress.core.rules import Resolved, RuleSet

log = logging.getLogger(__name__)


@dataclass
class DiffResult:
    findings: list[Finding] = field(default_factory=list)
    warnings: list[WarningItem] = field(default_factory=list)


def diff_json(
    base: Any, cand: Any, rules: RuleSet, compare: CompareMode = "structure", request: str | None = None
) -> DiffResult:
    """对比两段已解析的 JSON（见 jsonload）。规则按节点合并，ignore 的节点整棵子树不对比。"""
    ctx = DiffContext(rules=rules, compare=compare, visit=visit, request=request)
    visit(ctx, (), base, cand, FIELD)
    log.debug("body 对比完成：%d 条 finding，%d 条 warning", len(ctx.findings), len(ctx.warnings))
    return DiffResult(findings=ctx.findings, warnings=ctx.warnings)


def visit(ctx: DiffContext, path: Path, base: Any, cand: Any, presence: Presence) -> None:
    """base / cand 为 ABSENT 表示这一侧没有这个字段（或元素）。"""
    res = ctx.resolve(path)
    if res.ignore:
        return
    if base is ABSENT or cand is ABSENT:
        _presence(ctx, path, base, cand, presence, res)
        return
    base_type, cand_type = json_type(base), json_type(cand)
    if base_type != cand_type:
        _type_mismatch(ctx, path, base, cand, res)
        return
    _SAME_TYPE.get(base_type, _compare_scalar)(ctx, path, base, cand, res)


def _presence(ctx: DiffContext, path: Path, base: Any, cand: Any, presence: Presence, res: Resolved) -> None:
    if cand is ABSENT and base is not ABSENT:
        ctx.report(presence.removed, presence.removed_level, path, base, ABSENT, res)
    elif base is ABSENT and cand is not ABSENT:
        ctx.report(presence.added, presence.added_level, path, ABSENT, cand, res)


def _type_mismatch(ctx: DiffContext, path: Path, base: Any, cand: Any, res: Resolved) -> None:
    """一个节点只报一条结构类 finding，不再展开子节点（spec §7.4、D10、D12）。"""
    kinds = {json_type(base), json_type(cand)}
    if "null" not in kinds:
        ctx.report("type_changed", "breaking", path, base, cand, res)
        return
    if kinds - {"null"} <= SCALARS:
        if not res.nullable:
            level = "suspicious" if cand is None else "info"
            ctx.report("nullability_changed", level, path, base, cand, res)
        value_level = ctx.value_level(res)
        if value_level is not None:
            ctx.report("value_changed", value_level, path, base, cand, res)
    elif not res.nullable:
        ctx.report("type_changed", "breaking", path, base, cand, res)


def _compare_object(ctx: DiffContext, path: Path, base: dict, cand: dict, res: Resolved) -> None:
    for key in dict.fromkeys([*base, *cand]):
        ctx.visit(ctx, (*path, paths.Key(key)), base.get(key, ABSENT), cand.get(key, ABSENT), FIELD)


def _compare_scalar(ctx: DiffContext, path: Path, base: Any, cand: Any, res: Resolved) -> None:
    allow_string = res.decimal is not None and res.decimal.allow_string
    if res.decimal is not None:
        base_scale, cand_scale = scale_of(base, allow_string), scale_of(cand, allow_string)
        if base_scale is not None and cand_scale is not None and base_scale != cand_scale:
            ctx.report("scale_changed", res.scale_level or "breaking", path, base, cand, res)
    level = ctx.value_level(res)
    if level is not None and not scalar_equal(base, cand, allow_string):
        ctx.report("value_changed", level, path, base, cand, res)


_SAME_TYPE = {
    "object": _compare_object,
    "array": arrays.compare_array,
}
