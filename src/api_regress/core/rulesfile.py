"""树形规则文件：把和响应同形的 YAML 展开成路径规则（写法见 doc/user/rules-file.md）。

只做展开和结构检查，产出规则的原始数据（path + 属性）；属性值的校验交给 config 里的 `Rule` 模型，
这样和 suite 里写的规则完全一致，也避免 config ↔ rulesfile 循环引用。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from api_regress.core import paths
from api_regress.core.models import Loc

ATTR_PREFIX = "$"


@dataclass
class RawRule:
    """展开出的一条规则。loc 是规则第一个属性在文件里的位置（YAML key 序列），用来定位行号。"""

    path: str
    attrs: dict[str, Any]
    loc: Loc
    attr_locs: dict[str, Loc]


@dataclass
class Problem:
    loc: Loc
    where: str
    message: str


@dataclass
class Expansion:
    rules: list[RawRule] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    seen: dict[paths.Path, Loc] = field(default_factory=dict)


def expand(data: Any, attr_names: Iterable[str]) -> Expansion:
    """data 是规则文件解析后的顶层映射（空文件为 None）。attr_names 是规则属性名（不带 `$`）。"""
    walker = _Walker({ATTR_PREFIX + name: name for name in attr_names})
    if data is not None:
        walker.visit(data, (), ())
    return walker.out


def _parse_key(key: str) -> paths.Path:
    """key 是相对路径。以 `**` 结尾的 key（如 `"**"`）后面还会接子节点，单独解析时会被当成非法结尾，借一个占位段解析。"""
    if not (key == "**" or key.endswith(".**")):
        return paths.parse(key)
    try:
        return paths.parse(key + ".x")[:-1]
    except paths.PathSyntaxError as exc:
        raise paths.PathSyntaxError(str(exc).replace(repr(key + ".x"), repr(key))) from None


@dataclass
class _Node:
    """一个映射节点上找到的属性（属性名 → 值、位置）和子节点（完整路径、值、位置）。"""

    attrs: dict[str, Any] = field(default_factory=dict)
    attr_locs: dict[str, Loc] = field(default_factory=dict)
    children: list[tuple[paths.Path, Any, Loc]] = field(default_factory=list)


class _Walker:
    def __init__(self, attrs: dict[str, str]):
        self.attrs = attrs
        self.out = Expansion()

    def problem(self, loc: Loc, tokens: paths.Path, message: str) -> None:
        self.out.problems.append(Problem(loc, paths.format_path(tokens), message))

    def visit(self, node: Any, tokens: paths.Path, loc: Loc) -> None:
        """先展开本节点的属性、再展开子节点，所以规则按文件里从上到下（先父后子）的顺序排列。"""
        if node is None:
            return
        if not isinstance(node, dict):
            self.problem(loc, tokens, "值只能是映射（子字段或 $ 属性）或者留空，不能是字符串、数字、列表")
            return
        found = _Node()
        for key, value in node.items():
            self.classify(key, value, (*loc, str(key)), tokens, found)
        if found.attrs:
            self.add_rule(tokens, found)
        for child_tokens, value, child_loc in found.children:
            self.visit(value, child_tokens, child_loc)

    def classify(self, key: Any, value: Any, loc: Loc, tokens: paths.Path, found: _Node) -> None:
        if not isinstance(key, str):
            kind = type(key).__name__
            self.problem(loc, tokens, f"key {key!r} 被 YAML 解析成了 {kind}，字段名请加引号（如 \"200\":、\"on\":）")
        elif key in self.attrs:
            found.attrs[self.attrs[key]] = value
            found.attr_locs[self.attrs[key]] = loc
        elif key.startswith(ATTR_PREFIX):
            names = "、".join(self.attrs)
            self.problem(loc, tokens, f"未知的规则属性 {key!r}，只能是 {names}；字段名本身以 $ 开头时写成 '[\"{key}\"]':")
        else:
            try:
                found.children.append(((*tokens, *_parse_key(key)), value, loc))
            except paths.PathSyntaxError as exc:
                self.problem(loc, tokens, str(exc))

    def add_rule(self, tokens: paths.Path, found: _Node) -> None:
        text = paths.format_path(tokens)
        first = next(iter(found.attr_locs.values()))
        try:
            same = paths.parse(text) == tokens
        except paths.PathSyntaxError as exc:
            self.problem(first, tokens, f"展开后的路径不合法：{exc}")
            return
        if not same:
            self.problem(first, tokens, f"展开后的路径 {text!r} 不合法")
        elif tokens in self.out.seen:
            self.problem(first, tokens, f"路径 {text} 已经写过规则，同一个路径的规则只能写在一处")
        else:
            self.out.seen[tokens] = first
            self.out.rules.append(RawRule(text, found.attrs, first, found.attr_locs))
