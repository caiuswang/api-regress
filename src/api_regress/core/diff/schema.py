"""schema 数组：把所有元素合并成一张扁平表（泛化路径 → 类型集合、小数位集合），只比较两侧的表和长度。

unordered 模式也用这里的 `compare_elements` 比较元素结构（spec §7.6、D13）。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from api_regress.core import paths
from api_regress.core.diff.context import DiffContext, Path
from api_regress.core.diff.values import json_type, scale_of
from api_regress.core.models import ABSENT, Level
from api_regress.core.rules import Resolved

CONTAINERS = frozenset({"object", "array"})


@dataclass
class Column:
    """某个路径在所有元素里出现过的类型和小数位，值为第一次出现时的元素下标（用作 example_index）。"""

    types: dict[str, int] = field(default_factory=dict)
    scales: dict[int, int] = field(default_factory=dict)

    @property
    def first(self) -> int:
        return min(self.types.values())

    @property
    def non_null(self) -> set[str]:
        return set(self.types) - {"null"}


Table = dict[Path, Column]


def length_level(base: list, cand: list) -> Level:
    """空 ↔ 非空：suspicious；其他长度变化：info（spec D3）。"""
    return "suspicious" if bool(base) != bool(cand) else "info"


def report_length(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    if len(base) != len(cand):
        ctx.report("array_length_changed", length_level(base, cand), path, len(base), len(cand), res, summary=True)


# ---------- 建表 ----------


def _children(path: Path, value: Any, kind: str) -> Iterator[tuple[Path, Any]]:
    if kind == "object":
        for key, child in value.items():
            yield (*path, paths.Key(key)), child
    elif kind == "array":
        for child in value:
            yield (*path, paths.ANY), child


def _collect(ctx: DiffContext, path: Path, value: Any, index: int, table: Table) -> None:
    res = ctx.resolve(path)
    if res.ignore:
        return
    column = table.setdefault(path, Column())
    kind = json_type(value)
    column.types.setdefault(kind, index)
    if res.decimal is not None:
        found = scale_of(value, res.decimal.allow_string)
        if found is not None:
            column.scales.setdefault(found, index)
    for child_path, child in _children(path, value, kind):
        _collect(ctx, child_path, child, index, table)


def build_table(ctx: DiffContext, elem_path: Path, items: list) -> Table:
    """元素里嵌套的数组也按 schema 方式并入同一张表；index 始终是外层数组的下标（D14）。"""
    table: Table = {}
    for i, item in enumerate(items):
        _collect(ctx, elem_path, item, i, table)
    return table


# ---------- 比较一列 ----------


def _null_change(base: Column, cand: Column, new_types: set[str]) -> tuple[Level, set[str]] | None:
    """null 的出现与消失：返回 (nullability 的级别, 涉及的非 null 类型)；涉及对象或数组时改记 type_changed。"""
    if "null" in cand.types and "null" not in base.types:
        return "suspicious", base.non_null
    if not base.non_null and new_types:
        return "info", new_types
    if "null" in base.types and "null" not in cand.types:
        return "info", set()
    return None


def _compare_types(ctx: DiffContext, path: Path, base: Column, cand: Column, res: Resolved) -> bool:
    """返回是否已经在这一列报了 type_changed（报了就不再看小数位和子节点）。"""
    new_types = cand.non_null - base.non_null
    base_types, cand_types = sorted(base.types), sorted(cand.types)
    if new_types and base.non_null:
        example = min(cand.types[t] for t in new_types)
        ctx.report("type_changed", "breaking", path, base_types, cand_types, res, example, summary=True)
        return True
    change = _null_change(base, cand, new_types)
    if change is None or res.nullable:
        return False
    level, involved = change
    example = cand.types.get("null", cand.first) if level == "suspicious" else cand.first
    if involved & CONTAINERS:
        ctx.report("type_changed", "breaking", path, base_types, cand_types, res, example, summary=True)
        return True
    ctx.report("nullability_changed", level, path, base_types, cand_types, res, example, summary=True)
    return False


def _compare_scales(ctx: DiffContext, path: Path, base: Column, cand: Column, res: Resolved) -> None:
    """候选的小数位集合不是基线的子集时记 scale_changed。"""
    if res.decimal is None or not base.scales or not cand.scales:
        return
    new_scales = set(cand.scales) - set(base.scales)
    if new_scales:
        example = min(cand.scales[s] for s in new_scales)
        level = res.scale_level or "breaking"
        base_scales, cand_scales = sorted(base.scales), sorted(cand.scales)
        ctx.report("scale_changed", level, path, base_scales, cand_scales, res, example, summary=True)


def _presence(ctx: DiffContext, path: Path, base_table: Table, cand_table: Table) -> None:
    """只在一侧出现的列。父节点在另一侧已经不是对象（数组）时，变化记在父节点上，这里不再报。"""
    base, cand = base_table.get(path), cand_table.get(path)
    holder = "array" if isinstance(path[-1], paths.AnyIndex) else "object"
    other_parent = (cand_table if cand is None else base_table).get(path[:-1])
    if other_parent is None or holder not in other_parent.types:
        return
    if holder == "array":
        where = ctx.where(path[:-1])
        ctx.warn("array_empty", path[:-1], f"数组 {where} 在一侧的所有元素里都为空，未比较它的元素结构")
        return
    res = ctx.resolve(path)
    if cand is None:
        assert base is not None
        ctx.report("field_removed", "breaking", path, sorted(base.types), ABSENT, res, base.first, summary=True)
    else:
        ctx.report("field_added", "info", path, ABSENT, sorted(cand.types), res, cand.first, summary=True)


def _compare_column(ctx: DiffContext, path: Path, base_table: Table, cand_table: Table) -> bool:
    """返回是否不再看这一列的子节点。"""
    base, cand = base_table.get(path), cand_table.get(path)
    if base is None or cand is None:
        _presence(ctx, path, base_table, cand_table)
        return True
    res = ctx.resolve(path)
    if _compare_types(ctx, path, base, cand, res):
        return True
    _compare_scales(ctx, path, base, cand, res)
    return False


def _under(path: Path, reported: set[Path]) -> bool:
    return any(path[:k] in reported for k in range(len(path)))


# ---------- 元素内部不生效的规则 ----------


def _rule_path(ctx: DiffContext, res: Resolved, attr: str) -> str:
    ref = res.sources[attr]
    return next(r.rule.path for r in ctx.rules.rules if r.ref == ref)


def _warn_rules(ctx: DiffContext, path: Path, table: Table, schema_mode: bool) -> None:
    """元素没有逐个配对：值规则不生效；schema 模式下内层数组的非 schema array 规则也不生效（spec §7.6、D13、D14）。

    unordered 自己按多重集合比较值，内层数组的 positional 规则在那里仍然有用，所以不提示。"""
    where = ctx.where(path)
    hint = "需要逐个比较元素请改用 keyed 模式"
    if schema_mode and ctx.compare == "values":
        ctx.warn("rule_ineffective_in_schema", path, f"数组 {where} 的元素没有配对，compare: values 对元素内部不生效；{hint}")
    for column_path, column in table.items():
        res = ctx.resolve(column_path)
        if res.on_value_change is not None:
            rule = _rule_path(ctx, res, "on_value_change")
            ctx.warn("rule_ineffective_in_schema", path, f"规则 {rule!r} 位于数组 {where} 的元素内部，不比较值；{hint}")
        if schema_mode and res.array is not None and res.array.mode != "schema" and "array" in column.types:
            rule = _rule_path(ctx, res, "array")
            ctx.warn(
                "rule_ineffective_in_schema",
                path,
                f"规则 {rule!r}（mode: {res.array.mode}）位于数组 {where} 的元素内部，外层没有配对，内层按 schema 比较",
            )


# ---------- 入口 ----------


def compare_elements(ctx: DiffContext, path: Path, base: list, cand: list, *, schema_mode: bool) -> None:
    """不配对地比较两侧元素的结构。unordered 调用时 schema_mode 为假。"""
    if not base and not cand:
        return
    if not base or not cand:
        ctx.warn("array_empty", path, f"数组 {ctx.where(path)} 有一侧为空，未比较元素结构")
        return
    elem = (*path, paths.ANY)
    base_table, cand_table = build_table(ctx, elem, base), build_table(ctx, elem, cand)
    _warn_rules(ctx, path, base_table | cand_table, schema_mode)
    reported: set[Path] = set()
    # 两张表都是先父后子插入的，合并后父节点仍在子节点之前
    for column_path in dict.fromkeys([*base_table, *cand_table]):
        if not _under(column_path, reported) and _compare_column(ctx, column_path, base_table, cand_table):
            reported.add(column_path)


def compare_schema(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    report_length(ctx, path, base, cand, res)
    compare_elements(ctx, path, base, cand, schema_mode=True)
