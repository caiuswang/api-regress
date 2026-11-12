"""shape：字段树的内容、生效规则的标注、截断、样本来源、骨架和退出码。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from api_regress import cli
from api_regress.core import paths, service, shape
from api_regress.core.config import load_suite, load_suite_text
from api_regress.core.rules import RuleSet
from tests.mock_api import JSON, Reply, Server, j, record, req, run, suite_text

def _order(amount: str = "12.50", coupon: str = "null", items: int = 2) -> str:
    """数字要保留原文的小数位，所以直接拼 JSON 文本。"""
    rows = ['{"sku_id": "A12", "price": 99.00}', '{"sku_id": "B7", "price": 5.5, "gift": true}'][:items]
    return (
        f'{{"code": 0, "data": {{"amount": {amount}, "coupon": {coupon}, "items": [{", ".join(rows)}],'
        ' "updated_at": "2026-11-12T10:00:00Z", "meta": {"a": 1}}}'
    )


RULES_FILE = """\
data:
  coupon: {$nullable: true}
"""

DEFAULTS = """\
  rules:
    - path: "**.updated_at"
      ignore: true
    - path: "**.amount"
      decimal: {}
    - path: "**.never_there"
      ignore: true
"""

ORDER_REQ = req(
    "order-detail",
    "/v1/orders/10086",
    extra=(
        "    rules_file: rules/order.yaml\n"
        "    rules:\n"
        "      - path: data.amount\n"
        "        on_value_change: breaking\n"
        "      - path: data.items\n"
        "        array: {keys: [sku_id]}\n"
        "      - path: data.items[0].price\n"
        "        decimal: {}\n"
    ),
)

TREE = """\
# order-detail  GET /v1/orders/10086  样本 2 个（{src}）
code:          # number  例 0
data:          # object
  amount:      # number  小数位 {{1, 2}}  例 12.50  | decimal ← defaults.rules[1] **.amount; on_value_change: breaking ← rules[0] data.amount
  coupon:      # object|null  | nullable ← rules_file {rules}:2 data.coupon
    code:      # string  例 "NEW10"
  items:       # array  长度 {{1, 2}}  | array: keyed [sku_id] ← rules[1] data.items
  items[*]:    # object
    sku_id:    # string  例 "A12"
    price:     # number  小数位 {{1, 2}}  例 99.00
    gift:      # boolean  例 true
  updated_at:  # string  例 "2026-11-12T10:00:00Z"  | ignore ← defaults.rules[0] **.updated_at（子树不对比）
  meta:        # object
    a:         # number  例 1
# 写了下标的规则（树上不展示）：
#   rules[2] data.items[0].price
# 未命中的规则：
#   defaults.rules[2] **.never_there"""


def _server() -> Server:
    """基线两次的响应不同：类型、小数位、数组长度都要合并。"""
    first = Reply(body=_order())
    second = Reply(body=_order("3.5", '{"code": "NEW10"}', items=1))
    return Server().on("base", "/v1/orders/10086", first, second).on("cand", "/v1/orders/10086", first)


def _setup(tmp_path: Path) -> str:
    (tmp_path / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules" / "order.yaml").write_text(RULES_FILE)
    return suite_text(ORDER_REQ, defaults=DEFAULTS)


def _shape(tmp_path: Path, source: Path, env=None, **opts) -> service.ShapeReport:
    return service.shape_sync(tmp_path / "suite.yaml", service.ShapeOptions(source=source, **opts), env=env or {})


@pytest.fixture
def snap(tmp_path: Path) -> Path:
    report = record(tmp_path, _setup(tmp_path), _server())
    assert report.exit_code == 0, report.exit_reason
    return tmp_path / "snap"


# ---------- 字段树的内容和生效规则 ----------


def test_tree_from_snapshot(tmp_path, snap):
    report = _shape(tmp_path, snap)
    assert report.exit_code == 0, report.exit_reason
    rules = tmp_path / "rules" / "order.yaml"
    assert "\n".join(report.lines) == TREE.format(src=snap, rules=rules)
    assert report.requests[0].samples == 2


def test_tree_from_run_dir_reads_baseline(tmp_path):
    report = run(tmp_path, _setup(tmp_path), _server())
    assert report.out_dir is not None
    out = tmp_path / "out"
    shaped = _shape(tmp_path, out)
    assert shaped.exit_code == 0, shaped.exit_reason
    assert "\n".join(shaped.lines) == TREE.format(src=out, rules=tmp_path / "rules" / "order.yaml")


def test_snapshot_wins_when_both_files_exist(tmp_path, snap):
    (snap / "result.json").write_text("{}")
    assert _shape(tmp_path, snap).exit_code == 0


def _tree(bodies: list, rules: str = "", max_keys: int = 50) -> list[str]:
    loaded = load_suite_text(suite_text(req("x", extra=rules)), env={})
    ruleset = RuleSet.for_request(loaded.suite, loaded.suite.requests[0], 0)
    return shape.render_tree(shape.build(bodies), ruleset, max_keys)


def _values(*texts: str) -> list:
    from api_regress.core.jsonload import parse_json

    return [parse_json(t)[0] for t in texts]


def test_types_union_null_last_and_first_example():
    lines = _tree(_values('{"v": null, "w": [1, "a", null]}', '{"v": "x"}', '{"v": 1.50}'))
    assert lines == [
        'v:     # string|number|null  小数位 {2}  例 "x"',
        "w:     # array  长度 {3}",
        "w[*]:  # number|string|null  例 1",
    ]


def test_order_is_first_occurrence_and_empty_array_has_no_element_row():
    lines = _tree(_values('{"b": [], "a": 1}', '{"c": true, "a": 2}'))
    assert [line.split(":")[0] for line in lines] == ["b", "a", "c"]
    assert lines[0].endswith("# array  长度 {0}")


def test_long_string_is_clipped():
    lines = _tree([{"s": "x" * 41}, {"s": "y"}])
    assert lines == [f's:  # string  例 "{"x" * 40}"…']


def test_root_array_and_nested_arrays():
    lines = _tree(_values('[[{"id": 1}], []]'), "    rules:\n      - {path: $, array: {mode: unordered}}\n")
    assert lines == [
        "# 根节点 $：array  长度 {2}  | array: unordered ← rules[0] $",
        '"[*]":     # array  长度 {0, 1}',
        '"[*][*]":  # object',
        "  id:      # number  例 1",
    ]


def test_root_scalar():
    assert _tree(_values('"ok"', "12.5")) == ['# 根节点 $：string|number  小数位 {1}  例 "ok"']


def test_ignored_root_is_not_expanded():
    lines = _tree([{"a": 1}], "    rules:\n      - {path: $, ignore: true}\n")
    assert lines == ["# 根节点 $：object  | ignore ← rules[0] $（子树不对比）"]


def test_ignore_false_is_shown():
    lines = _tree([{"a": {"b": 1}}], "    rules:\n      - {path: a, ignore: false}\n      - {path: a.b, nullable: false}\n")
    assert lines == ["a:    # object  | ignore: false ← rules[0] a", "  b:  # number  例 1  | nullable: false ← rules[1] a.b"]


def test_special_keys_are_quoted_as_rules_file_keys():
    lines = _tree([{"x.y": 1, "on": 2, "$x": 3, "[a]": 4, "it's \"q\"": 5}])
    keys = [line.split("  #")[0].rstrip() for line in lines]
    assert keys == ["'[\"x.y\"]':", '"on":', "'[\"$x\"]':", "'[\"[a]\"]':", '"[\\"it\'s \\\\\\"q\\\\\\"\\"]":']


@pytest.mark.parametrize(
    "tokens",
    [
        (paths.Key("x.y"),),
        (paths.Key("on"),),
        (paths.Key("null"),),
        (paths.Key("1"),),
        (paths.Key("$x"),),
        (paths.Key("a: b"),),
        (paths.Key("# c"),),
        (paths.Key("it's \"q\""),),
        (paths.Key("a\nb"),),
        (paths.Key("items"), paths.ANY),
        (paths.ANY, paths.ANY),
        (paths.Key("中文"),),
    ],
)
def test_yaml_key_round_trips_to_the_same_path(tokens):
    key = shape.yaml_key(paths.format_path(tokens))
    loaded = yaml.safe_load(f"{key}:  # number\n")
    (parsed,) = loaded
    assert isinstance(parsed, str)
    assert paths.parse(parsed) == tokens


# ---------- 截断 ----------


def test_max_keys_truncates_display_only(tmp_path):
    body = {"ids": {str(i): {"v": i} for i in range(5)}}
    rules = "    rules:\n      - {path: 'ids[\"4\"].v', on_value_change: info}\n"
    lines = _tree([body], rules, max_keys=2)
    assert lines == [
        "ids:    # object",
        '  "0":  # object',
        "    v:  # number  例 0",
        '  "1":  # object',
        "    v:  # number  例 1",
        "  # … 还有 3 个 key 没有列出（key 可能是 id 之类的动态值）",
    ]
    loaded = load_suite_text(suite_text(req("x", extra=rules)), env={})
    ruleset = RuleSet.for_request(loaded.suite, loaded.suite.requests[0], 0)
    assert shape.unmatched(ruleset.rules, shape.build([body]), [body]) == []


def test_unmatched_matches_v1_definition():
    """树上判断和逐个 body 遍历（v1 的做法）结果相同。"""
    from api_regress.core.rules import unmatched_rules

    bodies = _values('{"a": [{"b": 1}, {"c": [2, 3]}], "d": null}', '{"a": []}')
    rules = "".join(
        f"      - {{path: '{p}', ignore: true}}\n"
        for p in ["a[*].b", "a[1].c[1]", "a[0].c", "**.c[*]", "a[*].x", "d.e", "**.d", "a[2]", "$"]
    )
    loaded = load_suite_text(suite_text(req("x", extra="    rules:\n" + rules)), env={})
    ruleset = RuleSet.for_request(loaded.suite, loaded.suite.requests[0], 0)
    expected = unmatched_rules(ruleset.rules, bodies)
    got = shape.unmatched(ruleset.rules, shape.build(bodies), bodies)
    assert [r.rule.path for r in got] == [r.rule.path for r in ruleset.rules if r in expected]
    assert [r.rule.path for r in got] == ["a[0].c", "a[*].x", "d.e", "a[2]"]


# ---------- 样本来源、环境变量与退出码 ----------


def test_needs_no_env_vars_and_sends_nothing(tmp_path, snap, monkeypatch):
    text = (tmp_path / "suite.yaml").read_text()
    text = text.replace("http://base.test", "${BASE_URL}").replace("http://cand.test", "${CAND_URL}")
    text = text.replace("defaults:\n", "defaults:\n  headers:\n    Authorization: Bearer ${TOKEN}\n")
    (tmp_path / "suite.yaml").write_text(text)
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("shape 不应该发请求")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    assert _shape(tmp_path, snap, env={}).exit_code == 0


@pytest.mark.parametrize(
    "extra",
    ['    rules:\n      - {path: "${FIELD}", ignore: true}\n', "    rules_file: ${RULES}\n"],
)
def test_rules_with_missing_env_var_is_suite_error(tmp_path, snap, extra):
    (tmp_path / "suite.yaml").write_text(suite_text(req("order-detail", "/v1/orders/10086", extra=extra)))
    report = _shape(tmp_path, snap)
    assert report.exit_code == 2
    assert "缺失的环境变量" in report.errors[0].message


def test_request_missing_from_source_is_exit_4(tmp_path, snap):
    text = _setup(tmp_path) + req("other", "/other")
    (tmp_path / "suite.yaml").write_text(text)
    report = _shape(tmp_path, snap)
    assert report.exit_code == 4
    assert "# other: 没有样本（快照里没有这条请求）" in report.lines
    assert report.lines[0].startswith("# order-detail")


def test_non_json_samples_are_skipped(tmp_path):
    server = Server().on("base", "/x", Reply(body="<html>", content_type="text/html"), j({"a": 1}))
    server.on("base", "/e", Reply(body="<html>", content_type="text/html"), Reply(body="", content_type=JSON))
    record(tmp_path, suite_text(req("x", "/x") + req("e", "/e", extra="    success: null\n")), server)
    report = _shape(tmp_path, tmp_path / "snap")
    assert report.requests[0].samples == 1
    # 有非空的 body 但都不是 JSON（多半是错误页）：仍然算没有样本
    assert report.exit_code == 4
    assert report.lines[-1] == "# e: 没有样本（样本都是网络错误或不是 JSON）"


def _no_body_server() -> Server:
    server = Server().on("base", "/h", Reply(body="", content_type=JSON))
    return server.on("base", "/n", Reply(status=204, body="", content_type=None)).on("base", "/x", j({"a": 1}))


NO_BODY = req("h", "/h", method="HEAD") + req("n", "/n") + req("x", "/x")


def test_head_and_empty_body_are_skipped_without_exit_4(tmp_path):
    record(tmp_path, suite_text(NO_BODY), _no_body_server())
    report = _shape(tmp_path, tmp_path / "snap")
    assert report.exit_code == 0, report.exit_reason
    assert report.lines[0] == "# h: 没有 body（HEAD 请求，跳过）"
    assert report.lines[2] == "# n: 没有 body（响应 body 为空，跳过）"
    assert [r.skipped for r in report.requests] == ["HEAD 请求，跳过", "响应 body 为空，跳过", None]


def test_write_skips_requests_without_body(tmp_path):
    record(tmp_path, suite_text(NO_BODY), _no_body_server())
    report = _shape(tmp_path, tmp_path / "snap", write=tmp_path / "skel")
    assert report.exit_code == 0
    assert [p.name for p in (tmp_path / "skel").iterdir()] == ["x.yaml"]
    assert report.lines[:2] == ["# h: 没有 body（HEAD 请求，跳过）", "# n: 没有 body（响应 body 为空，跳过）"]


def test_all_network_errors_is_still_no_samples(tmp_path):
    """HEAD 请求的 sample 全是网络错误（手工改写 run 目录）时，不按没有 body 跳过。"""
    server = Server().both("/h", Reply(body="", content_type=JSON))
    run(tmp_path, suite_text(req("h", "/h", method="HEAD")), server)
    side = tmp_path / "out" / "baseline" / "h.json"
    data = json.loads(side.read_text())
    data["samples"] = [{"url": s["url"], "status": None, "error": "ConnectError", "retries": 1} for s in data["samples"]]
    side.write_text(json.dumps(data))
    report = _shape(tmp_path, tmp_path / "out")
    assert report.exit_code == 4
    assert report.lines == ["# h: 没有样本（样本都是网络错误或不是 JSON）"]


def test_run_dir_without_baseline_file(tmp_path):
    server = Server().both("/x", j({"a": 1}))
    run(tmp_path, suite_text(req("x", "/x")), server)
    (tmp_path / "out" / "baseline" / "x.json").unlink()
    report = _shape(tmp_path, tmp_path / "out")
    assert report.exit_code == 4
    assert report.lines == ["# x: 没有样本（run 目录里没有这条请求的基线响应）"]


@pytest.mark.parametrize("kind", ["missing", "empty", "other", "corrupt-body", "bad-result"])
def test_unusable_source_is_exit_3(tmp_path, snap, kind):
    target = tmp_path / "src"
    if kind == "empty":
        target.mkdir()
    elif kind == "other":
        target.mkdir()
        (target / "a.txt").write_text("x")
    elif kind == "corrupt-body":
        target = snap
        next((snap / "samples" / "order-detail").iterdir()).write_text("{}")
    elif kind == "bad-result":
        target.mkdir()
        (target / "result.json").write_text("{}")
    report = _shape(tmp_path, target)
    assert report.exit_code == 3, report.exit_reason


def test_usage_errors_are_exit_2(tmp_path, snap):
    assert _shape(tmp_path, snap, only=["nope"]).exit_code == 2
    assert _shape(tmp_path, snap, max_keys=0).exit_code == 2
    # 参数错误优先于 --from 不可用
    assert _shape(tmp_path, tmp_path / "missing", only=["nope"]).exit_code == 2


def test_only_limits_requests(tmp_path, snap):
    text = _setup(tmp_path) + req("other", "/other")
    (tmp_path / "suite.yaml").write_text(text)
    report = _shape(tmp_path, snap, only=["order-detail"])
    assert report.exit_code == 0
    assert [r.name for r in report.requests] == ["order-detail"]


def test_logs_have_no_example_values(tmp_path, caplog):
    server = Server().on("base", "/x", j({"token": "SECRET-VALUE-123", "n": 424242}))
    record(tmp_path, suite_text(req("x", "/x")), server)
    caplog.set_level(logging.DEBUG, logger="api_regress")
    report = _shape(tmp_path, tmp_path / "snap")
    assert any("SECRET-VALUE-123" in line for line in report.lines)
    assert "SECRET-VALUE-123" not in caplog.text and "424242" not in caplog.text
    assert "节点" in caplog.text


# ---------- --write ----------


def test_write_skeleton(tmp_path, snap):
    out = tmp_path / "skel"
    report = _shape(tmp_path, snap, write=out)
    assert report.exit_code == 0, report.exit_reason
    path = out / "order-detail.yaml"
    assert report.written == [str(path)]
    assert report.lines[0] == f"wrote {path}"
    assert "rules_file: skel/order-detail.yaml" in report.lines[-1]
    text = path.read_text()
    assert text.splitlines()[3:] == [
        "code:          # number",
        "data:          # object",
        "  amount:      # number  小数位 {1, 2}",
        "  coupon:      # object|null",
        "    code:      # string",
        "  items:       # array",
        "  items[*]:    # object",
        "    sku_id:    # string",
        "    price:     # number  小数位 {1, 2}",
        "    gift:      # boolean",
        "  updated_at:  # string  （已 ignore，子树不展开）",
        "  meta:        # object",
        "    a:         # number",
    ]
    for value in ("NEW10", "A12", "12.50", "2026-11-12", "rules[", "←"):
        assert value not in text


def test_skeleton_as_rules_file_validates_without_rules(tmp_path, snap):
    _shape(tmp_path, snap, write=tmp_path / "skel")
    text = suite_text(req("order-detail", "/v1/orders/10086", extra="    rules_file: skel/order-detail.yaml\n"))
    (tmp_path / "v.yaml").write_text(text)
    assert service.validate_suite_sync(tmp_path / "v.yaml", env={}).ok
    loaded = load_suite(tmp_path / "v.yaml", env={})
    assert loaded.suite.requests[0].file_rules == []


def test_write_refuses_when_any_target_exists(tmp_path):
    server = Server().on("base", "/a", j({"a": 1})).on("base", "/b", j({"b": 1}))
    record(tmp_path, suite_text(req("a", "/a") + req("b", "/b")), server)
    out = tmp_path / "skel"
    out.mkdir()
    (out / "b.yaml").write_text("mine\n")
    report = _shape(tmp_path, tmp_path / "snap", write=out)
    assert report.exit_code == 2
    assert str(out / "b.yaml") in report.errors[0].message
    assert sorted(p.name for p in out.iterdir()) == ["b.yaml"]
    assert (out / "b.yaml").read_text() == "mine\n"


def test_write_failure_removes_written_files(tmp_path, monkeypatch):
    server = Server().on("base", "/a", j({"a": 1})).on("base", "/b", j({"b": 1}))
    record(tmp_path, suite_text(req("a", "/a") + req("b", "/b")), server)
    out = tmp_path / "skel"
    real_open = Path.open

    def flaky(self, mode="r", *args, **kwargs):
        if self.name == "b.yaml":
            raise OSError(28, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)
    report = _shape(tmp_path, tmp_path / "snap", write=out)
    assert report.exit_code == 3
    assert list(out.iterdir()) == []


def test_write_skips_requests_without_samples(tmp_path, snap):
    (tmp_path / "suite.yaml").write_text(_setup(tmp_path) + req("other", "/other"))
    report = _shape(tmp_path, snap, write=tmp_path / "skel")
    assert report.exit_code == 4
    assert sorted(p.name for p in (tmp_path / "skel").iterdir()) == ["order-detail.yaml"]
    assert report.lines[0] == "# other: 没有样本（快照里没有这条请求）"


# ---------- 命令行 ----------


def test_cli_prints_tree_and_exit_line(tmp_path, snap, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["shape", "suite.yaml", "--from", "snap", "--only", "order-detail", "--log-level", "WARNING"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == "# order-detail  GET /v1/orders/10086  样本 2 个（snap）"
    assert out[-1] == "exit 0: 全部输出"


def test_cli_write_and_usage_errors(tmp_path, snap, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["shape", "suite.yaml", "--from", "snap", "--write", "skel", "--log-level", "WARNING"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"wrote {Path('skel') / 'order-detail.yaml'}"
    assert cli.main(["shape", "suite.yaml", "--from", "snap", "--write", "skel", "--log-level", "WARNING"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "INVALID, 1 error(s)"
    assert out[-1] == "exit 2: suite 或命令行参数有问题"
    assert cli.main(["shape", "suite.yaml", "--from", "snap", "--max-keys", "x"]) == 2
    assert cli.main(["shape", "suite.yaml"]) == 2  # --from 必填


def test_skeleton_drops_all_keys_of_oversized_objects(tmp_path):
    """key 多到被截断的对象（多半是 id、邮箱之类的数据）在骨架里一个 key 都不写；展示版照常截断。"""
    users = {f"user{i}@corp.example": {"vip": True} for i in range(3)}
    server = Server().on("base", "/u", j({"code": 0, "users": users}))
    record(tmp_path, suite_text(req("u", "/u")), server)
    report = _shape(tmp_path, tmp_path / "snap", write=tmp_path / "skel", max_keys=2)
    assert report.exit_code == 0
    text = (tmp_path / "skel" / "u.yaml").read_text()
    assert "@corp.example" not in text
    assert text.splitlines()[3:] == [
        "code:   # number",
        "users:  # object",
        "  # … 有 3 个 key，超过 --max-keys，一个都不列出（key 可能是 id、邮箱之类的数据，不写进规则文件）",
    ]
    shown = "\n".join(_shape(tmp_path, tmp_path / "snap", max_keys=2).lines)
    assert "user0@corp.example" in shown and "还有 1 个 key 没有列出" in shown


def _hinted_path(report: service.ShapeReport) -> str:
    return report.lines[-1].split("rules_file: ", 1)[1].split("，", 1)[0]


def test_hint_is_relative_to_suite_path_without_resolving_symlinks(tmp_path, snap):
    real = tmp_path / "a" / "b"
    real.mkdir(parents=True)
    (real / "suite.yaml").write_text((tmp_path / "suite.yaml").read_text())
    (tmp_path / "link").mkdir()
    (tmp_path / "link" / "rules").symlink_to(tmp_path / "rules")  # suite 里的 rules_file 按链接所在目录解析
    link = tmp_path / "link" / "suite.yaml"
    link.symlink_to(real / "suite.yaml")
    report = service.shape_sync(link, service.ShapeOptions(source=snap, write=tmp_path / "skel"), env={})
    assert report.exit_code == 0, report.exit_reason
    hinted = _hinted_path(report)
    assert hinted == "../skel/order-detail.yaml"
    # 照着提示写进（通过符号链接打开的）suite，能读到这个规则文件
    (tmp_path / "link" / "v.yaml").write_text(
        suite_text(req("order-detail", "/v1/orders/10086", extra=f"    rules_file: {hinted}\n"))
    )
    assert service.validate_suite_sync(tmp_path / "link" / "v.yaml", env={}).ok


def test_hint_falls_back_to_absolute_path(tmp_path, snap, monkeypatch):
    import os

    def other_drive(*args):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)
    report = _shape(tmp_path, snap, write=tmp_path / "skel")
    assert report.exit_code == 0
    assert _hinted_path(report) == str((tmp_path / "skel" / "order-detail.yaml").absolute())
