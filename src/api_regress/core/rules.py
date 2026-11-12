"""规则合并：同一节点被多条规则匹配时，每个属性单独选出胜者（优先级见 doc/user/suite.md）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from api_regress.core import paths
from api_regress.core.config import RULE_ATTRS, ArrayRule, DecimalRule, Request, Rule, Suite
from api_regress.core.models import Level, Loc, RuleRef, WarningItem, format_loc

# 具体程度相同时：请求的 rules > rules_file > defaults
_SCOPE_RANK = {"defaults": 0, "rules_file": 1, "request": 2}


@dataclass(frozen=True, eq=False)
class CompiledRule:
    """按对象身份比较和 hash，可以放进 set（例如统计未命中的规则）。"""

    rule: Rule
    ref: RuleRef
    loc: Loc

    @property
    def tokens(self) -> paths.Path:
        return self.rule.tokens

    @property
    def rank(self) -> tuple[int, int, int, int]:
        """越大越优先：通配等级 → 层级 → 来源（请求的 rules > rules_file > defaults）→ 后写优先。"""
        level, depth = paths.specificity(self.tokens)
        return level, depth, _SCOPE_RANK[self.ref.scope], self.ref.index


@dataclass
class Resolved:
    """某个节点最终生效的规则属性，以及每个属性来自哪条规则。"""

    ignore: bool = False
    decimal: DecimalRule | None = None
    scale_level: Level | None = None
    on_value_change: Level | None = None
    array: ArrayRule | None = None
    nullable: bool = False
    sources: dict[str, RuleRef] = field(default_factory=dict)

    @property
    def refs(self) -> list[RuleRef]:
        return list(dict.fromkeys(self.sources.values()))


def compile_rules(suite: Suite, request: Request | None, request_index: int | None = None) -> list[CompiledRule]:
    out = [
        CompiledRule(rule, RuleRef(scope="defaults", index=i), ("defaults", "rules", i))
        for i, rule in enumerate(suite.defaults.rules)
    ]
    if request is None:
        return out
    req_loc: Loc = ("requests", request_index if request_index is not None else -1)
    out += [
        CompiledRule(fr.rule, RuleRef(scope="rules_file", index=i, file=fr.file, line=fr.line), (*req_loc, "rules_file", i))
        for i, fr in enumerate(request.file_rules)
    ]
    out += [
        CompiledRule(rule, RuleRef(scope="request", index=i), (*req_loc, "rules", i)) for i, rule in enumerate(request.rules)
    ]
    return out


def _winner(candidates: Iterable[CompiledRule], attr: str) -> CompiledRule | None:
    with_attr = [c for c in candidates if getattr(c.rule, attr) is not None]
    return max(with_attr, key=lambda c: c.rank, default=None)


class RuleSet:
    def __init__(self, rules: Sequence[CompiledRule]):
        self.rules = list(rules)
        self._cache: dict[tuple[paths.Token, ...], Resolved] = {}
        self._indices = {tok.value for r in self.rules for tok in r.tokens if isinstance(tok, paths.Index)}

    @classmethod
    def for_request(cls, suite: Suite, request: Request, request_index: int | None = None) -> RuleSet:
        return cls(compile_rules(suite, request, request_index))

    def matching(self, path: Sequence[paths.Token]) -> list[CompiledRule]:
        return [r for r in self.rules if paths.match(r.tokens, path)]

    def _cache_key(self, path: Sequence[paths.Token]) -> tuple[paths.Token, ...]:
        """匹配结果相同的路径共用缓存：keyed 元素只能被 [*] 和 ** 匹配，等同于 [*]；
        没有任何规则写到的下标同理。否则大数组的每个元素都要把所有规则重新匹配一遍。"""
        return tuple(
            paths.ANY
            if isinstance(tok, paths.KeyedElem) or (isinstance(tok, paths.Index) and tok.value not in self._indices)
            else tok
            for tok in path
        )

    def resolve(self, path: Sequence[paths.Token]) -> Resolved:
        key = self._cache_key(path)
        if key not in self._cache:
            self._cache[key] = self._resolve(key)
        return self._cache[key]

    def _resolve(self, path: tuple[paths.Token, ...]) -> Resolved:
        matched = self.matching(path)
        values: dict[str, Any] = {}
        sources: dict[str, RuleRef] = {}
        for attr in RULE_ATTRS:
            win = _winner(matched, attr)
            if win is not None:
                values[attr] = getattr(win.rule, attr)
                sources[attr] = win.ref
        return Resolved(**values, sources=sources)

    def is_pruned(self, path: Sequence[paths.Token]) -> bool:
        """节点自身或任一祖先（含根节点 `$`）最终 ignore 时为真：整棵子树不再对比。"""
        return any(self.resolve(path[:k]).ignore for k in range(len(path) + 1))


# ---------- 静态可判定的无效规则（validate 阶段） ----------


def _rescued(ignorer: CompiledRule, prefix: paths.Path, context: Sequence[CompiledRule]) -> bool:
    """是否有更优先的 ignore:false 可能作用到 prefix，使 ignorer 不一定生效。"""
    return any(
        r.rule.ignore is False and r.rank > ignorer.rank and paths.overlaps(r.tokens, prefix) for r in context
    )


def _ignorers(rule: CompiledRule, context: Sequence[CompiledRule]) -> list[CompiledRule]:
    # 全局规则只看全局的 ignore：请求级的 ignore 只在个别请求里生效，不能说全局规则无效
    return [
        r
        for r in context
        if r.rule.ignore and r is not rule and (rule.ref.scope != "defaults" or r.ref.scope == "defaults")
    ]


def _ignored_by(rule: CompiledRule, context: Sequence[CompiledRule]) -> tuple[CompiledRule, bool] | None:
    """返回 (生效的 ignore 规则, 是否就是规则所在节点本身)。

    检查所有严格前缀（含根节点）；规则自己没写 ignore 时，也检查它所在的节点本身。"""
    ignorers = _ignorers(rule, context)
    last = len(rule.tokens) + (1 if rule.rule.ignore is None else 0)
    for k in range(last):
        prefix = rule.tokens[:k]
        for ig in ignorers:
            if paths.covers(ig.tokens, prefix) and not _rescued(ig, prefix, context):
                return ig, k == len(rule.tokens)
    return None


def _array_prefixes(tokens: paths.Path, kind: type) -> Iterator[paths.Path]:
    for k, tok in enumerate(tokens):
        if isinstance(tok, kind):
            yield tokens[:k]


def _may_have_mode(array_path: paths.Path, context: Sequence[CompiledRule], modes: set[str]) -> bool:
    return any(
        r.rule.array is not None and r.rule.array.mode in modes and paths.overlaps(r.tokens, array_path)
        for r in context
    )


def _check_under_ignored(rule: CompiledRule, context: Sequence[CompiledRule]) -> str | None:
    found = _ignored_by(rule, context)
    if found is None:
        return None
    ig, same_node = found
    if same_node:
        return (
            f"规则 {rule.rule.path!r} 所在的节点已被 {ig.rule.path!r} ignore，这条规则不会生效；"
            "如果要对比它，在这条规则上加 ignore: false"
        )
    return f"规则 {rule.rule.path!r} 位于已被 ignore 的 {ig.rule.path!r} 之下，整棵子树不对比，这条规则不会生效"


def _check_index(rule: CompiledRule, context: Sequence[CompiledRule]) -> str | None:
    for arr in _array_prefixes(rule.tokens, paths.Index):
        if not _may_have_mode(arr, context, {"positional"}):
            where = paths.format_path(arr)
            return f"规则 {rule.rule.path!r} 用了字面下标，但数组 {where} 不是 positional 模式，规则不会生效"
    return None


def _unpaired_array(rule: CompiledRule, context: Sequence[CompiledRule], modes: set[str]) -> str | None:
    """规则路径经过的 `[*]` 数组里，第一个不可能是 modes 之一（只能是 schema 等不配对模式）的数组。"""
    for arr in _array_prefixes(rule.tokens, paths.AnyIndex):
        if not _may_have_mode(arr, context, modes):
            return paths.format_path(arr)
    return None


def _check_value_in_schema(rule: CompiledRule, context: Sequence[CompiledRule]) -> str | None:
    if rule.rule.on_value_change is None:
        return None
    where = _unpaired_array(rule, context, {"keyed", "positional"})
    if where is None:
        return None
    return (
        f"规则 {rule.rule.path!r} 配了 on_value_change，但数组 {where} 的元素没有逐个配对（schema 或 unordered 模式），"
        "元素内部不比较值；需要比较值请改用 keyed 模式"
    )


def _check_nested_array(rule: CompiledRule, context: Sequence[CompiledRule]) -> str | None:
    array = rule.rule.array
    if array is None or array.mode == "schema":
        return None
    where = _unpaired_array(rule, context, {"keyed", "positional", "unordered"})
    if where is None:
        return None
    return f"规则 {rule.rule.path!r}（mode: {array.mode}）所在的数组位于 schema 数组 {where} 的元素内部，外层没有配对，内层按 schema 比较"


_STATIC_CHECKS = (
    ("rule_under_ignored", _check_under_ignored),
    ("index_rule_ineffective", _check_index),
    ("rule_ineffective_in_schema", _check_value_in_schema),
    ("rule_ineffective_in_schema", _check_nested_array),
)


LineOf = Callable[[Loc], int | None]


def _warnings_for(
    targets: Sequence[CompiledRule], context: Sequence[CompiledRule], request: str | None, line_of: LineOf
) -> list[WarningItem]:
    out = []
    for rule in targets:
        for code, check in _STATIC_CHECKS:
            message = check(rule, context)
            if message:
                out.append(
                    WarningItem(code=code, message=message, request=request, path=rule.rule.path, **_where(rule, line_of))
                )
    return out


def _where(rule: CompiledRule, line_of: LineOf) -> dict[str, Any]:
    """warning 的位置：规则文件里的规则写文件、行号和节点路径，其余写 suite 里的位置和行号。"""
    if rule.ref.scope == "rules_file":
        return {"location": rule.rule.path, "line": rule.ref.line, "file": rule.ref.file}
    return {"location": format_loc(rule.loc), "line": line_of(rule.loc)}


def static_warnings(suite: Suite, line_of: LineOf = lambda loc: None) -> list[WarningItem]:
    """全局规则在「全局 + 所有请求级规则」的上下文里检查（任一请求能让它生效就不报），
    请求级规则在「全局 + 本请求」里检查。"""
    defaults = compile_rules(suite, None)
    request_rules = [compile_rules(suite, req, i)[len(defaults) :] for i, req in enumerate(suite.requests)]
    every_request = [r for rules in request_rules for r in rules]
    out = _warnings_for(defaults, defaults + every_request, None, line_of)
    for req, rules in zip(suite.requests, request_rules):
        out += _warnings_for(rules, defaults + rules, req.name, line_of)
    return out


# ---------- 本次未命中的规则（运行时） ----------


def _children(value: Any, path: paths.Path) -> Iterator[tuple[Any, paths.Path]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield child, (*path, paths.Key(key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield child, (*path, paths.Index(i))


def unmatched_rules(rules: Sequence[CompiledRule], bodies: Iterable[Any]) -> list[CompiledRule]:
    """在原始 body 里一个路径都没匹配到的规则，和对比模式、ignore 无关。

    数组元素用下标作为路径，`[*]` 和 `[n]` 都能匹配；命中一条就从待查集合移除，集合为空时提前结束。"""
    pending = list(rules)
    for body in bodies:
        stack: list[tuple[Any, paths.Path]] = [(body, ())]
        while stack and pending:
            value, path = stack.pop()
            pending = [r for r in pending if not paths.match(r.tokens, path)]
            stack.extend(_children(value, path))
    return pending
