import pytest

from api_regress.core.config import Suite
from api_regress.core.paths import ANY, Index, Key, KeyedElem
from api_regress.core.rules import RuleRef, RuleSet, static_warnings

TARGETS = {"baseline": {"base_url": "http://b"}, "candidate": {"base_url": "http://c"}}


def make_suite(defaults_rules=(), request_rules=(), extra_requests=()):
    requests = [{"name": "r", "method": "GET", "path": "/", "rules": list(request_rules)}, *extra_requests]
    return Suite.model_validate(
        {"version": 1, "targets": TARGETS, "defaults": {"rules": list(defaults_rules)}, "requests": requests}
    )


def ruleset(defaults_rules=(), request_rules=()):
    suite = make_suite(defaults_rules, request_rules)
    return RuleSet.for_request(suite, suite.requests[0], 0)


def k(*names):
    return tuple(Key(n) if isinstance(n, str) else n for n in names)


# ---------- spec §6.3 的两个示例 ----------


def test_example_attributes_merge():
    rs = ruleset([{"path": "**.amount", "decimal": {}}], [{"path": "data.amount", "on_value_change": "breaking"}])
    resolved = rs.resolve(k("data", "amount"))
    assert resolved.decimal is not None
    assert resolved.on_value_change == "breaking"
    assert resolved.sources == {
        "decimal": RuleRef(scope="defaults", index=0),
        "on_value_change": RuleRef(scope="request", index=0),
    }
    assert len(resolved.refs) == 2


def test_example_request_ignore_false_overrides_global():
    rs = ruleset([{"path": "**.updated_at", "ignore": True}], [{"path": "data.updated_at", "ignore": False}])
    assert rs.resolve(k("data", "updated_at")).ignore is False
    assert rs.resolve(k("meta", "updated_at")).ignore is True
    assert rs.resolve(k("updated_at")).ignore is True  # ** 包括 0 层


# ---------- 优先级的每一项 ----------


def test_no_wildcard_beats_star():
    rs = ruleset([{"path": "items[*].x", "on_value_change": "info"}, {"path": "items[0].x", "on_value_change": "breaking"}])
    assert rs.resolve(k("items", Index(0), "x")).on_value_change == "breaking"
    assert rs.resolve(k("items", Index(1), "x")).on_value_change == "info"


def test_star_beats_deep():
    # ** 那条写在后面、层级也更深，仍然输给 [*]
    rs = ruleset([{"path": "items[*].x", "scale_level": "info"}, {"path": "**.items[*].x", "scale_level": "breaking"}])
    assert rs.resolve(k("items", ANY, "x")).scale_level == "info"


def test_deeper_wins_within_same_class():
    rs = ruleset([{"path": "**.b.x", "nullable": True}, {"path": "**.x", "nullable": False}])
    assert rs.resolve(k("a", "b", "x")).nullable is True
    # 不含 ** 的模式长度固定，能匹配同一节点的两条模式层级必然相同；层级只在 ** 类里起作用
    rs = ruleset([{"path": "**.b[*].x", "ignore": True}, {"path": "**.x", "ignore": False}])
    assert rs.resolve(k("a", Index(0), "b", Index(1), "x")).ignore is True


def test_request_beats_global_at_same_specificity():
    rs = ruleset([{"path": "data.x", "on_value_change": "info"}], [{"path": "data.x", "on_value_change": "suspicious"}])
    assert rs.resolve(k("data", "x")).on_value_change == "suspicious"


def test_global_more_specific_beats_request():
    rs = ruleset([{"path": "data.x", "on_value_change": "info"}], [{"path": "**.x", "on_value_change": "breaking"}])
    assert rs.resolve(k("data", "x")).on_value_change == "info"


def test_later_wins_on_full_tie():
    rs = ruleset([{"path": "data.x", "scale_level": "info"}, {"path": "data.x", "scale_level": "suspicious"}])
    resolved = rs.resolve(k("data", "x"))
    assert resolved.scale_level == "suspicious"
    assert resolved.sources["scale_level"] == RuleRef(scope="defaults", index=1)


def test_unset_attribute_does_not_override():
    # 更具体的规则没写 ignore，不影响 ** 的 ignore
    rs = ruleset([{"path": "**.x", "ignore": True}], [{"path": "data.x", "nullable": True}])
    resolved = rs.resolve(k("data", "x"))
    assert resolved.ignore is True and resolved.nullable is True


def test_defaults_when_nothing_matches():
    resolved = ruleset([{"path": "a", "ignore": True}]).resolve(k("b"))
    assert (resolved.ignore, resolved.nullable, resolved.decimal, resolved.sources) == (False, False, None, {})


def test_index_rule_does_not_match_schema_or_keyed_element():
    rs = ruleset([{"path": "items[0].x", "ignore": True}])
    assert rs.resolve(k("items", Index(0), "x")).ignore is True
    assert rs.resolve(k("items", ANY, "x")).ignore is False
    assert rs.resolve(k("items", KeyedElem("id=0"), "x")).ignore is False


# ---------- ignore 剪枝 ----------


def test_ignore_prunes_subtree():
    rs = ruleset([{"path": "data.meta", "ignore": True}, {"path": "data.meta.x", "ignore": False}])
    assert rs.is_pruned(k("data", "meta"))
    assert rs.is_pruned(k("data", "meta", "x"))
    assert rs.is_pruned(k("data", "meta", "y", Index(0)))
    assert not rs.is_pruned(k("data", "other"))
    assert not rs.is_pruned(())


# ---------- 静态 warning ----------


def codes(suite):
    return [(w.code, w.path, w.request) for w in static_warnings(suite)]


def test_child_ignore_false_under_ignored_parent_warns():
    suite = make_suite([{"path": "data.meta", "ignore": True}], [{"path": "data.meta.x", "ignore": False}])
    assert codes(suite) == [("rule_under_ignored", "data.meta.x", "r")]


def test_rule_under_deep_ignore_warns():
    suite = make_suite([{"path": "**.debug", "ignore": True}], [{"path": "data.debug.items", "array": {}}])
    assert codes(suite) == [("rule_under_ignored", "data.debug.items", "r")]


def test_rescued_parent_does_not_warn():
    # 请求级把 data.meta 救回来了，data.meta.x 的规则可以生效
    suite = make_suite(
        [{"path": "**.meta", "ignore": True}],
        [{"path": "data.meta", "ignore": False}, {"path": "data.meta.x", "nullable": True}],
    )
    assert codes(suite) == []


def test_global_rule_rescued_in_some_request_does_not_warn():
    suite = make_suite(
        [{"path": "**.meta", "ignore": True}, {"path": "data.meta.x", "nullable": True}],
        [{"path": "data.meta", "ignore": False}],
    )
    assert codes(suite) == []


def test_global_rule_under_global_ignore_warns_once():
    suite = make_suite(
        [{"path": "data", "ignore": True}, {"path": "data.x", "decimal": {}}],
        extra_requests=[{"name": "r2", "method": "GET", "path": "/"}],
    )
    assert codes(suite) == [("rule_under_ignored", "data.x", None)]


def test_same_path_ignore_is_not_under():
    suite = make_suite([{"path": "**.x", "ignore": True}], [{"path": "data.x", "ignore": False}])
    assert codes(suite) == []


def test_index_rule_without_positional_warns():
    suite = make_suite(request_rules=[{"path": "data.items[0].x", "ignore": True}])
    assert codes(suite) == [("index_rule_ineffective", "data.items[0].x", "r")]
    suite = make_suite(
        request_rules=[{"path": "data.items", "array": {"keys": ["id"]}}, {"path": "data.items[0].x", "ignore": True}]
    )
    assert codes(suite) == [("index_rule_ineffective", "data.items[0].x", "r")]


def test_index_rule_with_positional_ok():
    suite = make_suite(
        [{"path": "**.items", "array": {"mode": "positional"}}], [{"path": "data.items[0].x", "ignore": True}]
    )
    assert codes(suite) == []


def test_value_rule_in_schema_array_warns():
    suite = make_suite(request_rules=[{"path": "data.list[*].price", "on_value_change": "breaking"}])
    assert codes(suite) == [("rule_ineffective_in_schema", "data.list[*].price", "r")]


def test_value_rule_in_keyed_array_ok():
    suite = make_suite(
        request_rules=[
            {"path": "data.items", "array": {"keys": ["sku_id"]}},
            {"path": "data.items[*].price", "on_value_change": "breaking"},
        ]
    )
    assert codes(suite) == []


def test_value_rule_in_unordered_array_warns():
    """D13：unordered 的元素也没有逐个配对，元素字段上的 on_value_change 不生效。"""
    suite = make_suite(
        request_rules=[
            {"path": "data.tags", "array": {"mode": "unordered"}},
            {"path": "data.tags[*].price", "on_value_change": "breaking"},
        ]
    )
    assert codes(suite) == [("rule_ineffective_in_schema", "data.tags[*].price", "r")]


@pytest.mark.parametrize(
    ("outer", "warned"),
    [(None, True), ({"mode": "unordered"}, False), ({"keys": ["id"]}, False), ({"mode": "positional"}, False)],
    ids=["schema", "unordered", "keyed", "positional"],
)
def test_nested_array_rule_in_schema_warns(outer, warned):
    """D14：schema 数组元素里的数组，非 schema 的 array 规则不生效。"""
    rules = [{"path": "data.list[*].sub", "array": {"keys": ["id"]}}]
    if outer:
        rules.insert(0, {"path": "data.list", "array": outer})
    expected = [("rule_ineffective_in_schema", "data.list[*].sub", "r")] if warned else []
    assert codes(make_suite(request_rules=rules)) == expected


def test_decimal_rule_in_schema_array_ok():
    suite = make_suite(request_rules=[{"path": "data.list[*].price", "decimal": {}}])
    assert codes(suite) == []


def test_warning_carries_location_and_line():
    suite = make_suite([{"path": "data", "ignore": True}], [{"path": "data.x", "decimal": {}}])
    [w] = static_warnings(suite, line_of=lambda loc: 42 if loc == ("requests", 0, "rules", 0) else None)
    assert (w.location, w.line) == ("requests[0].rules[0]", 42)


@pytest.mark.parametrize("path", ["**.updated_at", "data.items[*].price", 'meta["x.y"]'])
def test_spec_paths_have_no_static_warning(path):
    assert codes(make_suite([{"path": path, "ignore": True}])) == []


# ---------- review 修复：根节点、缓存、hash、全局规则的上下文、节点本身被 ignore ----------


def test_root_ignore_prunes_everything():
    rs = ruleset([{"path": "$", "ignore": True}])
    assert rs.is_pruned(())
    assert rs.is_pruned(k("a", "b"))


def test_root_array_rule():
    rs = ruleset(request_rules=[{"path": "$", "array": {"keys": ["id"]}}])
    assert rs.resolve(()).array.mode == "keyed"
    suite = make_suite(request_rules=[{"path": "$", "array": {"keys": ["id"]}}, {"path": "[*].price", "on_value_change": "breaking"}])
    assert codes(suite) == []


def test_root_array_warning_names_dollar():
    [w] = static_warnings(make_suite(request_rules=[{"path": "[*].price", "on_value_change": "breaking"}]))
    assert "数组 $ " in w.message


def test_resolve_is_cached():
    rs = ruleset([{"path": "**.x", "ignore": True}])
    assert rs.resolve(k("a", "x")) is rs.resolve(k("a", "x"))


def test_compiled_rules_are_hashable():
    rs = ruleset([{"path": "a", "ignore": True}, {"path": "a", "ignore": True}])
    assert len(set(rs.rules)) == 2


def test_global_value_rule_with_request_keyed_array_ok():
    """全局写 [*].price 的 on_value_change，请求级把数组配成 keyed：不应误报。"""
    suite = make_suite(
        [{"path": "data.items[*].price", "on_value_change": "breaking"}],
        [{"path": "data.items", "array": {"keys": ["sku_id"]}}],
    )
    assert codes(suite) == []


def test_global_index_rule_with_request_positional_ok():
    suite = make_suite(
        [{"path": "data.steps[0].name", "nullable": True}],
        [{"path": "data.steps", "array": {"mode": "positional"}}],
    )
    assert codes(suite) == []


def test_request_ignore_does_not_flag_global_rule():
    suite = make_suite([{"path": "data.x.y", "decimal": {}}], [{"path": "data.x", "ignore": True}])
    assert codes(suite) == []


def test_rule_on_ignored_node_itself_warns():
    """D9：全局 ** 忽略，请求级在同一节点配了值比较却没写 ignore: false。"""
    suite = make_suite([{"path": "**.updated_at", "ignore": True}], [{"path": "data.updated_at", "on_value_change": "breaking"}])
    [w] = static_warnings(suite)
    assert (w.code, w.path, w.request) == ("rule_under_ignored", "data.updated_at", "r")
    assert "ignore: false" in w.message


def test_rule_on_ignored_node_with_own_ignore_false_ok():
    suite = make_suite(
        [{"path": "**.updated_at", "ignore": True}],
        [{"path": "data.updated_at", "ignore": False, "on_value_change": "breaking"}],
    )
    assert codes(suite) == []
