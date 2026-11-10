from pathlib import Path

import pytest

from api_regress.core.config import ConfigError, UnverifiedSuite, load_suite, load_suite_text

FIXTURE = Path(__file__).parent / "fixtures" / "shop_suite.yaml"
ENV = {"BASE_TOKEN": "b", "CAND_TOKEN": "c"}

MINI = """\
version: 1
targets:
  baseline:
    base_url: http://base
  candidate:
    base_url: http://cand
requests:
  - name: a
    method: GET
    path: /a
"""


def load(text, sides=("baseline", "candidate"), env=None):
    return load_suite_text(text, "s.yaml", sides, env or {})


def errors(text, sides=("baseline", "candidate"), env=None):
    with pytest.raises(ConfigError) as info:
        load(text, sides, env)
    return info.value.issues


def test_spec_example_loads():
    loaded = load_suite(FIXTURE, sides=("baseline", "candidate"), env=ENV)
    suite = loaded.suite
    assert [r.name for r in suite.requests] == ["health", "order-list", "order-detail"]
    assert suite.targets.baseline.headers["Authorization"] == "Bearer b"
    assert suite.requests[0].method == "HEAD"
    assert suite.requests[1].query == {"page": "1", "size": "20"}
    detail = suite.requests[2]
    assert detail.rules[1].array.mode == "keyed"
    assert detail.rules[1].array.keys == ["sku_id"]
    assert suite.defaults.rules[1].decimal.allow_string is False
    assert suite.success_of(detail) == "body.code == 0"
    # raw 保留替换前的模板，供快照指纹使用
    assert loaded.raw["targets"]["baseline"]["headers"]["Authorization"] == "Bearer ${BASE_TOKEN}"


# ---------- AC-01 ----------


def test_unknown_field_reports_line():
    text = MINI.replace("    path: /a", "    path: /a\n    methd: GET")
    [issue] = errors(text)
    assert issue.line == 11
    assert issue.location == "requests[0].methd"
    assert "未知字段" in issue.message
    assert str(issue).startswith("s.yaml:11:")


def test_unknown_field_in_nested_rule():
    text = MINI + "    rules:\n      - path: data.x\n        ignroe: true\n"
    [issue] = errors(text)
    assert issue.line == 13
    assert issue.location == "requests[0].rules[0].ignroe"


@pytest.mark.parametrize("method", ["POST", "put", "DELETE", "PATCH"])
def test_write_method_rejected(method):
    [issue] = errors(MINI.replace("method: GET", f"method: {method}"))
    assert issue.line == 9
    assert "写方法" in issue.message


def test_unknown_method_rejected():
    [issue] = errors(MINI.replace("method: GET", "method: FETCH"))
    assert "GET 或 HEAD" in issue.message


def test_method_case_normalized():
    assert load(MINI.replace("method: GET", "method: get")).suite.requests[0].method == "GET"


def test_duplicate_name():
    text = MINI + "  - name: a\n    method: GET\n    path: /b\n"
    [issue] = errors(text)
    assert issue.line == 11
    assert "重复" in issue.message


@pytest.mark.parametrize("name", ["a b", "a/b", "中文", ""])
def test_invalid_name(name):
    [issue] = errors(MINI.replace("name: a", f'name: "{name}"'))
    assert issue.location == "requests[0].name"


def test_missing_var_is_error_when_needed():
    text = MINI.replace("base_url: http://cand", "base_url: http://cand\n    headers: {Authorization: '${CAND_TOKEN}'}")
    [issue] = errors(text)
    assert "CAND_TOKEN" in issue.message
    assert issue.line == 7


def test_missing_var_only_recorded_when_not_needed():
    text = MINI.replace("base_url: http://cand", "base_url: http://cand\n    headers: {Authorization: '${CAND_TOKEN}'}")
    loaded = load(text, sides=())
    [missing] = loaded.missing_vars
    assert (missing.name, missing.side, missing.line) == ("CAND_TOKEN", "candidate", 7)


def test_missing_var_other_side_ignored():
    """AC-03 的配置部分：record 只需要 baseline，候选缺变量照常加载。"""
    text = MINI.replace("http://cand", "${CAND_URL}")
    loaded = load(text, sides=("baseline",))
    assert loaded.suite.targets.candidate.base_url == "${CAND_URL}"
    assert errors(text, sides=("candidate",))[0].message == "缺少环境变量 CAND_URL"


def test_shared_var_needed_by_any_side():
    text = MINI.replace("path: /a", "path: /a/${ID}")
    assert "ID" in errors(text, sides=("baseline",))[0].message
    assert load(text, sides=()).missing_vars[0].side == "shared"


def unverified(text):
    with pytest.raises(UnverifiedSuite) as info:
        load(text, sides=())
    return info.value


def test_unresolved_typed_field_in_validate_is_unverified_not_error():
    exc = unverified(MINI + 'defaults:\n  timeout: "${T}"\n')
    assert [m.name for m in exc.missing] == ["T"]
    assert [f.location for f in exc.fields] == ["defaults.timeout"]
    assert exc.request_count == 1


@pytest.mark.parametrize(
    ("snippet", "location"),
    [
        ('    method: GET', None),
        ('    rules:\n      - path: data.amount\n        on_value_change: "${LVL}"', "requests[0].rules[0].on_value_change"),
    ],
)
def test_unresolved_var_never_turns_into_false_error(snippet, location):
    if location is None:  # method 本身来自缺失变量
        text, location = MINI.replace("method: GET", 'method: "${M}"'), "requests[0].method"
    else:
        text = MINI + snippet + "\n"
    assert [f.location for f in unverified(text).fields] == [location]
    # 变量设置后，非法值照常报错
    assert errors(text, env={"M": "POST", "LVL": "fatal"})


def test_unresolved_var_does_not_hide_other_errors():
    text = MINI.replace("method: GET", 'method: "${M}"') + "    methd: GET\n"
    [issue] = errors(text, sides=())
    assert issue.location == "requests[0].methd"


def test_yaml_syntax_error_has_line():
    [issue] = errors("version: 1\ntargets:\n  baseline: [\n")
    assert issue.line is not None
    assert "YAML" in issue.message


def test_yaml_duplicate_key():
    [issue] = errors(MINI.replace("    path: /a", "    path: /a\n    path: /b"))
    assert issue.line == 11


def test_unquoted_query_number_rejected():
    [issue] = errors(MINI + "    query: {page: 007}\n")
    assert issue.location == "requests[0].query.page"
    assert "引号" in issue.message


def test_bad_base_url_scheme():
    [issue] = errors(MINI.replace("http://base", "base"))
    assert issue.location == "targets.baseline.base_url"


def test_version_required():
    issues = errors(MINI.replace("version: 1\n", ""))
    assert issues[0].location == "version"


def test_root_rule_path():
    rule = load(MINI + '    rules:\n      - path: "$"\n        array: {keys: [id]}\n').suite.requests[0].rules[0]
    assert rule.tokens == ()


# ---------- AC-02 ----------


def test_default_value_when_unset():
    text = MINI.replace("http://cand", "${CAND_URL:-http://fallback}")
    assert load(text).suite.targets.candidate.base_url == "http://fallback"
    assert load(text, env={"CAND_URL": "https://x"}).suite.targets.candidate.base_url == "https://x"


def test_empty_default():
    text = MINI.replace("path: /a", "path: /a${SUFFIX:-}")
    assert load(text).suite.requests[0].path == "/a"


def test_numeric_field_from_var():
    text = MINI + 'defaults:\n  timeout: "${T}"\n  concurrency: "${C:-8}"\n  retries: "${R}"\n'
    defaults = load(text, env={"T": "2.5", "R": "0"}).suite.defaults
    assert (defaults.timeout, defaults.concurrency, defaults.retries) == (2.5, 8, 0)


def test_multiple_vars_in_one_string():
    text = MINI.replace("http://base", "${SCHEME}://${HOST}:${PORT:-80}")
    url = load(text, env={"SCHEME": "https", "HOST": "h"}).suite.targets.baseline.base_url
    assert url == "https://h:80"


# ---------- 规则字段 ----------


def rule_text(rule_yaml):
    return MINI + "    rules:\n      - " + rule_yaml.replace("\n", "\n        ") + "\n"


def test_rule_needs_attribute():
    [issue] = errors(rule_text("path: data.x"))
    assert "至少" in issue.message


def test_rule_bad_path():
    [issue] = errors(rule_text("path: 'data..x'\nignore: true"))
    assert issue.location == "requests[0].rules[0].path"


@pytest.mark.parametrize(
    ("array", "mode"),
    [("{}", "schema"), ("{keys: [id]}", "keyed"), ("{mode: positional}", "positional"), ("{mode: keyed, keys: [a, b]}", "keyed")],
)
def test_array_rule_modes(array, mode):
    rule = load(rule_text(f"path: data.items\narray: {array}")).suite.requests[0].rules[0]
    assert rule.array.mode == mode


@pytest.mark.parametrize("array", ["{mode: keyed}", "{mode: schema, keys: [id]}", "{keys: []}", "{mode: random}"])
def test_array_rule_invalid(array):
    [issue] = errors(rule_text(f"path: data.items\narray: {array}"))
    assert issue.location.startswith("requests[0].rules[0].array")


def test_bom_file(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_bytes(b"\xef\xbb\xbf" + MINI.encode())
    assert load_suite(f, env={}).suite.requests[0].name == "a"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_suite(tmp_path / "nope.yaml")
    assert "无法读取" in info.value.issues[0].message


# ---------- 环境变量的边界写法 ----------


def test_empty_var_falls_back_to_default():
    text = MINI.replace("http://cand", "${CAND_URL:-http://fallback}")
    assert load(text, env={"CAND_URL": ""}).suite.targets.candidate.base_url == "http://fallback"


def test_empty_var_without_default_is_kept_empty():
    text = MINI.replace("path: /a", "path: /a${SUFFIX}")
    assert load(text, env={"SUFFIX": ""}).suite.requests[0].path == "/a"


@pytest.mark.parametrize("value", ["${CAND_URL:-${BASE_URL}}", "${TOKEN-x}", "${TOKEN:x}", "${ TOKEN }", "${1A}", "x ${", "${A:-a$b}"])
def test_unrecognized_var_syntax_is_error(value):
    text = MINI.replace("http://cand", value)
    for sides in [(), ("baseline",)]:
        [issue] = errors(text, sides=sides, env={"CAND_URL": "http://c", "BASE_URL": "http://b", "TOKEN": "t"})
        assert issue.location == "targets.candidate.base_url"
        assert "无法识别" in issue.message


# ---------- base_url ----------


@pytest.mark.parametrize("url", ["api.example.com", "ftp://cand", "http://", "https//x"])
def test_validate_checks_literal_base_url(url):
    [issue] = errors(MINI.replace("http://cand", url), sides=())
    assert issue.location == "targets.candidate.base_url"


def test_base_url_scheme_case_insensitive():
    assert load(MINI.replace("http://cand", "HTTPS://Cand.example.com")).suite


# ---------- YAML 输入 ----------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINI + "    query: {start: 2024-02-30}\n", "无法加载"),
        ("version: 1\nx: &a [*a]\n", "引用了自身"),
        ("x: " + "[" * 2000 + "]" * 2000 + "\n", "嵌套"),
        ("x: " + "[" * 80 + "]" * 80 + "\n", "嵌套"),
    ],
)
def test_hostile_yaml_is_config_error(text, message):
    [issue] = errors(text)
    assert message in issue.message


def test_alias_bomb_is_rejected_quickly():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 7):
        refs = ", ".join([f"*a{i - 1}"] * 10)
        lines.append(f"a{i}: &a{i} [{refs}]")
    [issue] = errors("\n".join(lines) + "\n")
    assert "节点" in issue.message


def test_yaml_error_does_not_echo_source_line():
    text = MINI.replace("    base_url: http://base", "    base_url: http://base\n    headers:\n      Authorization: Bearer abc: SENTINEL-456")
    [issue] = errors(text)
    assert "SENTINEL-456" not in issue.message
    assert issue.line == 6


def test_yaml_error_line_within_file():
    text = 'version: 1\nx: "unclosed\n'
    [issue] = errors(text)
    assert 1 <= issue.line <= 2


def test_yaml_duplicate_keys_each_reported():
    text = MINI.replace("    path: /a", "    path: /a\n    path: /b\n    path: /c")
    issues = errors(text)
    assert [(i.location, i.line) for i in issues] == [("requests[0].path", 11), ("requests[0].path", 12)]


def test_infinite_timeout_rejected():
    [issue] = errors(MINI + "defaults:\n  timeout: .inf\n")
    assert issue.location == "defaults.timeout"


def test_version_from_var():
    assert load(MINI.replace("version: 1", 'version: "${V}"'), env={"V": "1"}).suite.version == 1
    assert "只支持 1" in errors(MINI.replace("version: 1", "version: 2"))[0].message


# ---------- success 覆盖（D6） ----------


def test_request_success_null_disables_default():
    text = MINI.replace("requests:", 'defaults:\n  success: "body.code == 0"\nrequests:') + "    success: null\n"
    text += "  - name: b\n    method: GET\n    path: /b\n  - name: c\n    method: GET\n    path: /c\n    success: \"status == 200\"\n"
    suite = load(text).suite
    assert [suite.success_of(r) for r in suite.requests] == [None, "body.code == 0", "status == 200"]


def test_names_are_case_sensitive():
    """D8：只差大小写的 name 是两条请求。"""
    text = MINI + "  - name: A\n    method: GET\n    path: /b\n"
    assert [r.name for r in load(text).suite.requests] == ["a", "A"]


def test_rule_tokens_follow_path_changes():
    rule = load(MINI + "    rules:\n      - path: data.x\n        ignore: true\n").suite.requests[0].rules[0]
    assert rule.model_copy(update={"path": "data.y"}).tokens != rule.tokens


def test_unresolved_list_items_are_unverified_not_crash():
    exc = unverified(MINI + '    asserts: ["${A}", "${B}"]\n')
    assert [f.location for f in exc.fields] == ["requests[0].asserts[0]", "requests[0].asserts[1]"]
