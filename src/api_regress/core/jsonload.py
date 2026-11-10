"""解析响应 body：判断是否为 JSON，数字用 Decimal，记录重复 key 和 NaN/Infinity（spec §7.2）。"""

from __future__ import annotations

import codecs
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from api_regress.core.models import Side, WarningCode, WarningItem

log = logging.getLogger(__name__)

_BOM = "\ufeff"
# 对比引擎是递归实现的，嵌套过深的 body 按解析失败处理，避免 RecursionError。只数对象和数组
MAX_DEPTH = 200
# Python 默认拒绝把超过 4300 位的数字串转成 int；更长的整数改用 Decimal（小数位仍为 0）
_MAX_INT_DIGITS = 4000
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}|[\ud800-\udfff]")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_CONSTANTS = {"NaN": Decimal("NaN"), "Infinity": Decimal("Infinity"), "-Infinity": Decimal("-Infinity")}


@dataclass
class LoadedBody:
    """`is_json` 为假时 `value` 无意义。"""

    is_json: bool
    value: Any = None
    warnings: list[WarningItem] = field(default_factory=list)


def media_type(content_type: str | None) -> str | None:
    """'Application/JSON; charset=utf-8' -> 'application/json'；没有 Content-Type 时返回 None。"""
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def is_json_media(content_type: str | None) -> bool | None:
    """声明为 JSON 返回 True，声明为其他类型返回 False，没有 Content-Type 返回 None。"""
    media = media_type(content_type)
    if not media:
        return None
    kind, _, subtype = media.partition("/")
    if not kind or not subtype:
        return False
    return media == "application/json" or (subtype.endswith("+json") and len(subtype) > len("+json"))


def charset_of(content_type: str | None) -> str:
    """取 charset 参数；没写或者不认识时用 UTF-8。"""
    for param in (content_type or "").split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"')
            try:
                info = codecs.lookup(charset)
            except LookupError:
                info = None
            # hex、base64 这类是 bytes→bytes 的编解码器，不能用来解码文本
            if info is not None and getattr(info, "_is_text_encoding", True):
                return info.name
            log.debug("不认识或不是文本编码的 charset %r，改用 utf-8", charset)
    return "utf-8"


class _Collector:
    """json.loads 的钩子：记录重复 key（取最后一个值）和 NaN/Infinity。"""

    def __init__(self) -> None:
        self.duplicates: list[str] = []
        self.constants: list[str] = []

    def pairs(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out and key not in self.duplicates:
                self.duplicates.append(key)
            out[key] = value
        return out

    def constant(self, name: str) -> Decimal:
        if name not in self.constants:
            self.constants.append(name)
        return _CONSTANTS[name]


def _decode(body: bytes | str, content_type: str | None) -> str:
    text = body if isinstance(body, str) else body.decode(charset_of(content_type))
    return text.removeprefix(_BOM)


class BodyRejected(ValueError):
    """消息可以直接放进 warning（不含 body 原文）。"""


class TooDeep(BodyRejected):
    pass


def _children(node: Any) -> Any:
    if isinstance(node, dict):
        return node.values()
    return node if isinstance(node, list) else ()


def _check_depth(value: Any, limit: int) -> None:
    """只数对象和数组：`[[1]]` 是 2 层。"""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            raise TooDeep(f"嵌套超过 {limit} 层")
        stack.extend((child, depth + 1) for child in _children(node) if isinstance(child, (dict, list)))


def _parse_int(text: str) -> int | Decimal:
    return int(text) if len(text) <= _MAX_INT_DIGITS else Decimal(text)


def _parse_float(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise BodyRejected("数字超出范围") from None


def _fix_text(text: str) -> str:
    """孤立的代理字符无法编码成 UTF-8，替换成 U+FFFD。"""
    return _LONE_SURROGATE.sub("\ufffd", text)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _fix_text(value)
    if isinstance(value, dict):
        return {_fix_text(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def parse_json(text: str) -> tuple[Any, _Collector]:
    """数字：整数保持 int，带小数点或指数的转成 Decimal，保留原文的小数位。"""
    collector = _Collector()
    value = json.loads(
        text,
        parse_float=_parse_float,
        parse_int=_parse_int,
        parse_constant=collector.constant,
        object_pairs_hook=collector.pairs,
    )
    _check_depth(value, MAX_DEPTH)
    if _SURROGATE_ESCAPE.search(text):
        value = _scrub(value)
    return value, collector


def _collector_warnings(collector: _Collector, where: str) -> list[tuple[WarningCode, str]]:
    out: list[tuple[WarningCode, str]] = []
    if collector.duplicates:
        keys = ", ".join(repr(k) for k in collector.duplicates)
        out.append(("duplicate_key", f"{where}有重复的 key：{keys}，取最后一个值"))
    if collector.constants:
        out.append(("nan_or_infinity", f"{where}出现了非标准数值：{', '.join(collector.constants)}"))
    return out


def load_body(
    body: bytes | str, content_type: str | None, *, request: str | None = None, side: Side | None = None
) -> LoadedBody:
    """Content-Type 是 application/json 或 *+json 时解析；没有 Content-Type 时尝试解析一次；
    声明为 JSON 却解析失败时按非 JSON 处理，并产生 json_parse_failed。"""
    declared = is_json_media(content_type)
    if declared is False:
        return LoadedBody(is_json=False)
    where = f"{side} 的响应" if side else "响应"
    try:
        value, collector = parse_json(_decode(body, content_type))
    except (ValueError, LookupError, ArithmeticError, RecursionError) as exc:
        if not declared:
            return LoadedBody(is_json=False)
        log.debug("%s 解析 JSON 失败：%s", where, type(exc).__name__)
        reason = str(exc) if isinstance(exc, BodyRejected) else type(exc).__name__
        message = f"{where}声明为 JSON，但解析失败（{reason}），按非 JSON 处理"
        return LoadedBody(is_json=False, warnings=[WarningItem(code="json_parse_failed", message=message, request=request)])
    warnings = [
        WarningItem(code=code, message=message, request=request)
        for code, message in _collector_warnings(collector, where)
    ]
    return LoadedBody(is_json=True, value=value, warnings=warnings)
