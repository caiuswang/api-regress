"""路径语法：解析、格式化、匹配、泛化。

规则里写的是「模式」，由 Key / Index / AnyIndex / Deep 组成；
对比时遍历出来的是「具体路径」，由 Key / Index / AnyIndex（schema 数组）/ KeyedElem 组成。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Key:
    name: str


@dataclass(frozen=True, slots=True)
class Index:
    value: int


@dataclass(frozen=True, slots=True)
class AnyIndex:
    """`[*]`：任意数组元素。也用作 schema 数组在具体路径里的元素。"""


@dataclass(frozen=True, slots=True)
class Deep:
    """`**`：任意深度，包括 0 层。只出现在模式里。"""


@dataclass(frozen=True, slots=True)
class KeyedElem:
    """keyed 数组的元素，展示为 `[sku_id=A12]`。只出现在具体路径里，不能写进规则。"""

    label: str


Token = Key | Index | AnyIndex | Deep | KeyedElem
Path = tuple[Token, ...]

ANY = AnyIndex()
DEEP = Deep()
_ELEMENTS = (Index, AnyIndex, KeyedElem)

_IDENT = re.compile(r'[^.\[\]"\s*]+')
_BRACKET = re.compile(r'\[(?:(\*)|(\d+)|"((?:[^"\\]|\\.)*)")\]')
_ESCAPE = re.compile(r"\\(.)")


class PathSyntaxError(ValueError):
    pass


# ---------- 解析 ----------


ROOT = "$"


@lru_cache(maxsize=4096)
def parse(text: str) -> Path:
    """解析规则路径。单独的 `$` 表示根节点，返回空元组。"""
    if text == ROOT:
        return ()
    if not text:
        raise PathSyntaxError("路径不能为空；根节点写 $")
    if text.startswith("$"):
        raise PathSyntaxError(f'路径 {text!r} 开头不写 $（根节点只写一个 $）；字段名本身以 $ 开头时写成 ["$..."]')
    tokens: list[Token] = []
    pos = _parse_first(text, tokens)
    while pos < len(text):
        pos = _parse_next(text, pos, tokens)
    _check_deep(text, tokens)
    return tuple(tokens)


def _parse_first(text: str, tokens: list[Token]) -> int:
    if text.startswith("["):
        return _parse_bracket(text, 0, tokens)
    return _parse_segment(text, 0, tokens)


def _parse_next(text: str, pos: int, tokens: list[Token]) -> int:
    char = text[pos]
    if char == ".":
        return _parse_segment(text, pos + 1, tokens)
    if char == "[":
        return _parse_bracket(text, pos, tokens)
    raise PathSyntaxError(f"路径 {text!r} 第 {pos + 1} 个字符 {char!r} 处应为 '.' 或 '['")


def _parse_segment(text: str, pos: int, tokens: list[Token]) -> int:
    if text.startswith("**", pos):
        tokens.append(DEEP)
        return pos + 2
    m = _IDENT.match(text, pos)
    if not m:
        raise PathSyntaxError(
            f"路径 {text!r} 第 {pos + 1} 个字符处缺少字段名；"
            '字段名里有 . [ " * 或空白时请写成 ["..."]'
        )
    tokens.append(Key(m.group()))
    return m.end()


def _parse_bracket(text: str, pos: int, tokens: list[Token]) -> int:
    m = _BRACKET.match(text, pos)
    if not m:
        raise PathSyntaxError(
            f'路径 {text!r} 第 {pos + 1} 个字符处的方括号不合法，只支持 [*]、[数字]、["key"]'
        )
    star, digits, quoted = m.groups()
    if star:
        tokens.append(ANY)
    elif digits is not None:
        tokens.append(Index(int(digits)))
    else:
        tokens.append(Key(_ESCAPE.sub(r"\1", quoted)))
    return m.end()


def _check_deep(text: str, tokens: list[Token]) -> None:
    if tokens[-1] == DEEP:
        raise PathSyntaxError(f"路径 {text!r} 不能以 ** 结尾")
    for a, b in zip(tokens, tokens[1:]):
        if a == DEEP and b == DEEP:
            raise PathSyntaxError(f"路径 {text!r} 不能出现连续的 **")


# ---------- 格式化 ----------


def _quote(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'["{escaped}"]'


def _format_key(tok: Key, first: bool) -> str:
    if _IDENT.fullmatch(tok.name) and not tok.name.startswith("$"):
        return tok.name if first else f".{tok.name}"
    return _quote(tok.name)


_FORMATTERS = {
    Key: _format_key,
    Index: lambda tok, first: f"[{tok.value}]",
    AnyIndex: lambda tok, first: "[*]",
    KeyedElem: lambda tok, first: f"[{tok.label}]",
    Deep: lambda tok, first: "**" if first else ".**",
}


def format_path(tokens: Iterable[Token]) -> str:
    """把 token 序列格式化成路径字符串；根节点为 `$`。结果可以被 parse 解析回来（KeyedElem 除外）。"""
    tokens = tuple(tokens)
    if not tokens:
        return ROOT
    return "".join(_FORMATTERS[type(tok)](tok, i == 0) for i, tok in enumerate(tokens))


# ---------- 泛化与具体程度 ----------


def generalize(tokens: Iterable[Token]) -> Path:
    """把下标和 keyed 元素都换成 `[*]`，结果可以直接写进规则。"""
    return tuple(ANY if isinstance(tok, _ELEMENTS) else tok for tok in tokens)


def specificity(pattern: Sequence[Token]) -> tuple[int, int]:
    """(通配等级, 层级)，越大越具体：无通配 2 > 含 [*] 1 > 含 ** 0。"""
    if DEEP in pattern:
        level = 0
    elif ANY in pattern:
        level = 1
    else:
        level = 2
    depth = sum(1 for tok in pattern if tok != DEEP)
    return level, depth


# ---------- 匹配 ----------


def _consumes(pat: Token, tok: Token) -> bool:
    if isinstance(pat, Deep):
        return True
    if isinstance(pat, AnyIndex):
        return isinstance(tok, _ELEMENTS)
    return pat == tok


def _closure(pattern: Sequence[Token], states: set[int]) -> set[int]:
    out = set(states)
    stack = list(states)
    while stack:
        i = stack.pop()
        if i < len(pattern) and pattern[i] == DEEP and i + 1 not in out:
            out.add(i + 1)
            stack.append(i + 1)
    return out


def _advance(pattern: Sequence[Token], states: set[int], tok: Token) -> set[int]:
    nxt = set()
    for i in states:
        if i < len(pattern) and _consumes(pattern[i], tok):
            nxt.add(i if pattern[i] == DEEP else i + 1)
    return _closure(pattern, nxt)


def match(pattern: Sequence[Token], path: Sequence[Token]) -> bool:
    """模式是否匹配具体路径。

    `[*]` 匹配任意元素，`[n]` 只匹配 positional 下标 n，`**` 匹配 0 到多层。
    path 里出现 Deep 时（把另一个模式当作具体路径），只有模式里的 `**` 能匹配它，
    因此 `match(a, b)` 为真意味着 a 覆盖 b 能匹配的所有路径。
    """
    states = _closure(pattern, {0})
    for tok in path:
        states = _advance(pattern, states, tok)
        if not states:
            return False
    return len(pattern) in states


def covers(general: Sequence[Token], specific: Sequence[Token]) -> bool:
    """general 能匹配的路径集合是否包含 specific 的（保守判断：为真一定成立）。"""
    return match(general, specific)


def _compatible(a: Token, b: Token) -> bool:
    if isinstance(a, Deep) or isinstance(b, Deep):
        return True
    if isinstance(a, AnyIndex):
        return isinstance(b, _ELEMENTS)
    if isinstance(b, AnyIndex):
        return isinstance(a, _ELEMENTS)
    return a == b


def _pair_moves(a: Sequence[Token], b: Sequence[Token], i: int, j: int) -> list[tuple[int, int]]:
    moves = []
    if i < len(a) and a[i] == DEEP:
        moves.append((i + 1, j))
    if j < len(b) and b[j] == DEEP:
        moves.append((i, j + 1))
    if i < len(a) and j < len(b) and _compatible(a[i], b[j]):
        moves.append((i if a[i] == DEEP else i + 1, j if b[j] == DEEP else j + 1))
    return moves


def overlaps(a: Sequence[Token], b: Sequence[Token]) -> bool:
    """两个模式是否可能匹配同一条具体路径。"""
    seen = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        if i == len(a) and j == len(b):
            return True
        for move in _pair_moves(a, b, i, j):
            if move not in seen:
                seen.add(move)
                stack.append(move)
    return False
