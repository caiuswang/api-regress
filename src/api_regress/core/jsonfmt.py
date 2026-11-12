"""把 `jsonload` 解析出的值写回 JSON 文本：数字按原文的小数位输出（`Decimal` 原样，不加引号）。

报告的 body 对照和落盘的 body 文件用同一种排版。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def _scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    if isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def compact_json(value: Any) -> str:
    """单行 JSON，数字按原文的小数位输出（对象、数组里嵌套的 `Decimal` 也一样，不会变成带引号的字符串）。"""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)}: {compact_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(compact_json(v) for v in value) + "]"
    return _scalar(value)


def pretty_json(value: Any) -> str:
    """缩进 2 格的 JSON，末尾不带换行。"""
    return "\n".join(_lines(value, ""))


def _lines(value: Any, indent: str) -> list[str]:
    inner = indent + "  "
    if isinstance(value, dict) and value:
        body = [_entry(f"{inner}{json.dumps(k, ensure_ascii=False)}: ", v, inner) for k, v in value.items()]
        return ["{", *_join(body), f"{indent}}}"]
    if isinstance(value, list) and value:
        body = [_entry(inner, v, inner) for v in value]
        return ["[", *_join(body), f"{indent}]"]
    return [_scalar(value)]


def _entry(prefix: str, value: Any, indent: str) -> list[str]:
    lines = _lines(value, indent)
    return [prefix + lines[0], *lines[1:]]


def _join(entries: list[list[str]]) -> list[str]:
    out: list[str] = []
    for i, lines in enumerate(entries):
        if i < len(entries) - 1:
            lines = [*lines[:-1], lines[-1] + ","]
        out += lines
    return out
