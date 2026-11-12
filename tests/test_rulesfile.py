"""树形规则文件：展开、报错定位、优先级、和 rules 写法的等价性、result.json 与报告里的来源。"""

from __future__ import annotations

import json
import textwrap

import pytest

from api_regress.core import service
from api_regress.core.config import ConfigError, Rule, load_suite, parse_rules_file
from api_regress.core.paths import ANY, Key
from api_regress.core.rules import RuleSet
from api_regress.report import common, reporters
from tests.mock_api import Server, j, only, req, run, suite_text

EXAMPLE = """\
code:
data:
  amount: {$decimal: {}, $on_value_change: breaking}
  coupon: {$nullable: true}
  items: {$array: {keys: [sku_id]}}
  items[*]:
    price: {$decimal: {}}
  meta["x.y"]: {$ignore: true}
"**.updated_at": {$ignore: true}
"""

EXAMPLE_RULES = [
    {"path": "data.amount", "decimal": {}, "on_value_change": "breaking"},
    {"path": "data.coupon", "nullable": True},
    {"path": "data.items", "array": {"keys": ["sku_id"]}},
    {"path": "data.items[*].price", "decimal": {}},
    {"path": 'data.meta["x.y"]', "ignore": True},
    {"path": "**.updated_at", "ignore": True},
]


def _plain(rules):
    return [r.rule.model_dump(exclude_none=True) for r in rules]


def _normalized(rules):
    return [Rule.model_validate(r).model_dump(exclude_none=True) for r in rules]


# ---------- 展开 ----------


def test_example_expands_to_equivalent_rules():
    rules = parse_rules_file(EXAMPLE, "r.yaml")
    assert _plain(rules) == _normalized(EXAMPLE_RULES)
    assert [r.line for r in rules] == [3, 4, 5, 7, 8, 9]
    assert {r.file for r in rules} == {"r.yaml"}


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("", []),
        ("# 只有注释\n", []),
        ("a:\nb:\n  c:\n", []),  # 只列字段不产生规则
        ("$array: {keys: [id]}\n'[*]':\n  price: {$decimal: {}}\n", ["$", "[*].price"]),
        ("data.items[*].price: {$decimal: {}}\n", ["data.items[*].price"]),
        ('"**":\n  updated_at: {$ignore: true}\n', ["**.updated_at"]),
        ('a:\n  "**":\n    b: {$ignore: true}\n', ["a.**.b"]),
        ('a: {$ignore: true}\na.b: {$ignore: false}\n', ["a", "a.b"]),
        ("meta:\n  '[\"$x\"]': {$ignore: true}\n", ['meta["$x"]']),
    ],
)
def test_expand_paths(text, paths):
    assert [r.rule.path for r in parse_rules_file(text, "r.yaml")] == paths


def test_parent_rule_comes_before_children():
    text = "a:\n  b: {$ignore: true}\n  $nullable: true\n"
    assert [r.rule.path for r in parse_rules_file(text, "r.yaml")] == ["a", "a.b"]


# ---------- 报错 ----------


@pytest.mark.parametrize(
    ("text", "line", "where", "message"),
    [
        ("a:\n  $foo: 1\n", 2, "a", "未知的规则属性 '$foo'"),
        ("a:\n  b: 3\n", 2, "a.b", "值只能是映射"),
        ("a:\n  b: [1]\n", 2, "a.b", "值只能是映射"),
        ("a:\n  b..c: {}\n", 2, "a", "缺少字段名"),
        ('"**": {$ignore: true}\n', 1, "**", "展开后的路径不合法"),
        ('"**":\n  "**.x": {$ignore: true}\n', 2, "**.**.x", "连续的 **"),
        ("a:\n  $array: {mode: keyed}\n", 2, "a $array", "keyed 模式必须写 keys"),
        ("a:\n  $on_value_change: fatal\n", 2, "a $on_value_change", ""),
        ("data:\n  amount: {$decimal: {}}\ndata.amount: {$ignore: true}\n", 3, "data.amount", "已经写过规则"),
        ("a:\n  200: {}\n", 2, "a", "被 YAML 解析成了 int"),
        ("a:\n  on: {}\n", 1, "a", "被 YAML 解析成了 bool"),  # 布尔 key 还原不出原文，行号退到父节点
        ("a:\n  b: {}\n  b: {}\n", 3, "a.b", "重复的字段"),
        ("- a\n", 1, "", "规则文件顶层必须是一个映射"),
    ],
)
def test_errors_point_into_rules_file(text, line, where, message):
    with pytest.raises(ConfigError) as exc:
        parse_rules_file(text, "rules/r.yaml")
    issue = exc.value.issues[0]
    assert (issue.source, issue.line, issue.location) == ("rules/r.yaml", line, where)
    assert message in issue.message


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as exc:
        parse_rules_file("a:\n  $foo: 1\nb: 2\n", "r.yaml")
    assert [i.line for i in exc.value.issues] == [2, 3]


def _suite_with_file(tmp_path, rules_file="rules/r.yaml", rules_text=None, extra=""):
    if rules_text is not None:
        (tmp_path / "rules").mkdir(exist_ok=True)
        (tmp_path / "rules" / "r.yaml").write_text(rules_text)
    text = suite_text(req("a", extra=f"    rules_file: {rules_file}\n{extra}"))
    path = tmp_path / "suite.yaml"
    path.write_text(text)
    return path


def test_missing_rules_file_reported_at_suite_line(tmp_path):
    path = _suite_with_file(tmp_path, "rules/nope.yaml")
    with pytest.raises(ConfigError) as exc:
        load_suite(path)
    issue = exc.value.issues[0]
    assert issue.source == str(path) and issue.location == "requests[0].rules_file" and issue.line == 13
    assert "无法读取规则文件" in issue.message


def test_rules_file_relative_to_suite_dir(tmp_path, monkeypatch):
    path = _suite_with_file(tmp_path, rules_text="a: {$ignore: true}\n")
    monkeypatch.chdir("/")
    loaded = load_suite(path)
    [rule] = loaded.suite.requests[0].file_rules
    assert rule.rule.path == "a" and rule.file == str(tmp_path / "rules" / "r.yaml") and rule.line == 1


def test_rules_file_error_uses_file_location(tmp_path):
    path = _suite_with_file(tmp_path, rules_text="a:\n  $foo: 1\n")
    report = service.validate_suite_sync(path)
    assert report.exit_code == 2
    [issue] = report.errors
    assert (issue.source, issue.line) == (str(tmp_path / "rules" / "r.yaml"), 2)


def test_same_file_loaded_once_for_many_requests(tmp_path):
    (tmp_path / "r.yaml").write_text("a: {$ignore: true}\n")
    text = suite_text(req("a", extra="    rules_file: r.yaml\n") + req("b", extra="    rules_file: r.yaml\n"))
    (tmp_path / "suite.yaml").write_text(text)
    reqs = load_suite(tmp_path / "suite.yaml").suite.requests
    assert reqs[0].file_rules is reqs[1].file_rules and len(reqs[0].file_rules) == 1


# ---------- 优先级 ----------


def _ruleset(tmp_path, defaults_rules, file_text, request_rules):
    (tmp_path / "r.yaml").write_text(file_text)
    extra = "    rules_file: r.yaml\n"
    if request_rules:
        extra += "    rules:\n" + "".join(f"      - {json.dumps(r)}\n" for r in request_rules)
    defaults = ("  rules:\n" + "".join(f"    - {json.dumps(r)}\n" for r in defaults_rules)) if defaults_rules else ""
    (tmp_path / "suite.yaml").write_text(suite_text(req("a", extra=extra), defaults=defaults))
    suite = load_suite(tmp_path / "suite.yaml").suite
    return RuleSet.for_request(suite, suite.requests[0], 0)


def test_same_specificity_request_beats_file_beats_defaults(tmp_path):
    rs = _ruleset(
        tmp_path,
        [{"path": "a.x", "on_value_change": "info", "scale_level": "info", "nullable": False}],
        "a:\n  x: {$on_value_change: suspicious, $scale_level: suspicious}\n",
        [{"path": "a.x", "on_value_change": "breaking"}],
    )
    resolved = rs.resolve((Key("a"), Key("x")))
    assert resolved.on_value_change == "breaking"
    assert resolved.scale_level == "suspicious" and resolved.sources["scale_level"].scope == "rules_file"
    assert resolved.sources["nullable"].scope == "defaults"


def test_more_specific_wins_regardless_of_source(tmp_path):
    rs = _ruleset(
        tmp_path,
        [{"path": "items[*].x", "on_value_change": "info"}],
        '"**.x": {$on_value_change: breaking}\n',
        [{"path": "**.x", "scale_level": "info"}],
    )
    assert rs.resolve((Key("items"), ANY, Key("x"))).on_value_change == "info"


def test_later_in_file_wins(tmp_path):
    text = '"**.b.x": {$on_value_change: info}\n"a.**.x": {$on_value_change: breaking}\n'
    rs = _ruleset(tmp_path, [], text, [])
    assert rs.resolve((Key("a"), Key("b"), Key("x"))).on_value_change == "breaking"


# ---------- 和 rules 写法等价 ----------

BASE_BODY = {
    "code": 0,
    "updated_at": "1",
    "data": {
        "amount": 12.5,
        "coupon": {"c": 1},
        "items": [{"sku_id": "A", "price": 1.5}, {"sku_id": "B", "price": 2.5}],
        "meta": {"x.y": 1},
        "updated_at": "1",
    },
}
CAND_BODY = {
    "code": 0,
    "updated_at": "2",
    "data": {
        "amount": 13.25,
        "coupon": None,
        "items": [{"sku_id": "B", "price": 2.75}, {"sku_id": "C", "price": 1}],
        "meta": {"x.y": "changed"},
        "updated_at": "2",
    },
}


def _bodies():
    # json.dumps 会把 12.50 写成 12.5，这里用原文控制小数位
    base = json.dumps(BASE_BODY)
    cand = json.dumps(CAND_BODY).replace("2.75", "2.750")
    return Server().on("base", "/x", j(base)).on("cand", "/x", j(cand))


def _finding_view(report):
    r = only(report)
    findings = [(f.type, f.level, f.path, f.generalized_path) for f in r.findings]
    return findings, [(w.code, w.message) for w in r.warnings]


def test_rules_file_equivalent_to_rules(tmp_path):
    inline = "    rules:\n" + "".join(f"      - {json.dumps(r)}\n" for r in EXAMPLE_RULES)
    by_rules = run(tmp_path / "rules", suite_text(req("a", extra=inline)), _bodies())
    (tmp_path / "file").mkdir()
    (tmp_path / "file" / "r.yaml").write_text(EXAMPLE)
    by_file = run(tmp_path / "file", suite_text(req("a", extra="    rules_file: r.yaml\n")), _bodies())
    assert _finding_view(by_file) == _finding_view(by_rules)
    assert _finding_view(by_file)[0], "用例要能产生 finding，否则比较没有意义"
    file_refs = {ref.scope for f in only(by_file).findings for ref in f.rule_refs}
    assert file_refs == {"rules_file"}


# ---------- 输出 ----------


def test_result_json_and_report_show_rules_file(tmp_path):
    (tmp_path / "r.yaml").write_text("data:\n  amount: {$on_value_change: breaking}\nnothing: {$ignore: true}\n")
    report = run(tmp_path, suite_text(req("a", extra="    rules_file: r.yaml\n")), _bodies(), reporters=reporters(["html"]))
    data = json.loads((tmp_path / "out" / "result.json").read_text())
    [finding] = [f for f in data["requests"][0]["findings"] if f["path"] == "data.amount"]
    file = str(tmp_path / "r.yaml")
    assert finding["rule_refs"] == [{"scope": "rules_file", "index": 0, "file": file, "line": 2}]
    assert data["unmatched_rules"] == [
        {"scope": "rules_file", "index": 1, "request": "a", "path": "nothing", "file": file, "line": 3}
    ]
    ref = only(report).findings[0].rule_refs
    assert common.rule_where(ref[0].scope, ref[0].index, ref[0].file, ref[0].line) == f"rules_file {file}:2"
    html = (tmp_path / "out" / "report.html").read_text()
    assert f"rules_file {file}:2 data.amount（on_value_change: breaking）" in html
    assert f"a: rules_file {file}:3 nothing" in html


def test_without_rules_file_no_new_fields(tmp_path):
    inline = '    rules:\n      - {path: data.amount, on_value_change: breaking}\n'
    run(tmp_path, suite_text(req("a", extra=inline)), _bodies())
    text = (tmp_path / "out" / "result.json").read_text()
    assert '"file"' not in text and '"line": null' not in text.split('"unmatched_rules"')[1]
    assert '"rule_refs": [\n            {\n              "scope": "request",\n              "index": 0\n            }' in text


def test_static_warning_points_into_rules_file(tmp_path):
    rules = textwrap.dedent("""\
        data: {$ignore: true}
        data.amount: {$decimal: {}}
    """)
    path = _suite_with_file(tmp_path, rules_text=rules)
    report = service.validate_suite_sync(path)
    [w] = [w for w in report.warnings if w.code == "rule_under_ignored"]
    assert (w.file, w.line, w.location) == (str(tmp_path / "rules" / "r.yaml"), 2, "data.amount")


def test_cli_validate_prints_rules_file_location(tmp_path, capsys):
    from api_regress.cli import main

    path = _suite_with_file(tmp_path, rules_text="data: {$ignore: true}\ndata.amount: {$decimal: {}}\n")
    assert main(["validate", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"[rule_under_ignored] {tmp_path / 'rules' / 'r.yaml'}:2: data.amount: 规则 'data.amount'" in out
    (tmp_path / "rules" / "r.yaml").write_text("data:\n  $foo: 1\n")
    assert main(["validate", str(path)]) == 2
    assert f"{tmp_path / 'rules' / 'r.yaml'}:2: data: 未知的规则属性" in capsys.readouterr().out
