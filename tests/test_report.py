"""报告：Markdown / JUnit 快照、XSS 转义、标准解析器读 junit.xml、--format、报告写入失败。

快照在 tests/golden/ 下；报告格式有意变化时用 `UPDATE_GOLDEN=1 uv run pytest tests/test_report.py` 重新生成，再逐行检查 diff。"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from api_regress import report as report_pkg
from api_regress.cli import main
from api_regress.core import service
from api_regress.core.config import Rule
from api_regress.core.reportdata import Reporter, ReportInput, RequestExtras
from api_regress.core.samples import Sample
from api_regress.report import common, html, junit, md, sidediff
from tests.mock_api import Reply, Server, j, req, run, suite_text

GOLDEN = Path(__file__).parent / "golden"
XSS = "<script>alert(1)</script>"

ASSERTS = (
    "    asserts:\n"
    "      - expr: 'cand.body.data.total > 100'\n        message: 总额过低\n"
    "      - expr: 'exists(cand.body, \"data.coupon_amount\")'\n        level: breaking\n"
)
REQUESTS = (
    req("order-detail", "/orders/10086", extra="    headers:\n      X-Trace: t\n" + ASSERTS)
    + req("order-list", "/orders", extra="    query:\n      page: '1'\n")
    + req("xss", "/xss", extra="    rules:\n      - path: data.note\n        on_value_change: breaking\n")
    + req("expired", "/expired")
    + req("down", "/down")
    + req("page", "/page")
    + req("health", "/health", extra="    rules:\n      - path: data.nothing_here\n        ignore: true\n")
    + req("biz", "/biz")
)
DEFAULTS = (
    '  success: "body.code == 0"\n'
    "  headers:\n    Authorization: Bearer secret-token\n"
    "  rules:\n    - path: '**.price'\n      decimal: {}\n"
)


def _server() -> Server:
    server = Server()
    server.on("base", "/orders/10086", j('{"code": 0, "data": {"coupon_amount": "5.00", "total": 12.50}}'))
    server.on("cand", "/orders/10086", j('{"code": 0, "data": {"discount_amount": "5.00", "total": 12.50}}'))
    lists = '{"code": 0, "server_time": %d, "data": {"list": [{"id": 1, "price": %s}, {"id": 2, "price": 3.00}]}}'
    server.on("base", "/orders", j(lists % (1, "12.50")), j(lists % (2, "12.50")))
    server.on("cand", "/orders", j(lists % (3, "12.5")))
    server.on("base", "/xss", j({"code": 0, "data": {"note": "plain"}}))
    server.on("cand", "/xss", j({"code": 0, "data": {"note": XSS}}))
    server.on("base", "/expired", j({"code": 0})).on("cand", "/expired", j({"code": 0}, status=401))
    server.on("base", "/down", j({"code": 0})).on("cand", "/down", httpx.ConnectError("connection refused"))
    server.both("/page", Reply(body=f"<html>{XSS}</html>", content_type="text/html"))
    server.both("/health", j({"code": 0, "data": {"up": True}}))
    server.on("base", "/biz", j({"code": 0})).on("cand", "/biz", j({"code": 1, "msg": "库存服务异常"}))
    return server


FIXED = datetime(2026, 11, 11, 10, 15, 0, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def captured(tmp_path) -> ReportInput:
    """跑一次 mock 服务，拿到报告的输入；时间换成固定值，方便做快照。"""
    box: list[ReportInput] = []

    def capture(data: ReportInput) -> bytes:
        box.append(data)
        return b""

    report = run(tmp_path, suite_text(REQUESTS, defaults=DEFAULTS), _server(), reporters=[Reporter("captured", capture)])
    assert report.exit_code == 1
    data = box[0]
    result = data.result.model_copy(
        update={"suite": "suite.yaml", "started_at": FIXED, "finished_at": FIXED + timedelta(seconds=2)}
    )
    for req_result in result.requests:
        for info in [*req_result.baseline, *([req_result.candidate] if req_result.candidate else [])]:
            info.elapsed_ms = 12.0 if info.elapsed_ms is not None else None
    return ReportInput(result=result, out_dir=data.out_dir, extras=data.extras)


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), f"{name} 和快照不一致；有意修改时用 UPDATE_GOLDEN=1 重新生成"


def test_markdown_snapshot(captured):
    _golden("report.md", md.render(captured))


def test_junit_snapshot(captured):
    _golden("junit.xml", junit.render(captured).decode("utf-8"))


def test_junit_counts_follow_fail_on(captured):
    """标准解析器能读；failure = 有达到 --fail-on 的 finding，error = error / inconclusive，unsupported 记 skipped。"""
    root = ET.fromstring(junit.render(captured))
    suite = root.find("testsuite")
    assert suite is not None
    cases = {case.get("name"): case for case in suite.iter("testcase")}
    outcome = {name: [child.tag for child in case if child.tag != "system-out"] for name, case in cases.items()}
    assert outcome == {
        "order-detail": ["failure"], "order-list": ["failure"], "xss": ["failure"], "expired": ["error"],
        "down": ["error"], "page": ["skipped"], "health": [], "biz": ["failure"],
    }  # fmt: skip
    attrs = {k: suite.get(k) for k in ("tests", "failures", "errors", "skipped")}
    assert attrs == {"tests": "8", "failures": "4", "errors": "2", "skipped": "1"}

    never = ReportInput(captured.result.model_copy(update={"fail_on": "never"}), captured.out_dir, captured.extras)
    suite = ET.fromstring(junit.render(never)).find("testsuite")
    assert suite is not None and (suite.get("failures"), suite.get("errors")) == ("0", "2")


def test_junit_strips_characters_xml_cannot_hold(captured):
    req_result = next(r for r in captured.result.requests if r.name == "xss")
    req_result.findings[0].message = "a\x00b\x1bc"  # 值会经过 json.dumps 转义，message 不会
    root = ET.fromstring(junit.render(captured))
    assert "a�b�c" in ET.tostring(root, encoding="unicode")


def test_html_escapes_response_content(captured):
    """响应里的 <script> 在 finding 表格、body 对照、curl 里都被转义。"""
    page = html.render(captured)
    assert "<script" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.count("<html") == 1 and "&lt;html&gt;" not in page  # unsupported 两侧相同，不出 body 对照


def test_html_has_all_sections(captured):
    page = html.render(captured)
    for text in (
        "退出码 1：存在 5 条达到 breaking 级别的 finding",
        "data.coupon_amount", "data.discount_amount",
        "data.list[*].price（示例下标 0）", "小数位 {2}", "小数位 {1, 2}",
        "curl -sS -i http://cand.test/orders/10086 -H &#39;Authorization: &lt;redacted&gt;&#39; -H &#39;X-Trace: &lt;redacted&gt;&#39;",
        "建议 ignore", "- path: server_time", "本次未命中的规则", "health: rules[0] data.nothing_here",
        "body 对照", "两侧 body 文本相同",
        "对比模式 structure", "rules[0] data.note（on_value_change: breaking）", "defaults.success body.code == 0",
        "总额过低", "asserts[0] cand.body.data.total &gt; 100", "asserts[1] exists(cand.body, &#34;data.coupon_amount&#34;)",
    ):  # fmt: skip
        assert text in page, text
    assert "secret-token" not in page


def test_finding_shows_suite_config(captured):
    """报出 finding 的 suite 配置直接写进报告：assert 的表达式（配了 message 也写）、success 表达式、规则内容；
    没配 message 的 assert 不在类型后面重复表达式。"""
    extras = captured.extras["order-detail"]
    by_path = {f.path: f for f in captured.result.requests[0].findings}
    with_message, bare = by_path["$assert[0]"], by_path["$assert[1]"]
    assert common.rules_text(with_message, extras) == "asserts[0] cand.body.data.total > 100"
    assert common.finding_note(with_message, extras) == "总额过低"
    assert common.finding_note(bare, extras) is None
    biz = next(r for r in captured.result.requests if r.name == "biz")
    assert common.rules_text(biz.findings[0], captured.extras["biz"]) == "defaults.success body.code == 0"


def test_rule_attrs_text():
    rule = Rule(path="data.items", array={"keys": ["sku_id"]}, nullable=True)
    assert common.rule_attrs_text(rule) == "array: {mode: keyed, keys: [sku_id]}, nullable: true"
    assert common.rule_attrs_text(Rule(path="**.price", decimal={})) == "decimal: {}"
    assert common.rule_attrs_text(Rule(path="x", decimal={"allow_string": True})) == "decimal: {allow_string: true}"


def test_body_diff_truncated_when_large():
    big = json.dumps({"rows": [{"i": i, "v": "x" * 50} for i in range(3000)]}).encode()
    samples = {
        "baseline": Sample(url="u", status=200, headers={"content-type": "application/json"}, body=big),
        "candidate": Sample(url="u", status=200, headers={"content-type": "application/json"}, body=big + b" "),
    }
    diff = html.body_diff(RequestExtras(path="/", header_names={}, samples=samples))
    assert diff is not None and diff.note is not None and "body 过大" in diff.note


def test_pretty_json_keeps_scale():
    assert common.pretty_json({"a": Decimal("12.50"), "b": [], "c": [1, {}]}) == (
        '{\n  "a": 12.50,\n  "b": [],\n  "c": [\n    1,\n    {}\n  ]\n}'
    )


def test_markdown_cell_escapes_table_breakers():
    assert md.cell("a|b\n<i>`x`") == "a\\|b &lt;i&gt;\\`x\\`"
    assert md.cell("data.list[*].items[*] **.price a_b") == "data.list[\\*].items[\\*] \\*\\*.price a\\_b"


# ---------- CLI 与写入 ----------


def _suite(tmp_path: Path) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(suite_text(req("a")))
    return path


def test_reporters_by_name():
    assert [r.filename for r in report_pkg.reporters(["junit", "html", "junit"])] == ["report.html", "junit.xml"]


@pytest.mark.parametrize("values", [["pdf"], [""], ["html,,xml"], ["none,md"], ["none", "--format", "junit"]])
def test_cli_rejects_bad_format(tmp_path, capsys, values):
    assert main(["run", str(_suite(tmp_path)), "--format", *values]) == 2
    assert "--format" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "written"),
    [
        ([], ["report.html", "report.md", "junit.xml"]),
        (["--format", "md", "--format", "junit"], ["report.md", "junit.xml"]),
        (["--format", "junit,md", "--format", "md"], ["report.md", "junit.xml"]),
        (["--format", "none"], []),
    ],
)
def test_cli_format_repeat_and_none(tmp_path, monkeypatch, args, written):
    seen: list[list[str]] = []
    real = service.run_sync

    def spy(path, options, **kwargs):
        seen.append([r.filename for r in options.reporters])
        return real(path, options, transport=Server().both("/x", j({"a": 1})).transport)

    monkeypatch.setattr(service, "run_sync", spy)
    out = tmp_path / "out"
    assert main(["run", str(_suite(tmp_path)), "--out", str(out), *args]) == 0
    assert seen == [written]
    assert sorted(p.name for p in out.iterdir() if p.is_file()) == sorted([*written, "result.json"])


def test_only_selected_formats_are_written(tmp_path):
    server = Server().both("/x", j({"a": 1}))
    report = run(tmp_path, suite_text(req("a")), server, reporters=report_pkg.reporters(["md"]))
    out = tmp_path / "out"
    assert report.exit_code == 0 and report.reports == [str(out / "report.md")]
    assert sorted(p.name for p in out.iterdir()) == ["baseline", "candidate", "report.md", "result.json"]


def test_reused_run_dir_drops_stale_reports(tmp_path):
    server = Server().both("/x", j({"a": 1}))
    run(tmp_path, suite_text(req("a")), server, reporters=report_pkg.reporters(["html", "md", "junit"]))
    run(tmp_path, suite_text(req("a")), server, reporters=report_pkg.reporters(["junit"]))
    assert not (tmp_path / "out" / "report.html").exists() and (tmp_path / "out" / "junit.xml").exists()


def test_render_failure_exit_3_and_written_reports_agree(tmp_path):
    """一个报告生成失败：其他报告照写，并且里面的退出码和 result.json、进程退出码一致（都是 3）。"""

    def broken(data: ReportInput) -> bytes:
        raise ValueError("template bug")

    server = Server().both("/x", j({"a": 1}))
    reporters = [Reporter("broken.html", broken), *report_pkg.reporters(["md", "junit"])]
    report = run(tmp_path, suite_text(req("a")), server, reporters=reporters)
    out = tmp_path / "out"
    assert report.exit_code == 3 and "无法生成报告" in report.exit_reason and "template bug" in report.exit_reason
    assert json.loads((out / "result.json").read_text())["exit_code"] == 3
    assert report.reports == [str(out / "report.md"), str(out / "junit.xml")]
    assert "## 汇总  退出码 3：" in (out / "report.md").read_text()
    assert not (out / "broken.html").exists()


def test_write_failure_leaves_no_reports_and_no_result(tmp_path, monkeypatch):
    """写文件失败（磁盘满）：删掉本次写出的报告，不写 result.json，退出码 3。"""
    out = tmp_path / "out"
    real = Path.write_bytes

    def flaky(self, data):
        if self.name == "junit.xml":
            raise OSError("disk full")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    report = run(tmp_path, suite_text(req("a")), Server().both("/x", j({"a": 1})),
                 reporters=report_pkg.reporters(["md", "junit"]))  # fmt: skip
    assert report.exit_code == 3 and "disk full" in report.exit_reason and report.out_dir is None
    assert not (out / "report.md").exists() and not (out / "result.json").exists()


def test_reuse_removes_old_result_first_and_accepts_leftover(tmp_path, monkeypatch):
    """复用 run 目录时先删旧的 result.json：本次中途失败时不会留下旧结果配新响应；这样留下的目录下次还能复用。"""
    server = Server().both("/x", j({"a": 1}))
    run(tmp_path, suite_text(req("a")), server)
    out = tmp_path / "out"
    assert (out / "result.json").exists()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.rundir, "write_result", boom)
    assert run(tmp_path, suite_text(req("a")), server, reporters=report_pkg.reporters(["md"])).exit_code == 3
    assert not (out / "result.json").exists() and (out / "candidate" / "a.json").exists()
    monkeypatch.undo()
    assert run(tmp_path, suite_text(req("a")), server).exit_code == 0
    assert (out / "result.json").exists()


def test_leftover_rule_does_not_take_foreign_files(tmp_path):
    out = tmp_path / "out"
    (out / "baseline").mkdir(parents=True)
    (out / "baseline" / "notes.txt").write_text("x")
    report = run(tmp_path, suite_text(req("a")), Server().both("/x", j({"a": 1})), out=out)
    assert report.exit_code == 3 and (out / "baseline" / "notes.txt").exists()


def test_nested_decimal_keeps_scale_unquoted():
    assert common.compact_json({"price": Decimal("12.50"), "tags": ["a", None, True]}) == (
        '{"price": 12.50, "tags": ["a", null, true]}'
    )


def test_markdown_expands_unsupported_with_reason(captured):
    text = md.render(captured)
    assert "### page  GET /page" in text and "**unsupported**：两侧都不是 JSON" in text
    assert "没有差异：health\n" in text


def test_cli_prints_report_paths(tmp_path, monkeypatch, capsys):
    """真实 CLI 走默认 --format：三种报告都写，stdout 列出路径。用一个不存在的地址，请求记 error。"""
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        suite_text(req("a")).replace("http://base.test", "http://127.0.0.1:9").replace("http://cand.test", "http://127.0.0.1:9")
    )
    out = tmp_path / "out"
    code = main(["run", str(suite), "--out", str(out), "--format", "md,junit"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 4
    assert lines[-2] == f"reports: {out / 'report.md'}, {out / 'junit.xml'}"
    assert re.search(r"error", (out / "report.md").read_text())


# ---------- body 对照 ----------


def _cells(rows):
    return [(left.lineno, str(left.html), left.kind, right.lineno, str(right.html), right.kind) for left, right in rows]


def test_side_diff_pairs_replaced_lines_in_order():
    base = ["{", '  "a": 1,', '  "b": "x",', "}"]
    cand = ["{", '  "a": 2,', '  "c": true,', '  "d": 0', "}"]
    [group] = sidediff.row_groups(base, cand)
    assert _cells(group) == [
        (1, "{", "", 1, "{", ""),
        (2, '  &#34;a&#34;: <span class="hl">1</span>,', "sub", 2, '  &#34;a&#34;: <span class="hl">2</span>,', "add"),
        (3, '  &#34;<span class="hl">b</span>&#34;: <span class="hl">&#34;x&#34;</span>,', "sub",
         3, '  &#34;<span class="hl">c</span>&#34;: <span class="hl">true</span>,', "add"),
        (None, "", "", 4, "  &#34;d&#34;: 0", "add"),
        (4, "}", "", 5, "}", ""),
    ]  # fmt: skip


def test_side_diff_keeps_only_context_around_changes():
    base = [f"line {i}" for i in range(20)]
    cand = [*base[:2], "new", *base[2:17], *base[18:]]
    groups = sidediff.row_groups(base, cand)
    assert [[(left.lineno, right.lineno) for left, right in g] for g in groups] == [
        [(1, 1), (2, 2), (None, 3), (3, 4), (4, 5), (5, 6)],
        [(15, 16), (16, 17), (17, 18), (18, None), (19, 19), (20, 20)],
    ]
    page = str(sidediff.table(base, cand, "基线", "候选"))
    assert page.count('class="skip"') == 1 and "line 9" not in page


def test_side_diff_long_lines_skip_intraline():
    base, cand = ["a" * 300], ["a" * 299 + "b"]
    [[(left, right)]] = sidediff.row_groups(base, cand)
    assert (left.kind, right.kind) == ("sub", "add") and "hl" not in str(left.html + right.html)


def test_side_diff_escapes_and_identical_input():
    page = str(sidediff.table(["<b>&"], ["<i>"], "<基线>", "候选"))
    assert "<b>" not in page and "<i>" not in page and "&lt;" in page and "&amp;" in page and "&lt;基线&gt;" in page
    assert sidediff.row_groups(["x"], ["x"]) == []


def test_side_diff_stays_fast_when_every_line_changes():
    """上千行都变了时不能退化成两两比较（原来的 difflib.HtmlDiff 在这里要 10 秒左右）。"""
    base = [f'    "k{i}": "v-{(i + 1) * 7919 % 1000003:09d}",' for i in range(html.DIFF_MAX_LINES)]
    cand = [f'    "k{i}": "v-{(i + 1) * 104729 % 1000003:09d}",' for i in range(html.DIFF_MAX_LINES)]
    start = time.perf_counter()
    [group] = sidediff.row_groups(base, cand)
    assert time.perf_counter() - start < 1.0
    assert len(group) == html.DIFF_MAX_LINES and all(left.kind == "sub" for left, _ in group)
