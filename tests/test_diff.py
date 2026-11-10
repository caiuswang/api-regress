"""对比引擎的表驱动用例：输入两段 JSON 加规则，断言 finding 列表（spec §7.4 ~ §7.6）。"""

import json

import pytest

from api_regress.core.config import Suite
from api_regress.core.diff import diff_json
from api_regress.core.jsonload import parse_json
from api_regress.core.models import ABSENT, PathToken, RuleRef
from api_regress.core.paths import Index, Key
from api_regress.core.rules import RuleSet

TARGETS = {"baseline": {"base_url": "http://b"}, "candidate": {"base_url": "http://c"}}


def run(base, cand, rules=(), defaults_rules=(), compare="structure"):
    """base / cand 是 JSON 原文，保留小数位。"""
    suite = Suite.model_validate(
        {
            "version": 1,
            "targets": TARGETS,
            "defaults": {"compare": compare, "rules": list(defaults_rules)},
            "requests": [{"name": "r", "method": "GET", "path": "/", "rules": list(rules)}],
        }
    )
    ruleset = RuleSet.for_request(suite, suite.requests[0], 0)
    return diff_json(parse_json(base)[0], parse_json(cand)[0], ruleset, compare, request="r")


def brief(result):
    return [(f.type, f.level, f.path) for f in result.findings]


def codes(result):
    return [w.code for w in result.warnings]


CASES = [
    # --- AC-04 / AC-09：字段增删 ---
    ("field_removed", '{"a": 1, "b": 2}', '{"a": 1}', {}, [("field_removed", "breaking", "b")]),
    ("field_added", '{"a": 1}', '{"a": 1, "b": 2}', {}, [("field_added", "info", "b")]),
    (
        "renamed",
        '{"data": {"coupon_amount": "5.00"}}',
        '{"data": {"discount_amount": "5.00"}}',
        {},
        [("field_removed", "breaking", "data.coupon_amount"), ("field_added", "info", "data.discount_amount")],
    ),
    ("object_removed_only_top", '{"d": {"x": 1, "y": {"z": 2}}}', "{}", {}, [("field_removed", "breaking", "d")]),
    # --- AC-05：类型 ---
    ("number_to_string", '{"a": 1}', '{"a": "1"}', {}, [("type_changed", "breaking", "a")]),
    ("object_to_null_only_one", '{"data": {"x": 1, "y": [1]}}', '{"data": null}', {}, [("type_changed", "breaking", "data")]),
    ("array_to_null", '{"a": [1]}', '{"a": null}', {}, [("type_changed", "breaking", "a")]),
    ("null_to_object", '{"a": null}', '{"a": {}}', {}, [("type_changed", "breaking", "a")]),
    ("bool_vs_number", '{"a": true}', '{"a": 1}', {}, [("type_changed", "breaking", "a")]),
    ("object_to_array", '{"a": {}}', '{"a": []}', {}, [("type_changed", "breaking", "a")]),
    ("root_type", "[]", "{}", {}, [("type_changed", "breaking", "$")]),
    ("int_vs_decimal_same_type", '{"a": 1}', '{"a": 1.0}', {}, []),
    # --- AC-06：可空性 ---
    ("scalar_to_null", '{"a": "x"}', '{"a": null}', {}, [("nullability_changed", "suspicious", "a")]),
    ("null_to_scalar", '{"a": null}', '{"a": 3}', {}, [("nullability_changed", "info", "a")]),
    ("nullable_suppresses", '{"a": "x"}', '{"a": null}', {"rules": [{"path": "a", "nullable": True}]}, []),
    # D12：nullable 也放过对象/数组与 null 之间的变化，但不放过两个非 null 类型之间的变化
    ("nullable_allows_container_null", '{"a": {"x": 1}}', '{"a": null}', {"rules": [{"path": "a", "nullable": True}]}, []),
    ("nullable_allows_null_to_array", '{"a": null}', '{"a": [1]}', {"rules": [{"path": "a", "nullable": True}]}, []),
    (
        "nullable_keeps_other_type_changes",
        '{"a": {}}',
        '{"a": "x"}',
        {"rules": [{"path": "a", "nullable": True}]},
        [("type_changed", "breaking", "a")],
    ),
    # --- AC-07：decimal ---
    ("no_decimal_rule", '{"amount": 12.50}', '{"amount": 12.5}', {}, []),
    (
        "decimal_scale",
        '{"amount": 12.50}',
        '{"amount": 12.5}',
        {"defaults_rules": [{"path": "**.amount", "decimal": {}}]},
        [("scale_changed", "breaking", "amount")],
    ),
    ("decimal_int_vs_1dp", '{"p": 1}', '{"p": 1.0}', {"rules": [{"path": "p", "decimal": {}}]}, [("scale_changed", "breaking", "p")]),
    ("decimal_exponent_same", '{"p": 1.50E1}', '{"p": 15.0}', {"rules": [{"path": "p", "decimal": {}}]}, []),
    (
        "scale_level",
        '{"p": 1.10}',
        '{"p": 1.1}',
        {"rules": [{"path": "p", "decimal": {}, "scale_level": "info"}]},
        [("scale_changed", "info", "p")],
    ),
    ("decimal_string_default_off", '{"p": "1.10"}', '{"p": "1.1"}', {"rules": [{"path": "p", "decimal": {}}]}, []),
    (
        "decimal_allow_string",
        '{"p": "1.10"}',
        '{"p": "1.1"}',
        {"rules": [{"path": "p", "decimal": {"allow_string": True}}]},
        [("scale_changed", "breaking", "p")],
    ),
    (
        "allow_string_still_type_changed",
        '{"p": "1.10"}',
        '{"p": 1.10}',
        {"rules": [{"path": "p", "decimal": {"allow_string": True}}]},
        [("type_changed", "breaking", "p")],
    ),
    ("allow_string_non_numeric", '{"p": "abc"}', '{"p": "1.1"}', {"rules": [{"path": "p", "decimal": {"allow_string": True}}]}, []),
    (
        "decimal_and_value",
        '{"p": 12.50}',
        '{"p": 13.0}',
        {"rules": [{"path": "p", "decimal": {}, "on_value_change": "breaking"}]},
        [("scale_changed", "breaking", "p"), ("value_changed", "breaking", "p")],
    ),
    (
        "decimal_value_numerically_equal",
        '{"p": 12.50}',
        '{"p": 12.5}',
        {"rules": [{"path": "p", "decimal": {}, "on_value_change": "breaking"}]},
        [("scale_changed", "breaking", "p")],
    ),
    # --- AC-08：值 ---
    ("structure_ignores_values", '{"a": 1, "s": "x"}', '{"a": 2, "s": "y"}', {}, []),
    (
        "on_value_change",
        '{"a": 1, "s": "x"}',
        '{"a": 2, "s": "y"}',
        {"rules": [{"path": "a", "on_value_change": "suspicious"}]},
        [("value_changed", "suspicious", "a")],
    ),
    (
        "compare_values",
        '{"a": 1, "s": "x", "same": 1}',
        '{"a": 2, "s": "y", "same": 1.0}',
        {"compare": "values"},
        [("value_changed", "suspicious", "a"), ("value_changed", "suspicious", "s")],
    ),
    (
        "on_value_change_overrides_values_level",
        '{"a": 1}',
        '{"a": 2}',
        {"compare": "values", "rules": [{"path": "a", "on_value_change": "info"}]},
        [("value_changed", "info", "a")],
    ),
    # D10：标量变 null 且配了值比较时，另报 value_changed；nullable 只去掉 nullability_changed
    (
        "values_null_change",
        '{"a": 1}',
        '{"a": null}',
        {"compare": "values"},
        [("nullability_changed", "suspicious", "a"), ("value_changed", "suspicious", "a")],
    ),
    (
        "on_value_change_null",
        '{"amount": 12.50}',
        '{"amount": null}',
        {"rules": [{"path": "amount", "on_value_change": "breaking"}]},
        [("nullability_changed", "suspicious", "amount"), ("value_changed", "breaking", "amount")],
    ),
    (
        "on_value_change_null_nullable",
        '{"amount": null}',
        '{"amount": 1}',
        {"rules": [{"path": "amount", "on_value_change": "breaking", "nullable": True}]},
        [("value_changed", "breaking", "amount")],
    ),
    # --- AC-21：ignore ---
    ("ignore_subtree", '{"m": {"x": 1}}', '{"m": {"x": "1", "y": 2}}', {"rules": [{"path": "m", "ignore": True}]}, []),
    ("ignore_removed_field", '{"a": 1, "t": 1}', '{"a": 1}', {"defaults_rules": [{"path": "**.t", "ignore": True}]}, []),
    (
        "request_ignore_false_overrides",
        '{"data": {"t": 1}, "t": 1}',
        '{"data": {"t": "x"}, "t": "x"}',
        {"defaults_rules": [{"path": "**.t", "ignore": True}], "rules": [{"path": "data.t", "ignore": False}]},
        [("type_changed", "breaking", "data.t")],
    ),
    (
        "ignore_false_under_ignored_parent",
        '{"m": {"x": 1}}',
        '{"m": {"x": "1"}}',
        {"rules": [{"path": "m", "ignore": True}, {"path": "m.x", "ignore": False}]},
        [],
    ),
    ("ignore_root", '{"a": 1}', "[]", {"rules": [{"path": "$", "ignore": True}]}, []),
]


@pytest.mark.parametrize(("base", "cand", "opts", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_cases(base, cand, opts, expected):
    assert brief(run(base, cand, **opts)) == expected


def test_finding_fields():
    result = run('{"data": {"a": 1}}', '{"data": {}}', defaults_rules=[{"path": "**.a", "on_value_change": "info"}])
    (f,) = result.findings
    assert f.path == f.generalized_path == "data.a"
    assert f.path_tokens == [PathToken(kind="key", value="data"), PathToken(kind="key", value="a")]
    assert f.base_value == 1 and f.cand_value is ABSENT
    assert f.rule_refs == [RuleRef(scope="defaults", index=0)]
    dumped = f.model_dump(mode="json")
    assert dumped["cand_value"] == {"absent": True}


def test_null_value_is_not_absent():
    (f,) = run('{"a": null}', "{}").findings
    assert f.type == "field_removed" and f.base_value is None and f.cand_value is ABSENT


def test_decimal_value_serializes_with_scale():
    (f,) = run('{"p": 1.50E1}', '{"p": 15}', rules=[{"path": "p", "decimal": {}}]).findings
    assert f.model_dump(mode="json")["base_value"] == "15.0"


def test_nan_values_compare_equal():
    assert run('{"a": NaN}', '{"a": NaN}', compare="values").findings == []
    assert brief(run('{"a": NaN}', '{"a": 1}', compare="values")) == [("value_changed", "suspicious", "a")]


# ---------- 数组：positional（AC-16、AC-20） ----------

POSITIONAL = [{"path": "items", "array": {"mode": "positional"}}]


def test_positional_extra_elements():
    result = run('{"items": [1]}', '{"items": [1, 2, 3]}', rules=POSITIONAL)
    assert brief(result) == [
        ("array_length_changed", "info", "items"),
        ("element_added", "info", "items[1]"),
        ("element_added", "info", "items[2]"),
    ]
    assert result.findings[0].base_value == 1 and result.findings[0].cand_value == 3


def test_positional_missing_elements_and_empty():
    assert brief(run('{"items": [1, 2]}', '{"items": []}', rules=POSITIONAL)) == [
        ("array_length_changed", "suspicious", "items"),
        ("element_missing", "suspicious", "items[0]"),
        ("element_missing", "suspicious", "items[1]"),
    ]


def test_positional_shuffled():
    base = '{"items": [{"id": 1}, {"id": 2}]}'
    cand = '{"items": [{"id": 2}, {"id": 1}]}'
    assert run(base, cand, rules=POSITIONAL).findings == []
    assert brief(run(base, cand, rules=POSITIONAL, compare="values")) == [
        ("value_changed", "suspicious", "items[0].id"),
        ("value_changed", "suspicious", "items[1].id"),
    ]


def test_positional_index_rule():
    rules = [*POSITIONAL, {"path": "items[1]", "on_value_change": "breaking"}]
    assert brief(run('{"items": [1, 2]}', '{"items": [9, 9]}', rules=rules)) == [("value_changed", "breaking", "items[1]")]


def test_positional_root_array():
    rules = [{"path": "$", "array": {"mode": "positional"}}, {"path": "[*].p", "decimal": {}}]
    assert brief(run('[{"p": 1.10}]', '[{"p": 1.1}]', rules=rules)) == [("scale_changed", "breaking", "[0].p")]


# ---------- 数组：keyed（AC-16、AC-17） ----------

KEYED = [{"path": "items", "array": {"keys": ["sku_id"]}}]


def test_keyed_shuffled_no_finding():
    base = '{"items": [{"sku_id": "A", "n": 1}, {"sku_id": "B", "n": 2}]}'
    cand = '{"items": [{"sku_id": "B", "n": 2}, {"sku_id": "A", "n": 1}]}'
    assert run(base, cand, rules=KEYED, compare="values").findings == []


def test_keyed_pairs_and_presence():
    base = '{"items": [{"sku_id": "A12", "p": 1}, {"sku_id": "B", "p": 1}]}'
    cand = '{"items": [{"sku_id": "C", "p": 1}, {"sku_id": "A12", "p": "1"}]}'
    result = run(base, cand, rules=KEYED)
    assert brief(result) == [
        ("type_changed", "breaking", "items[sku_id=A12].p"),
        ("element_missing", "suspicious", "items[sku_id=B]"),
        ("element_added", "info", "items[sku_id=C]"),
    ]
    assert result.findings[0].generalized_path == "items[*].p"
    assert result.findings[0].path_tokens[1] == PathToken(kind="keyed", value="sku_id=A12")


def test_keyed_multi_keys_and_label_quoting():
    rules = [{"path": "items", "array": {"keys": ["id", "color"]}}]
    base = '{"items": [{"id": 1, "color": "red", "v": 1}, {"id": "1", "color": "红 色", "v": 1}]}'
    cand = '{"items": [{"id": 1, "color": "red", "v": "x"}, {"id": "1", "color": "红 色", "v": "x"}]}'
    assert [f.path for f in run(base, cand, rules=rules).findings] == [
        "items[id=1,color=red].v",
        'items[id="1",color="红 色"].v',
    ]


def test_keyed_int_and_decimal_keys_pair():
    base = '{"items": [{"id": 1, "v": 1}]}'
    cand = '{"items": [{"id": 1.0, "v": 1}]}'
    assert run(base, cand, rules=[{"path": "items", "array": {"keys": ["id"]}}]).findings == []


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ('{"items": [{"sku_id": "A"}, {"sku_id": "A"}]}', '{"items": [{"sku_id": "A"}]}'),
        ('{"items": [{"sku_id": "A"}]}', '{"items": [{"x": 1}]}'),
        ('{"items": [{"sku_id": "A"}]}', '{"items": [{"sku_id": {"a": 1}}]}'),
        ('{"items": [1, 2]}', '{"items": [1, 2]}'),
    ],
    ids=["duplicate", "missing", "non_scalar", "not_object"],
)
def test_keyed_degrades_to_schema(base, cand):
    result = run(base, cand, rules=KEYED)
    assert codes(result)[0] == "array_degraded_to_schema" and result.warnings[0].path == "items"
    assert all("[*]" in f.path or f.path == "items" for f in result.findings)


def test_keyed_degraded_still_reports_schema_changes():
    base = '{"items": [{"sku_id": "A", "p": 1}, {"sku_id": "A", "p": 2}]}'
    cand = '{"items": [{"sku_id": "A"}, {"sku_id": "A"}]}'
    result = run(base, cand, rules=KEYED)
    assert codes(result) == ["array_degraded_to_schema"]
    assert brief(result) == [("field_removed", "breaking", "items[*].p")]


def test_keyed_root_array():
    rules = [{"path": "$", "array": {"keys": ["id"]}}]
    assert [f.path for f in run('[{"id": 1, "a": 1}]', '[{"id": 1}]', rules=rules).findings] == ["[id=1].a"]


def test_keyed_ignore_element_field():
    rules = [*KEYED, {"path": "items[*].t", "ignore": True}]
    base = '{"items": [{"sku_id": "A", "t": 1}]}'
    cand = '{"items": [{"sku_id": "A", "t": "x"}]}'
    assert run(base, cand, rules=rules).findings == []


# ---------- 数组：unordered ----------

UNORDERED = [{"path": "tags", "array": {"mode": "unordered"}}]


def test_unordered_multiset():
    assert run('{"tags": ["a", "b", "a"]}', '{"tags": ["b", "a", "a"]}', rules=UNORDERED).findings == []
    assert brief(run('{"tags": ["a", "b", "a"]}', '{"tags": ["a", "b", "b"]}', rules=UNORDERED)) == [
        ("value_changed", "suspicious", "tags")
    ]


def test_unordered_level_and_objects():
    rules = [{"path": "tags", "array": {"mode": "unordered"}, "on_value_change": "breaking"}]
    base = '{"tags": [{"k": 1, "x": [1, 2]}, {"k": 2.0}]}'
    # 内层数组默认也按多重集合比较
    assert run(base, '{"tags": [{"k": 2}, {"x": [2, 1], "k": 1}]}', rules=rules).findings == []
    assert brief(run(base, '{"tags": [{"k": 2}, {"x": [1, 3], "k": 1}]}', rules=rules)) == [
        ("value_changed", "breaking", "tags")
    ]
    positional_inner = [*rules, {"path": "tags[*].x", "array": {"mode": "positional"}}]
    assert brief(run(base, '{"tags": [{"k": 2}, {"x": [2, 1], "k": 1}]}', rules=positional_inner)) == [
        ("value_changed", "breaking", "tags")
    ]


def test_unordered_skips_ignored_fields():
    rules = [*UNORDERED, {"path": "**.t", "ignore": True}]
    assert run('{"tags": [{"k": 1, "t": 1}]}', '{"tags": [{"k": 1, "t": 2}]}', rules=rules).findings == []


# ---------- 数组：schema（AC-18、AC-19、3b、3d） ----------


def test_schema_default_order_and_values_ignored():
    base = '{"list": [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}'
    cand = '{"list": [{"id": 3, "n": "z"}, {"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}'
    assert brief(run(base, cand)) == [("array_length_changed", "info", "list")]


def test_schema_field_removed_from_all_elements():
    base = '{"list": [{"id": 1, "x": 1}, {"id": 2}]}'
    result = run(base, '{"list": [{"id": 1}, {"id": 2}]}')
    assert brief(result) == [("field_removed", "breaking", "list[*].x")]
    assert result.findings[0].example_index == 0
    assert result.findings[0].base_value == ["number"]
    # 只要候选还有一个元素带这个字段，就不算删除
    assert run(base, '{"list": [{"id": 1}, {"id": 2, "x": 5}]}').findings == []


def test_schema_field_added_and_only_top():
    result = run('{"list": [{"id": 1}]}', '{"list": [{"id": 1}, {"id": 2, "ext": {"a": 1}}]}')
    assert brief(result) == [("array_length_changed", "info", "list"), ("field_added", "info", "list[*].ext")]
    assert result.findings[1].example_index == 1


def test_schema_candidate_empty():
    result = run('{"list": [{"id": 1}]}', '{"list": []}')
    assert brief(result) == [("array_length_changed", "suspicious", "list")]
    assert codes(result) == ["array_empty"]


def test_schema_both_empty_no_warning():
    """D15：两侧都为空时没有可比较的，也不提示。"""
    result = run('{"list": []}', '{"list": []}')
    assert result.findings == [] and result.warnings == []


def test_schema_type_sets():
    base = '{"list": [{"v": 1}, {"v": null}]}'
    result = run(base, '{"list": [{"v": 1}, {"v": "x"}]}')
    assert brief(result) == [("type_changed", "breaking", "list[*].v")]
    f = result.findings[0]
    assert (f.base_value, f.cand_value, f.example_index) == (["null", "number"], ["number", "string"], 1)
    # 候选只用了基线已有类型的子集，不报
    assert brief(run('{"list": [{"v": 1}, {"v": "x"}]}', '{"list": [{"v": 1}]}')) == [
        ("array_length_changed", "info", "list")
    ]


def test_schema_type_changed_stops_descent():
    result = run('{"list": [{"o": {"a": 1}}]}', '{"list": [{"o": "s"}]}')
    assert brief(result) == [("type_changed", "breaking", "list[*].o")]


def test_schema_element_type():
    assert brief(run("[1, 2]", '["a", "b"]')) == [("type_changed", "breaking", "[*]")]


@pytest.mark.parametrize(
    ("base", "cand", "expected"),
    [
        ('[{"v": 1}]', '[{"v": 1}, {"v": null}]', [("nullability_changed", "suspicious", "[*].v")]),
        ('[{"v": 1}, {"v": null}]', '[{"v": 1}, {"v": 2}]', [("nullability_changed", "info", "[*].v")]),
        ('[{"v": null}]', '[{"v": 2}]', [("nullability_changed", "info", "[*].v")]),
        ('[{"v": {}}]', '[{"v": {}}, {"v": null}]', [("type_changed", "breaking", "[*].v")]),
        ('[{"v": null}]', '[{"v": [1]}]', [("type_changed", "breaking", "[*].v")]),
        ('[{"v": {}}, {"v": null}]', '[{"v": {}}]', [("nullability_changed", "info", "[*].v")]),
        ('[{"v": null}]', '[{"v": null}]', []),
    ],
    ids=[
        "gained_null",
        "lost_null",
        "only_null_to_value",
        "object_gained_null",
        "null_to_array",
        "object_lost_null",
        "both_null",
    ],
)
def test_schema_nullability(base, cand, expected):
    result = [x for x in brief(run(base, cand)) if x[0] != "array_length_changed"]
    assert result == expected


def test_schema_nullable_rule():
    assert run('[{"v": 1}]', '[{"v": null}]', rules=[{"path": "[*].v", "nullable": True}]).findings == []


def test_schema_decimal_scales():
    """AC-19 / 3b：data.list[*].price 出现 1 位小数，附示例下标。"""
    base = '{"data": {"list": [{"price": 12.50}, {"price": 3.00}]}}'
    cand = '{"data": {"list": [{"price": 12.50}, {"price": 12.5}, {"price": 1.0}]}}'
    result = run(base, cand, defaults_rules=[{"path": "**.price", "decimal": {}}])
    scale = [f for f in result.findings if f.type == "scale_changed"]
    assert [(f.level, f.path, f.base_value, f.cand_value, f.example_index) for f in scale] == [
        ("breaking", "data.list[*].price", [2], [1, 2], 1)
    ]
    # 候选的小数位是基线的子集：不报
    sub = run('[{"p": 1.5}, {"p": 1.25}]', '[{"p": 1.25}]', rules=[{"path": "[*].p", "decimal": {}}])
    assert [f.type for f in sub.findings] == ["array_length_changed"]


def test_schema_nested_arrays_flatten():
    base = '{"l": [{"sub": [{"a": 1}, {"a": 2, "b": 1}]}, {"sub": []}]}'
    cand = '{"l": [{"sub": [{"a": 1}]}, {"sub": [{"a": 3}]}]}'
    result = run(base, cand)
    assert brief(result) == [("field_removed", "breaking", "l[*].sub[*].b")]


def test_schema_ignore_inside_elements():
    rules = [{"path": "list[*].x", "ignore": True}]
    assert run('{"list": [{"x": 1}]}', '{"list": [{"x": "s"}]}', rules=rules).findings == []


def test_schema_literal_index_rule_does_not_apply():
    rules = [{"path": "list[0].x", "ignore": True}]
    assert brief(run('{"list": [{"x": 1}]}', '{"list": [{"x": "s"}]}', rules=rules)) == [
        ("type_changed", "breaking", "list[*].x")
    ]


def test_schema_value_rules_warn():
    rules = [{"path": "list[*].x", "on_value_change": "breaking"}]
    result = run('{"list": [{"x": 1}]}', '{"list": [{"x": 2}]}', rules=rules)
    assert result.findings == [] and codes(result) == ["rule_ineffective_in_schema"]
    assert "list[*].x" in result.warnings[0].message


def test_schema_compare_values_warns_once_per_array():
    result = run('{"list": [{"x": 1}, {"x": 2}]}', '{"list": [{"x": 3}, {"x": 4}]}', compare="values")
    assert result.findings == [] and codes(result) == ["rule_ineffective_in_schema"]
    assert result.warnings[0].path == "list" and result.warnings[0].request == "r"


def test_compare_values_inside_keyed_and_positional():
    base = {"k": [{"id": 1, "v": 1}], "p": [1]}
    cand = {"k": [{"id": 1, "v": 2}], "p": [2]}
    rules = [{"path": "k", "array": {"keys": ["id"]}}, {"path": "p", "array": {"mode": "positional"}}]
    assert brief(run(json.dumps(base), json.dumps(cand), rules=rules, compare="values")) == [
        ("value_changed", "suspicious", "k[id=1].v"),
        ("value_changed", "suspicious", "p[0]"),
    ]


def test_array_length_levels():
    assert brief(run('{"a": [1]}', '{"a": [1, 2]}')) == [("array_length_changed", "info", "a")]
    assert brief(run('{"a": []}', '{"a": [1]}')) == [("array_length_changed", "suspicious", "a")]


# ---------- 组合穷举时发现的问题（D10–D15 与 review） ----------


@pytest.mark.parametrize(
    ("base", "cand", "rules", "expected"),
    [
        ('[{"o": {"a": 1}}]', '[{"o": null}]', [], [("type_changed", "breaking", "[*].o")]),
        ('[{"o": {"a": 1}}]', '[{"o": null}]', [{"path": "[*].o", "nullable": True}], []),
        ('[{"v": 1}, {"v": {"x": 1}}]', '[{"v": 1}, {"v": 2}]', [], []),
        ('[{"attrs": {"color": "r"}}]', '[{"attrs": []}]', [], [("type_changed", "breaking", "[*].attrs")]),
        ('[{"o": null}]', '[{"o": {"a": 1}}]', [{"path": "[*].o", "nullable": True}], []),
    ],
    ids=["object_to_null", "object_to_null_nullable", "narrowed_to_scalar", "object_to_array", "null_to_object_nullable"],
)
def test_schema_children_follow_parent(base, cand, rules, expected):
    """只报最高层节点：父节点在另一侧已经不是对象时，子字段不再报 field_removed / field_added。"""
    assert brief(run(base, cand, rules=rules)) == expected


@pytest.mark.parametrize(("base", "cand"), [('[{"sub": [{"a": 1}]}]', '[{"sub": []}]'), ('[{"sub": []}]', '[{"sub": [{"a": 1}]}]')])
def test_schema_inner_array_empty_on_one_side(base, cand):
    """D14：内层数组在一侧全部为空时只提示，不报字段增删。"""
    result = run(base, cand)
    assert result.findings == []
    assert [(w.code, w.path) for w in result.warnings] == [("array_empty", "[*].sub")]


def test_schema_inner_array_rule_warns():
    rules = [{"path": "l[*].sub", "array": {"keys": ["id"]}}]
    result = run('{"l": [{"sub": [{"id": 1}]}]}', '{"l": [{"sub": [{"id": 2}]}]}', rules=rules)
    assert result.findings == []
    assert codes(result) == ["rule_ineffective_in_schema"] and "keyed" in result.warnings[0].message


def test_schema_type_changed_suppresses_scale():
    rules = [{"path": "[*].p", "decimal": {"allow_string": True}}]
    assert brief(run('[{"p": 1.10}]', '[{"p": "1.1"}]', rules=rules)) == [("type_changed", "breaking", "[*].p")]


def test_warnings_deduplicated_by_generalized_path():
    """compare: values 时，keyed 元素里的 schema 数组只提示一次。"""
    items = [{"id": i, "sub": [{"a": i}]} for i in range(5)]
    result = run(json.dumps({"k": items}), json.dumps({"k": items}), rules=[{"path": "k", "array": {"keys": ["id"]}}], compare="values")
    assert [(w.code, w.path) for w in result.warnings] == [("rule_ineffective_in_schema", "k[*].sub")]


# ---------- unordered（D13） ----------


def test_unordered_reports_structure():
    result = run('{"t": [{"k": 1, "x": 1}]}', '{"t": [{"k": 1}]}', rules=UNORDERED_T)
    assert brief(result) == [("field_removed", "breaking", "t[*].x"), ("value_changed", "suspicious", "t")]


def test_unordered_decimal():
    rules = [*UNORDERED_T, {"path": "t[*]", "decimal": {}}]
    assert brief(run('{"t": [12.50, 3.00]}', '{"t": [3.0, 12.5]}', rules=rules)) == [("scale_changed", "breaking", "t[*]")]


def test_unordered_allow_string_numeric_equality():
    rules = [*UNORDERED_T, {"path": "t[*]", "decimal": {"allow_string": True}}]
    assert brief(run('{"t": ["12.50"]}', '{"t": ["12.5"]}', rules=rules)) == [("scale_changed", "breaking", "t[*]")]
    assert run('{"t": ["1.50E1"]}', '{"t": ["15.0"]}', rules=rules).findings == []


def test_unordered_element_value_rule_warns():
    rules = [*UNORDERED_T, {"path": "t[*].p", "on_value_change": "breaking"}]
    result = run('{"t": [{"p": 1}]}', '{"t": [{"p": 2}]}', rules=rules)
    assert brief(result) == [("value_changed", "suspicious", "t")]
    assert codes(result) == ["rule_ineffective_in_schema"]


UNORDERED_T = [{"path": "t", "array": {"mode": "unordered"}}]


# ---------- 数字字符串、类型、keyed 标签 ----------


@pytest.mark.parametrize(
    ("base", "cand", "expected"),
    [
        ('"1e9999999999999999999"', '"1.5"', [("value_changed", "breaking", "p")]),
        ('"007"', '"7"', [("value_changed", "breaking", "p")]),
        ('"１２.５０"', '"12.50"', [("value_changed", "breaking", "p")]),
        ('"12.50"', '"12.5"', [("scale_changed", "breaking", "p")]),
    ],
    ids=["huge_exponent", "leading_zero", "fullwidth", "numeric"],
)
def test_allow_string_requires_json_number_syntax(base, cand, expected):
    rules = [{"path": "p", "decimal": {"allow_string": True}, "on_value_change": "breaking"}]
    assert brief(run(f'{{"p": {base}}}', f'{{"p": {cand}}}', rules=rules)) == expected


def test_finding_value_types():
    (f,) = run('{"amount": 12.50}', '{"amount": "12.50"}').findings
    dumped = f.model_dump(mode="json")
    assert (dumped["base_value"], dumped["cand_value"]) == ("12.50", "12.50")
    assert (dumped["base_type"], dumped["cand_type"]) == ("number", "string")
    (g,) = run('{"x": {"absent": true}}', "{}").findings
    assert (g.base_type, g.cand_type) == ("object", "absent")
    (h,) = run('{"l": [1]}', '{"l": [1, 2]}').findings
    assert (h.base_type, h.cand_type) == (None, None)


def test_keyed_number_and_string_labels_differ():
    rules = [{"path": "items", "array": {"keys": ["id"]}}]
    base = '{"items": [{"id": 1.5, "v": 1}, {"id": "1.5", "v": 1}]}'
    cand = '{"items": [{"id": 1.5, "v": "x"}, {"id": "1.5", "v": "x"}]}'
    assert [f.path for f in run(base, cand, rules=rules).findings] == ["items[id=1.5].v", 'items[id="1.5"].v']


@pytest.mark.parametrize("written", ["1", "1.0", "1E0", "1.00"])
def test_keyed_number_label_is_stable(written):
    rules = [{"path": "items", "array": {"keys": ["id"]}}]
    (f,) = run(f'{{"items": [{{"id": {written}, "v": 1}}]}}', '{"items": [{"id": 1, "v": "x"}]}', rules=rules).findings
    assert f.path == "items[id=1].v"


def test_rule_cache_shared_by_elements():
    suite = Suite.model_validate(
        {
            "version": 1,
            "targets": TARGETS,
            "defaults": {"rules": [{"path": "**.p", "decimal": {}}]},
            "requests": [{"name": "r", "method": "GET", "path": "/", "rules": [
                {"path": "k", "array": {"keys": ["id"]}},
                {"path": "p", "array": {"mode": "positional"}},
                {"path": "p[3].x", "ignore": True},
            ]}],
        }
    )
    ruleset = RuleSet.for_request(suite, suite.requests[0], 0)
    body = {"k": [{"id": i, "p": 1} for i in range(300)], "p": [{"x": i} for i in range(300)]}
    diff_json(body, body, ruleset)
    assert len(ruleset._cache) < 20
    assert ruleset.resolve((Key("p"), Index(3), Key("x"))).ignore
    assert not ruleset.resolve((Key("p"), Index(4), Key("x"))).ignore
