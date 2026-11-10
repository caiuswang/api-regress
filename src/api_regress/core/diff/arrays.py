"""数组对齐：schema / keyed / positional / unordered 四种模式（spec §7.6）。"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from typing import Any

from api_regress.core import paths
from api_regress.core.diff import schema
from api_regress.core.diff.context import ELEMENT, DiffContext, Path
from api_regress.core.diff.values import SCALARS, canon_scalar, json_type
from api_regress.core.models import ABSENT
from api_regress.core.rules import Resolved

_BARE_LABEL = re.compile(r'[^\s,=\[\]"]+')


def compare_array(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    mode = res.array.mode if res.array is not None else "schema"
    _MODES[mode](ctx, path, base, cand, res)


def _at(items: list, index: int) -> Any:
    return items[index] if index < len(items) else ABSENT


def _positional(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    schema.report_length(ctx, path, base, cand, res)
    for i in range(max(len(base), len(cand))):
        ctx.visit(ctx, (*path, paths.Index(i)), _at(base, i), _at(cand, i), ELEMENT)


# ---------- keyed ----------


def _number_label(value: int | Decimal) -> str:
    """数字按数值展示，同一个 key 写成 1、1.0、1E0 时标签相同（finding_id 跨运行稳定）。
    这里只用于展示，不涉及小数位，所以可以 normalize。"""
    if isinstance(value, int) or not value.is_finite():
        return str(value)
    return format(value.normalize(), "f")


def _label_value(value: Any) -> str:
    """数字不加引号；简单字符串不加引号；会和数字、true/false/null 混淆的字符串加引号。"""
    kind = json_type(value)
    if kind == "number":
        return _number_label(value)
    if kind == "string" and _BARE_LABEL.fullmatch(value):
        try:
            json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False)


def _label(keys: list[str], item: dict) -> str:
    return ",".join(f"{k}={_label_value(item[k])}" for k in keys)


def _element_key(item: Any, keys: list[str]) -> tuple | None:
    if not isinstance(item, dict):
        return None
    values = [item.get(k, ABSENT) for k in keys]
    if any(v is ABSENT or json_type(v) not in SCALARS for v in values):
        return None
    return tuple(canon_scalar(v) for v in values)


def _index_by_key(items: list, keys: list[str], side: str) -> tuple[dict[tuple, dict] | None, str]:
    """返回 (key → 元素, 降级原因)；key 缺失（或不是标量）、重复时返回 None。"""
    out: dict[tuple, dict] = {}
    for i, item in enumerate(items):
        key = _element_key(item, keys)
        if key is None:
            return None, f"{side}第 {i} 个元素缺少 key 字段 {', '.join(keys)}（或不是标量）"
        if key in out:
            return None, f"{side}的 key {_label(keys, item)} 重复"
        out[key] = item
    return out, ""


def _keyed(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    assert res.array is not None and res.array.keys
    keys = res.array.keys
    base_map, why = _index_by_key(base, keys, "基线")
    cand_map, why_cand = _index_by_key(cand, keys, "候选")
    if base_map is None or cand_map is None:
        where = ctx.where(path)
        ctx.warn("array_degraded_to_schema", path, f"数组 {where} 按 keyed 对齐失败：{why or why_cand}，降级为 schema 模式")
        schema.compare_schema(ctx, path, base, cand, res)
        return
    for key in dict.fromkeys([*base_map, *cand_map]):
        b, c = base_map.get(key, ABSENT), cand_map.get(key, ABSENT)
        label = _label(keys, c if b is ABSENT else b)
        ctx.visit(ctx, (*path, paths.KeyedElem(label)), b, c, ELEMENT)


# ---------- unordered ----------


def _canon_array(ctx: DiffContext, path: Path, value: list) -> Any:
    elem = (*path, paths.ANY)
    if ctx.resolve(elem).ignore:
        return ("a", ())
    items = [_canon_at(ctx, elem, v) for v in value]
    rule = ctx.resolve(path).array
    if rule is not None and rule.mode == "positional":
        return ("a", tuple(items))
    return ("m", frozenset(Counter(items).items()))


def _canon_at(ctx: DiffContext, path: Path, value: Any) -> Any:
    """可 hash 的规范形式：跳过被 ignore 的字段；内层数组除 positional 外按多重集合；
    配了 decimal.allow_string 的字段，数字字符串按数值。"""
    kind = json_type(value)
    if kind == "object":
        children = ((k, (*path, paths.Key(k)), v) for k, v in value.items())
        return ("o", frozenset((k, _canon_at(ctx, p, v)) for k, p, v in children if not ctx.resolve(p).ignore))
    if kind == "array":
        return _canon_array(ctx, path, value)
    decimal = ctx.resolve(path).decimal
    return canon_scalar(value, decimal is not None and decimal.allow_string)


def _unordered(ctx: DiffContext, path: Path, base: list, cand: list, res: Resolved) -> None:
    """元素结构按 schema 比较；值按多重集合比较，不相等时在数组路径上记一条 value_changed（D13）。"""
    elem = (*path, paths.ANY)
    if ctx.resolve(elem).ignore:
        return
    schema.compare_elements(ctx, path, base, cand, schema_mode=False)
    if Counter(_canon_at(ctx, elem, v) for v in base) != Counter(_canon_at(ctx, elem, v) for v in cand):
        ctx.report("value_changed", res.on_value_change or "suspicious", path, base, cand, res)


_MODES = {
    "schema": schema.compare_schema,
    "keyed": _keyed,
    "positional": _positional,
    "unordered": _unordered,
}
