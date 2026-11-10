"""JSON 值的类型、小数位、相等性与规范形式。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

JsonType = str  # object / array / string / number / boolean / null

SCALARS = frozenset({"string", "number", "boolean"})

_TYPES: tuple[tuple[type | tuple[type, ...], JsonType], ...] = (
    (bool, "boolean"),  # bool 是 int 的子类，必须排在前面
    ((int, Decimal, float), "number"),
    (str, "string"),
    (dict, "object"),
    (list, "array"),
)

# JSON 数字语法：不允许前导 0、全角数字、下划线
_DECIMAL_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", re.ASCII)


def json_type(value: Any) -> JsonType:
    if value is None:
        return "null"
    for kind, name in _TYPES:
        if isinstance(value, kind):
            return name
    raise TypeError(f"不是 JSON 值：{type(value).__name__}")


def scale(number: int | Decimal) -> int | None:
    """按 JSON 原文算小数位：1.10 → 2，1.50E1 → 1，1E2 → 0。NaN/Infinity 返回 None。

    不能用 Decimal.normalize()，它会抹掉末尾的 0。"""
    if isinstance(number, int):
        return 0
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return max(0, -exponent)


def decimal_text(value: str) -> Decimal | None:
    """`"12.50"` 这类字符串按 JSON 数字语法解析；其他写法（"NaN"、"1_000"、"007"、空白、超出范围）返回 None。"""
    if not _DECIMAL_TEXT.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def scale_of(value: Any, allow_string: bool) -> int | None:
    """decimal 规则下的小数位；不适用时返回 None。"""
    kind = json_type(value)
    if kind == "number":
        return scale(value)
    if kind == "string" and allow_string:
        parsed = decimal_text(value)
        return None if parsed is None else scale(parsed)
    return None


def _number_key(value: int | Decimal) -> Any:
    # NaN 和自身不相等，非有限值按文本比较
    if isinstance(value, Decimal) and not value.is_finite():
        return ("nonfinite", str(value))
    return value


def scalar_equal(base: Any, cand: Any, numeric_strings: bool) -> bool:
    """同类型标量是否相等。数字按数值比较（1 == 1.0）；numeric_strings 时 "12.50" == "12.5"。"""
    if json_type(base) == "number":
        return _number_key(base) == _number_key(cand)
    if numeric_strings and isinstance(base, str):
        b, c = decimal_text(base), decimal_text(cand)
        if b is not None and c is not None:
            return b == c
    return base == cand


def canon_scalar(value: Any, numeric_string: bool = False) -> Any:
    """标量的可 hash 规范形式（keyed 的 key、unordered 的元素）。数字按数值相等（Decimal 与 int 的 hash 一致）；
    numeric_string 时数字字符串也按数值，但和真正的数字仍然不等。"""
    kind = json_type(value)
    if kind == "number":
        return ("n", _number_key(value))
    if numeric_string and kind == "string":
        parsed = decimal_text(value)
        if parsed is not None:
            return ("ns", parsed)
    return (kind, value)
