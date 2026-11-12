"""字段树：把一条请求的若干个 body 合并成一棵树，标注每个节点最终生效的规则，渲染成和规则文件同形的文本。

- 所有数组一律按元素合并（和数组规则无关，数组规则只用来标注）：数组节点下只有一个元素节点 `[*]`。
- 树保留全部 key；`max_keys` 只在渲染时截断，未命中的规则按全部 key 计算。
- 两种渲染：展示版（类型、小数位、长度、示例值、生效的规则）和骨架（只有类型和小数位）。
  骨架用的格式化函数表里没有示例值，写出的规则文件可能提交到仓库，不能带出响应里的数据。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any

import yaml

from api_regress.core import paths
from api_regress.core.config import RULE_ATTRS, ArrayRule, DecimalRule
from api_regress.core.diff.values import json_type, scale
from api_regress.core.models import rule_where
from api_regress.core.rules import CompiledRule, Resolved, RuleSet, unmatched_rules

EXAMPLE_LIMIT = 40
COMMENT_COLUMN_MAX = 40
"""注释最多对齐到第几列；更长的 key 后面只空两格。"""


# ---------- 合并样本 ----------


@dataclass(slots=True, eq=False)
class Node:
    """一个泛化路径上合并出的信息。types 按第一次出现的顺序（dict 当有序集合用）。"""

    types: dict[str, None] = field(default_factory=dict)
    scales: set[int] = field(default_factory=set)
    lengths: set[int] = field(default_factory=set)
    example: str | None = None
    fields: dict[str, Node] = field(default_factory=dict)
    element: Node | None = None


def _example_text(value: Any) -> str:
    if isinstance(value, str):
        text = json.dumps(value[:EXAMPLE_LIMIT], ensure_ascii=False)
        return text + "…" if len(value) > EXAMPLE_LIMIT else text
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _add_object(node: Node, value: dict) -> None:
    fields = node.fields
    for key, child_value in value.items():
        child = fields.get(key)
        if child is None:
            child = fields[key] = Node()
        _add(child, child_value)


def _add_array(node: Node, value: list) -> None:
    node.lengths.add(len(value))
    if not value:
        return
    if node.element is None:
        node.element = Node()
    for item in value:
        _add(node.element, item)


def _add_scalar(node: Node, value: Any) -> None:
    if node.example is None:
        node.example = _example_text(value)


def _add_number(node: Node, value: int | Decimal) -> None:
    found = scale(value)
    if found is not None:
        node.scales.add(found)
    _add_scalar(node, value)


_ADDERS: dict[str, Callable[[Node, Any], None]] = {
    "object": _add_object,
    "array": _add_array,
    "number": _add_number,
    "string": _add_scalar,
    "boolean": _add_scalar,
}


def _add(node: Node, value: Any) -> None:
    kind = json_type(value)
    node.types.setdefault(kind, None)
    adder = _ADDERS.get(kind)
    if adder is not None:
        adder(node, value)


def build(values: Iterable[Any]) -> Node:
    """values 是 jsonload 解析出的 body（数字为 int / Decimal）。合并是线性的：每个 key 用 dict 归并。"""
    root = Node()
    for value in values:
        _add(root, value)
    return root


def _walk(node: Node, tokens: paths.Path = ()) -> Iterator[tuple[Node, paths.Path]]:
    stack = [(node, tokens)]
    while stack:
        current, path = stack.pop()
        yield current, path
        stack.extend((child, (*path, paths.Key(key))) for key, child in current.fields.items())
        if current.element is not None:
            stack.append((current.element, (*path, paths.ANY)))


def count_nodes(root: Node) -> int:
    return sum(1 for _ in _walk(root))


# ---------- 规则 ----------


def has_index(rule: CompiledRule) -> bool:
    return any(isinstance(tok, paths.Index) for tok in rule.tokens)


def unmatched(rules: Sequence[CompiledRule], root: Node, bodies: Iterable[Any]) -> list[CompiledRule]:
    """在这些样本里一个路径都没匹配到的规则，按规则原来的顺序。

    没写下标的规则在树上判断（树上的路径就是所有具体路径的泛化，结果和逐个 body 遍历相同，但节点少得多）；
    写了下标的规则要看具体下标，仍然遍历原始 body。"""
    missed = set(_unmatched_on_tree([r for r in rules if not has_index(r)], root))
    missed |= set(unmatched_rules([r for r in rules if has_index(r)], bodies))
    return [r for r in rules if r in missed]


def _unmatched_on_tree(pending: list[CompiledRule], root: Node) -> list[CompiledRule]:
    for _, path in _walk(root):
        if not pending:
            break
        pending = [r for r in pending if not paths.match(r.tokens, path)]
    return pending


def rule_text(rule: CompiledRule) -> str:
    """来源的写法和报告里「命中的规则」相同（`models.rule_where`），后面跟规则路径。"""
    ref = rule.ref
    return f"{rule_where(ref.scope, ref.index, ref.file, ref.line)} {rule.rule.path}"


def _flag(name: str) -> Callable[[bool], str]:
    return lambda value: name if value else f"{name}: false"


def _decimal_text(value: DecimalRule) -> str:
    return "decimal: {allow_string: true}" if value.allow_string else "decimal"


def _array_text(value: ArrayRule) -> str:
    keys = f" [{', '.join(value.keys)}]" if value.keys else ""
    return f"array: {value.mode}{keys}"


_ATTR_TEXT: dict[str, Callable[[Any], str]] = {
    "ignore": _flag("ignore"),
    "decimal": _decimal_text,
    "scale_level": lambda value: f"scale_level: {value}",
    "on_value_change": lambda value: f"on_value_change: {value}",
    "array": _array_text,
    "nullable": _flag("nullable"),
}
assert set(_ATTR_TEXT) == set(RULE_ATTRS)


# ---------- 遍历出要显示的行 ----------


@dataclass(frozen=True)
class Row:
    """渲染的一行：字段行（key 是 YAML 里的写法），或者截断提示（node 为 None）。"""

    depth: int
    key: str
    node: Node | None
    resolved: Resolved | None = None
    hidden: int = 0
    dropped: bool = False
    """骨架里 key 太多的对象：一个 key 都不列（hidden 是 key 的总数）。"""


@lru_cache(maxsize=65536)
def yaml_key(text: str) -> str:
    """相对路径写成 YAML 的 key：能原样当作字符串 key 时不加引号，否则加引号（`"[*]"`、`'["x.y"]'`、`"on"`）。"""
    try:
        plain = yaml.safe_load(f"{text}:") == {text: None}
    except yaml.YAMLError:
        plain = False
    quoted = json.dumps(text, ensure_ascii=False)
    if plain:
        return text
    if quoted == f'"{text}"':  # 不需要转义时用双引号：`"[*]"`
        return quoted
    return f"'{text}'" if "'" not in text and text.isprintable() else quoted


@dataclass
class _Walker:
    rules: RuleSet
    max_keys: int
    drop_truncated: bool = False
    """骨架用：key 超过 max_keys 的对象一个 key 都不写。这种 key 多半是 id、邮箱之类的数据，骨架要提交到仓库。"""

    def fields(self, node: Node, tokens: paths.Path, depth: int) -> Iterator[Row]:
        if self.drop_truncated and len(node.fields) > self.max_keys:
            yield Row(depth, "", None, hidden=len(node.fields), dropped=True)
            return
        items = list(node.fields.items())
        for key, child in items[: self.max_keys]:
            child_tokens = (*tokens, paths.Key(key))
            yield from self.entry(child, child_tokens, paths.format_path((paths.Key(key),)), depth)
        if len(items) > self.max_keys:
            yield Row(depth, "", None, hidden=len(items) - self.max_keys)

    def children(self, node: Node, tokens: paths.Path, name: str, depth: int) -> Iterator[Row]:
        """先列对象的 key（缩进一级），再在同一级列元素行 `name[*]`。根节点 name 为空。"""
        yield from self.fields(node, tokens, depth + 1 if name else depth)
        if node.element is not None:
            yield from self.entry(node.element, (*tokens, paths.ANY), f"{name}[*]", depth)

    def entry(self, node: Node, tokens: paths.Path, name: str, depth: int) -> Iterator[Row]:
        resolved = self.rules.resolve(tokens)
        yield Row(depth, yaml_key(name), node, resolved)
        if not resolved.ignore:
            yield from self.children(node, tokens, name, depth)


def _needs_root_line(root: Node, resolved: Resolved) -> bool:
    return list(root.types) != ["object"] or bool(resolved.sources)


# ---------- 渲染 ----------


def _sorted_set(values: Iterable[int]) -> str:
    return "{" + ", ".join(str(v) for v in sorted(values)) + "}"


def _types_part(node: Node) -> str | None:
    kinds = [t for t in node.types if t != "null"] + (["null"] if "null" in node.types else [])
    return "|".join(kinds) or None


def _scales_part(node: Node) -> str | None:
    """只有整数（小数位全是 0）时不列，免得每个整数字段都带一个 `小数位 {0}`。"""
    return f"小数位 {_sorted_set(node.scales)}" if node.scales - {0} else None


def _lengths_part(node: Node) -> str | None:
    return f"长度 {_sorted_set(node.lengths)}" if node.lengths else None


def _example_part(node: Node) -> str | None:
    return f"例 {node.example}" if node.example is not None else None


NodePart = Callable[[Node], str | None]
DISPLAY_PARTS: tuple[NodePart, ...] = (_types_part, _scales_part, _lengths_part, _example_part)
SKELETON_PARTS: tuple[NodePart, ...] = (_types_part, _scales_part)
"""骨架只有类型和小数位：没有示例值（可能是用户数据或 token），也没有长度。"""


def _node_text(node: Node, parts: Sequence[NodePart]) -> str:
    return "  ".join(text for part in parts if (text := part(node)) is not None)


class Annotator:
    """展示版的注释：类型等信息 + 生效的规则（属性 ← 来源 路径）。"""

    def __init__(self, rules: RuleSet):
        self.by_ref = {r.ref: r for r in rules.rules}

    def rules_text(self, resolved: Resolved) -> str:
        items = [
            f"{_ATTR_TEXT[attr](getattr(resolved, attr))} ← {rule_text(self.by_ref[ref])}"
            for attr in RULE_ATTRS
            if (ref := resolved.sources.get(attr)) is not None
        ]
        text = "; ".join(items)
        return text + "（子树不对比）" if resolved.ignore else text

    def __call__(self, node: Node, resolved: Resolved) -> str:
        text = _node_text(node, DISPLAY_PARTS)
        rules = self.rules_text(resolved)
        return f"{text}  | {rules}" if rules else text


def _skeleton_annotation(node: Node, resolved: Resolved) -> str:
    text = _node_text(node, SKELETON_PARTS)
    return f"{text}  （已 ignore，子树不展开）" if resolved.ignore else text


Annotate = Callable[[Node, Resolved], str]


def _hidden_line(row: Row) -> str:
    indent = "  " * row.depth
    if row.dropped:
        return f"{indent}# … 有 {row.hidden} 个 key，超过 --max-keys，一个都不列出（key 可能是 id、邮箱之类的数据，不写进规则文件）"
    return f"{indent}# … 还有 {row.hidden} 个 key 没有列出（key 可能是 id 之类的动态值）"


def _lines(rows: list[Row], annotate: Annotate) -> list[str]:
    """字段行的注释对齐到同一列（最多 COMMENT_COLUMN_MAX）。"""
    prefixes = [f"{'  ' * row.depth}{row.key}:" for row in rows]
    width = min(max((len(p) for p, row in zip(prefixes, rows) if row.node is not None), default=0), COMMENT_COLUMN_MAX)
    out = []
    for prefix, row in zip(prefixes, rows):
        if row.node is None:
            out.append(_hidden_line(row))
            continue
        assert row.resolved is not None
        note = annotate(row.node, row.resolved)
        out.append(f"{prefix.ljust(width)}  # {note}" if note else prefix)
    return out


def _render(root: Node, walker: _Walker, annotate: Annotate) -> list[str]:
    resolved = walker.rules.resolve(())
    head = [f"# 根节点 $：{annotate(root, resolved)}"] if _needs_root_line(root, resolved) else []
    if resolved.ignore:
        return head
    rows = list(walker.children(root, (), "", 0))
    return head + _lines(rows, annotate)


def render_tree(root: Node, rules: RuleSet, max_keys: int) -> list[str]:
    """展示版字段树（不含请求标题和末尾的规则列表）。"""
    return _render(root, _Walker(rules, max_keys), Annotator(rules))


def render_skeleton(root: Node, rules: RuleSet, max_keys: int) -> list[str]:
    """规则文件骨架的正文：只有字段和类型注释。rules 只用来判断哪些子树被 ignore、不展开。
    key 超过 max_keys 的对象一个 key 都不写（展示版只截断）。"""
    return _render(root, _Walker(rules, max_keys, drop_truncated=True), _skeleton_annotation)


def footer(rules: RuleSet, missed: Sequence[CompiledRule]) -> list[str]:
    """请求末尾的两个列表：写了字面下标的规则（树上按 [*] 展示，看不到它们）和未命中的规则。"""
    out = []
    for title, items in (("写了下标的规则（树上不展示）", [r for r in rules.rules if has_index(r)]), ("未命中的规则", missed)):
        if items:
            out.append(f"# {title}：")
            out += [f"#   {rule_text(r)}" for r in items]
    return out
