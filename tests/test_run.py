"""run 的集成测试：用 httpx.MockTransport 模拟两侧服务，mock 返回固定内容。"""

from __future__ import annotations

import asyncio
import json
import time
import threading
from pathlib import Path

import httpx
import pytest
import yaml

from api_regress.core import executor, service
from api_regress.core.result import EXIT_RULES, ExitState, exit_code
from api_regress.core.service import RunOptions
from tests.mock_api import BASE, CAND, JSON, Reply, Server, j, only, req, run, suite_text, types

# ---------- 状态与流程 ----------


def test_identical_responses_no_findings(tmp_path):
    server = Server().both("/x", j({"code": 0, "data": {"id": 1}}))
    report = run(tmp_path, suite_text(req("a")), server)
    assert report.exit_code == 0
    assert only(report).status == "ok" and types(only(report)) == []
    assert server.count("base", "/x") == 2 and server.count("cand", "/x") == 1


def test_status_500_only_status_changed(tmp_path):
    """200 变成 500：只有 status_changed，不再出现 body 和 success 相关的 finding。"""
    server = Server().on("base", "/x", j({"code": 0, "a": 1})).on("cand", "/x", j({"code": 9}, status=500))
    report = run(tmp_path, suite_text(req("a"), defaults='  success: "body.code == 0"\n'), server)
    r = only(report)
    assert r.status == "ok" and types(r) == ["status_changed"]
    f = r.findings[0]
    assert (f.level, f.path, f.base_value, f.cand_value) == ("breaking", "$status", 200, 500)
    assert report.exit_code == 1


@pytest.mark.parametrize(
    ("base", "cand", "hint"),
    [
        (j({}, status=500), j({}), "基线不可用"),
        (j({}, status=401), j({}), "检查基线的 token"),
        (j({}), j({}, status=401), "检查候选的 token"),
        (j({}), j({}, status=429), "限流"),
        (j({}, status=403), j({}), "检查基线的 token"),
    ],
)
def test_inconclusive_statuses(tmp_path, base, cand, hint):
    server = Server().on("base", "/x", base).on("cand", "/x", cand)
    report = run(tmp_path, suite_text(req("a")), server)
    assert only(report).status == "inconclusive" and hint in only(report).reason
    assert types(only(report)) == []
    assert report.exit_code == 4


def test_both_404_without_success_is_ok_with_warning(tmp_path):
    server = Server().both("/x", j({"msg": "not found"}, status=404))
    report = run(tmp_path, suite_text(req("a")), server)
    r = only(report)
    assert r.status == "ok" and [w.code for w in r.warnings] == ["both_non_2xx"]
    assert report.exit_code == 0


def test_json_vs_html_is_breaking(tmp_path):
    html = Reply(body="<html></html>", content_type="text/html")
    server = Server().on("base", "/x", j({"a": 1})).on("cand", "/x", html)
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.status == "ok" and types(r) == ["body_kind_changed"]
    assert (r.findings[0].path, r.findings[0].base_value, r.findings[0].cand_value) == ("$body", "json", "non-json")


def test_both_html_unsupported(tmp_path):
    server = Server().both("/x", Reply(body="<html></html>", content_type="text/html"))
    report = run(tmp_path, suite_text(req("a")), server)
    assert only(report).status == "unsupported" and report.exit_code == 0


def test_unsupported_still_compares_status(tmp_path):
    server = Server().on("base", "/x", Reply(body="<p>", content_type="text/html"))
    server.on("cand", "/x", Reply(status=302, body="<p>", content_type="text/html", headers={"Location": "/y"}))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.status == "unsupported" and types(r) == ["status_changed"]


def test_redirect_not_followed(tmp_path):
    server = Server().both("/x", Reply(status=302, content_type=None, headers={"Location": "/y"}))
    server.both("/y", j({}))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.candidate.status == 302 and server.count("cand", "/y") == 0


def test_connect_error_retried_then_error_others_continue(tmp_path):
    """连接被拒绝：重试 1 次后为 error，其余请求照常执行，退出码 4。"""
    server = Server().both("/ok", j({"a": 1}))
    server.on("base", "/down", j({})).on("cand", "/down", httpx.ConnectError("Connection refused"))
    report = run(tmp_path, suite_text(req("down", "/down") + req("fine", "/ok")), server)
    down = only(report, "down")
    assert down.status == "error" and "候选请求失败" in down.reason and "ConnectError" in down.reason
    assert down.candidate.retries == 1 and server.count("cand", "/down") == 2
    assert only(report, "fine").status == "ok"
    assert report.exit_code == 4


def test_timeout_is_retried(tmp_path):
    server = Server().on("base", "/x", j({"a": 1}))
    server.on("cand", "/x", httpx.ReadTimeout("slow"), j({"a": 1}))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.status == "ok" and r.candidate.retries == 1


def test_status_codes_never_retried(tmp_path):
    server = Server().on("base", "/x", j({})).on("cand", "/x", j({}, status=503))
    run(tmp_path, suite_text(req("a")), server)
    assert server.count("cand", "/x") == 1


def test_baseline_first_failure_is_error_without_second_sample(tmp_path):
    server = Server().on("base", "/x", httpx.ConnectError("x")).on("cand", "/x", j({}))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.status == "error" and "基线请求失败" in r.reason
    assert len(r.baseline) == 1 and server.count("base", "/x") == 2  # 1 次 + 1 次重试


def test_breaking_and_error_exit_1(tmp_path):
    server = Server().on("base", "/x", j({"a": 1})).on("cand", "/x", j({}))
    server.on("base", "/down", httpx.ConnectError("x")).on("cand", "/down", j({}))
    report = run(tmp_path, suite_text(req("a") + req("down", "/down")), server)
    assert report.exit_code == 1


def test_config_error_exit_2(tmp_path):
    text = suite_text(req("a")).replace(f"base_url: {CAND}", "base_url: ${CAND_URL}")
    report = run(tmp_path, text, Server())
    assert report.exit_code == 2 and report.result is None
    assert "CAND_URL" in report.errors[0].message
    assert not (tmp_path / "out").exists()


def test_exit_code_priority():
    """2 > 3 > 1 > 4 > 0。"""
    all_set = ExitState(suite_error=True, internal_error=True, failing=1, incomplete=1)
    assert exit_code(all_set)[0] == 2
    assert exit_code(ExitState(internal_error=True, failing=1, incomplete=1))[0] == 3
    assert exit_code(ExitState(failing=1, incomplete=1))[0] == 1
    assert exit_code(ExitState(incomplete=1))[0] == 4
    assert exit_code(ExitState())[0] == 0
    assert [code for code, _, _ in EXIT_RULES] == [2, 3, 1, 4, 0]


@pytest.mark.parametrize(("fail_on", "code"), [("breaking", 0), ("suspicious", 1), ("info", 1), ("never", 0)])
def test_fail_on(tmp_path, fail_on, code):
    server = Server().on("base", "/x", j({"a": 1})).on("cand", "/x", j({"a": None}))  # nullability: suspicious
    assert run(tmp_path, suite_text(req("a")), server, fail_on=fail_on).exit_code == code


# ---------- success 与 asserts ----------

SUCCESS = '  success: "body.code == 0"\n'


def test_business_error_skips_body(tmp_path):
    server = Server().on("base", "/x", j({"code": 0, "data": {"a": 1}})).on("cand", "/x", j({"code": 500}))
    r = only(run(tmp_path, suite_text(req("a", extra="    asserts:\n      - expr: 'False'\n"), SUCCESS), server))
    assert r.status == "ok" and types(r) == ["business_error"]
    assert r.findings[0].path == "$success" and r.findings[0].level == "breaking"


def test_baseline_business_failure_inconclusive(tmp_path):
    server = Server().on("base", "/x", j({"code": 1})).on("cand", "/x", j({"code": 0}))
    r = only(run(tmp_path, suite_text(req("a"), SUCCESS), server))
    assert r.status == "inconclusive" and "基线业务失败" in r.reason


def test_success_eval_error_is_not_satisfied_with_warning(tmp_path):
    server = Server().on("base", "/x", j({"code": 0})).on("cand", "/x", j({"nocode": 1}))
    r = only(run(tmp_path, suite_text(req("a"), SUCCESS), server))
    assert types(r) == ["business_error"]
    assert "success_eval_error" in [w.code for w in r.warnings]


def test_head_skips_success(tmp_path):
    server = Server().both("/h", Reply(content_type=None))
    r = only(run(tmp_path, suite_text(req("h", "/h", "HEAD"), SUCCESS), server))
    assert r.status == "ok" and r.findings == [] and r.warnings == []


def test_request_success_null_disables(tmp_path):
    server = Server().on("base", "/x", j({"code": 0})).on("cand", "/x", j({"code": 7}))
    r = only(run(tmp_path, suite_text(req("a", extra="    success: null\n"), SUCCESS), server))
    assert r.findings == []


def test_asserts_failed_and_error_others_still_run(tmp_path):
    asserts = (
        "    asserts:\n"
        "      - expr: \"len(cand.body.items) >= len(base.body.items)\"\n"
        "        level: breaking\n"
        "        message: 条目变少\n"
        "      - expr: \"cand.body.nope == 1\"\n"
        "      - expr: \"cand.status == 200\"\n"
        "      - expr: \"cand.body.total == 0.10 + 0.20\"\n"
    )
    server = Server().on("base", "/x", j('{"items": [1, 2], "total": 0.30}'))
    server.on("cand", "/x", j('{"items": [1], "total": 0.30}'))
    r = only(run(tmp_path, suite_text(req("a", extra=asserts)), server))
    asserts_found = [(f.type, f.path, f.level) for f in r.findings if f.type.startswith("assert")]
    assert asserts_found == [("assert_failed", "$assert[0]", "breaking"), ("assert_error", "$assert[1]", "suspicious")]
    assert r.findings[-2].message == "条目变少"
    assert "nope" in r.findings[-1].message


def test_asserts_see_raw_body_ignoring_rules(tmp_path):
    extra = "    rules:\n      - path: secret\n        ignore: true\n    asserts:\n      - expr: \"cand.body.secret == base.body.secret\"\n"
    server = Server().on("base", "/x", j({"secret": 1})).on("cand", "/x", j({"secret": 2}))
    r = only(run(tmp_path, suite_text(req("a", extra=extra)), server))
    assert types(r) == ["assert_failed"]


# ---------- 响应头与 body ----------


def test_compare_headers(tmp_path):
    extra = "    compare_headers: [Content-Type, X-Version, Cache-Control, X-New]\n"
    base = j({}, headers={"X-Version": "1", "Cache-Control": "no-store"}, content_type="application/json; charset=utf-8")
    cand = j({}, headers={"x-version": "2", "X-New": "y"}, content_type="application/json")
    server = Server().on("base", "/x", base).on("cand", "/x", cand)
    r = only(run(tmp_path, suite_text(req("a", extra=extra)), server))
    got = {(f.type, f.path, f.level) for f in r.findings}
    assert got == {
        ("header_changed", "$header.X-Version", "suspicious"),
        ("header_missing", "$header.Cache-Control", "breaking"),
        ("header_changed", "$header.X-New", "suspicious"),
    }


def test_content_type_media_change_is_breaking(tmp_path):
    extra = "    compare_headers: [content-type]\n"
    server = Server().on("base", "/x", j({})).on("cand", "/x", j({}, content_type="application/problem+json"))
    r = only(run(tmp_path, suite_text(req("a", extra=extra)), server))
    assert [(f.type, f.level) for f in r.findings] == [("header_changed", "breaking")]


def test_body_diff_uses_rules_and_decimal(tmp_path):
    defaults = "  rules:\n    - path: \"**.price\"\n      decimal: {}\n"
    server = Server().on("base", "/x", j('{"list": [{"price": 12.50}, {"price": 1.00}]}'))
    server.on("cand", "/x", j('{"list": [{"price": 12.50}, {"price": 1.0}]}'))
    r = only(run(tmp_path, suite_text(req("a"), defaults), server))
    f = r.findings[0]
    assert (f.type, f.path, f.example_index, f.base_value, f.cand_value) == ("scale_changed", "list[*].price", 1, [2], [1, 2])


def test_json_parse_warning(tmp_path):
    server = Server().on("base", "/x", j({"a": 1})).on("cand", "/x", Reply(body="{bad", content_type=JSON))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert types(r) == ["body_kind_changed"] and [w.code for w in r.warnings] == ["json_parse_failed"]


# ---------- 不稳定字段建议 ----------


def test_unstable_suggestions(tmp_path):
    first = {"server_time": 1, "data": {"trace": {"id": "a"}, "list": [1, 2], "keep": 1}}
    second = {"server_time": 2, "data": {"list": [1, 2, 3], "keep": 1}}
    extra = "    rules:\n      - path: data.list\n        array: {mode: positional}\n"
    server = Server().on("base", "/x", j(first), j(second)).on("cand", "/x", j(first))
    r = only(run(tmp_path, suite_text(req("a", extra=extra), "  compare: values\n"), server))
    paths = {s.path: s.reason for s in r.suggestions}
    assert paths == {"server_time": "基线两次的值不同", "data.trace": "只在一次基线响应里出现"}
    for s in r.suggestions:
        assert yaml.safe_load(s.yaml) == [{"path": s.path, "ignore": True}]
    # 只是建议：候选和第 1 个 sample 相同，不受第 2 个 sample 影响；建议的字段不会被自动忽略
    assert r.findings == []


def test_suggestions_not_auto_ignored(tmp_path):
    server = Server().on("base", "/x", j({"t": 1}), j({"t": 2})).on("cand", "/x", j({"t": 3}))
    r = only(run(tmp_path, suite_text(req("a"), "  compare: values\n"), server))
    assert [s.path for s in r.suggestions] == ["t"] and types(r) == ["value_changed"]


def test_suggestion_yaml_quotes_special_paths(tmp_path):
    server = Server().on("base", "/x", j({"a b": 1}), j({"a b": 2})).on("cand", "/x", j({"a b": 1}))
    r = only(run(tmp_path, suite_text(req("a")), server))
    assert r.suggestions[0].path == '["a b"]'
    assert yaml.safe_load(r.suggestions[0].yaml)[0]["path"] == '["a b"]'


def test_second_sample_unavailable(tmp_path):
    server = Server().on("base", "/x", j({"code": 0}), j({"code": 1})).on("cand", "/x", j({"code": 0}))
    r = only(run(tmp_path, suite_text(req("a"), SUCCESS), server))
    assert r.status == "ok" and r.suggestions == []
    assert [w.code for w in r.warnings] == ["second_sample_unavailable"]


# ---------- 本次未命中的规则 ----------


def test_unmatched_rules(tmp_path):
    defaults = (
        "  rules:\n"
        "    - path: \"**.price\"\n      decimal: {}\n"  # 命中（schema 数组元素里）
        "    - path: \"**.nowhere\"\n      ignore: true\n"  # 未命中
    )
    extra_a = "    rules:\n      - path: list[*].sku\n        nullable: true\n      - path: gone\n        ignore: true\n"
    server = Server().both("/a", j({"list": [{"price": 1.5, "sku": "x"}]})).both("/b", j({"other": 1}))
    report = run(tmp_path, suite_text(req("a", "/a", extra=extra_a) + req("b", "/b"), defaults), server)
    got = [(u.scope, u.index, u.request, u.path) for u in report.result.unmatched_rules]
    assert got == [("defaults", 1, None, "**.nowhere"), ("request", 1, "a", "gone")]


def test_unmatched_ignores_requests_without_json(tmp_path):
    defaults = "  rules:\n    - path: x\n      ignore: true\n"
    server = Server().both("/h", Reply(content_type=None))
    report = run(tmp_path, suite_text(req("h", "/h", "HEAD"), defaults), server)
    assert report.result.unmatched_rules == []


# ---------- 选项、事件、取消 ----------


def test_only_and_url_override(tmp_path):
    server = Server().both("/a", j({})).both("/b", j({}))
    server.on("other", "/b", j({"z": 1}))
    report = run(tmp_path, suite_text(req("a", "/a") + req("b", "/b")), server, only=["b"],
                 candidate_url="http://other.test/")  # fmt: skip
    assert [r.name for r in report.result.requests] == ["b"]
    assert only(report).candidate.url == "http://other.test/b"
    assert types(only(report)) == ["field_added"]


def test_only_unknown_name_exit_2(tmp_path):
    report = run(tmp_path, suite_text(req("a")), Server(), only=["nope"])
    assert report.exit_code == 2 and report.errors[0].location == "--only"


def test_bad_override_url_exit_2(tmp_path):
    report = run(tmp_path, suite_text(req("a")), Server(), baseline_url="ftp://x")
    assert report.exit_code == 2 and report.errors[0].location == "--baseline-url"


def test_query_and_header_merge(tmp_path):
    text = suite_text(
        req("a", "/x", extra="    query: {page: \"1\", q: \"a b\"}\n    headers: {X-Req: r, accept: text/plain}\n"),
        "  headers:\n    Accept: application/json\n    X-Def: d\n",
    ).replace(f"base_url: {BASE}\n", f"base_url: {BASE}/api/\n    headers: {{X-Def: target}}\n")
    server = Server().on("base", "/api/x", j({})).on("cand", "/x", j({}))
    run(tmp_path, text, server)
    base_call = next(c for c in server.calls if c.url.host == "base.test")
    assert base_call.url.params["page"] == "1" and base_call.url.params["q"] == "a b"
    assert base_call.headers["x-def"] == "target" and base_call.headers["accept"] == "text/plain"
    assert base_call.headers["x-req"] == "r"


def test_events_and_cancel(tmp_path):
    server = Server().both("/x", j({}))
    suite = tmp_path / "suite.yaml"
    suite.write_text(suite_text(req("a") + req("b")))
    events = []
    report = service.run_sync(suite, RunOptions(out=tmp_path / "o1"), env={}, transport=server.transport,
                              on_event=events.append)  # fmt: skip
    kinds = sorted((e.event, e.name) for e in events)
    assert kinds == [("request_done", "a"), ("request_done", "b"), ("request_started", "a"), ("request_started", "b")]
    cancel = threading.Event()
    cancel.set()
    report = service.run_sync(suite, RunOptions(out=tmp_path / "o2"), env={}, transport=server.transport,
                              cancel_event=cancel)  # fmt: skip
    assert report.result.cancelled and {r.status for r in report.result.requests} == {"error"}
    assert report.exit_code == 4


# ---------- run 目录 ----------


def test_run_dir_layout(tmp_path):
    extra = "    compare_headers: [X-Version]\n"
    base = j({"a": 1}, headers={"X-Version": "1", "Set-Cookie": "s=1"})
    server = Server().on("base", "/x", base).on("cand", "/x", Reply(body=b"\xff\xfe", content_type="application/octet-stream"))
    report = run(tmp_path, suite_text(req("a", extra=extra)), server)
    out = Path(report.out_dir)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "baseline/a.json", "candidate/a.json", "result.json",
        "samples/baseline/a/1.json", "samples/baseline/a/2.json", "samples/candidate/a/1.bin",
    ]  # fmt: skip
    base_file = json.loads((out / "baseline" / "a.json").read_text())
    assert len(base_file["samples"]) == 2 and base_file["fingerprint"] == only(report).fingerprint
    assert base_file["samples"][0]["headers"] == {"content-type": JSON, "x-version": "1"}
    assert (out / "samples" / "baseline" / "a" / "1.json").read_bytes() == b'{\n  "a": 1\n}\n'
    cand_file = json.loads((out / "candidate" / "a.json").read_text())
    assert cand_file["samples"][0]["body_file"] == "1.bin"
    assert (out / "samples" / "candidate" / "a" / "1.bin").read_bytes() == b"\xff\xfe"


def test_out_dir_reuse_rules(tmp_path):
    server = Server().both("/x", j({}))
    text = suite_text(req("a"))
    assert run(tmp_path, text, server).exit_code == 0
    assert run(tmp_path, text, server).exit_code == 0  # 之前的 run 目录可以复用
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "keep.txt").write_text("x")
    report = run(tmp_path, text, server, out=junk)
    assert report.exit_code == 3 and "拒绝写入" in report.exit_reason
    assert (junk / "keep.txt").exists() and not (junk / "result.json").exists()


def test_default_out_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server = Server().both("/x", j({}))
    suite = tmp_path / "suite.yaml"
    suite.write_text(suite_text(req("a")))
    first = service.run_sync(suite, RunOptions(), env={}, transport=server.transport)
    second = service.run_sync(suite, RunOptions(), env={}, transport=server.transport)
    assert Path(first.out_dir).parent == Path("api-regress-out")
    assert first.out_dir != second.out_dir


# ---------- 健壮性：单条出错、取消、总时限、run 目录 ----------


def test_one_request_crash_does_not_abort_run(tmp_path, monkeypatch):
    """单条请求处理时抛了意外的异常：这条记为 error，其他照常，result.json 照常写，退出码 3。"""
    real = executor.evaluate

    def flaky(plan, *args, **kwargs):
        if plan.name == "a":
            raise OverflowError("boom")
        return real(plan, *args, **kwargs)

    monkeypatch.setattr(executor, "evaluate", flaky)
    report = run(tmp_path, suite_text(req("a") + req("b")), Server().both("/x", j({})))
    assert report.exit_code == 3
    assert only(report, "a").status == "error" and "OverflowError" in only(report, "a").reason
    assert only(report, "b").status == "ok"
    saved = json.loads((Path(report.out_dir) / "result.json").read_text())
    assert saved["exit_code"] == 3 and [r["status"] for r in saved["requests"]] == ["error", "ok"]


def test_event_callback_error_does_not_abort_run(tmp_path):
    def on_event(event):
        raise RuntimeError("callback")

    suite = tmp_path / "suite.yaml"
    suite.write_text(suite_text(req("a") + req("b")))
    server = Server().both("/x", j({}))
    report = service.run_sync(suite, RunOptions(out=tmp_path / "out"), env={}, transport=server.transport,
                              on_event=on_event)  # fmt: skip
    assert report.exit_code == 3 and [r.status for r in report.result.requests] == ["ok", "ok"]


def test_cancel_during_run_stops_queued_requests(tmp_path):
    """并发为 1：第一条做完时设置取消，排队中的请求不再发出。"""
    cancel = threading.Event()

    def on_event(event):
        if event.event == "request_done":
            cancel.set()

    suite = tmp_path / "suite.yaml"
    suite.write_text(suite_text(req("a") + req("b") + req("c"), "  concurrency: 1\n"))
    server = Server().both("/x", j({}))
    report = service.run_sync(suite, RunOptions(out=tmp_path / "out"), env={}, transport=server.transport,
                              on_event=on_event, cancel_event=cancel)  # fmt: skip
    assert [r.status for r in report.result.requests] == ["ok", "error", "error"]
    assert report.result.cancelled and report.exit_code == 4
    assert len(server.calls) == 3  # 只有第一条：基线 2 次、候选 1 次


def test_timeout_is_a_total_deadline(tmp_path):
    """响应一点点吐数据时，单次读写都不超时，但总耗时超过 timeout 就算超时。"""

    async def drip():
        for _ in range(50):
            await asyncio.sleep(0.05)
            yield b" "

    async def handle(request):
        if request.url.host.startswith("base"):
            return httpx.Response(200, content=drip(), headers={"Content-Type": JSON})
        return httpx.Response(200, json={})

    suite = tmp_path / "suite.yaml"
    suite.write_text(suite_text(req("a"), "  timeout: 0.3\n").replace("retries: 1", "retries: 0"))
    started = time.monotonic()
    report = service.run_sync(suite, RunOptions(out=tmp_path / "out"), env={}, transport=httpx.MockTransport(handle))
    assert time.monotonic() - started < 2
    result = only(report)
    assert result.status == "error" and result.baseline[0].error == "TimeoutError: 总耗时超过 timeout"


def test_out_dir_checked_before_sending(tmp_path):
    """--out 不能用时一个请求都不发；result.json 不是 api-regress 写的也不算之前的 run 目录。"""
    junk = tmp_path / "junk"
    (junk / "baseline").mkdir(parents=True)
    (junk / "baseline" / "keep.txt").write_text("x")
    (junk / "result.json").write_text('{"name": "something else"}')
    server = Server().both("/x", j({}))
    report = run(tmp_path, suite_text(req("a")), server, out=junk)
    assert report.exit_code == 3 and "拒绝写入" in report.exit_reason and report.result is None
    assert server.calls == [] and (junk / "baseline" / "keep.txt").exists()


def test_write_failure_updates_exit_in_result(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.rundir, "write_result", fail)
    report = run(tmp_path, suite_text(req("a")), Server().both("/x", j({})))
    assert report.exit_code == 3 and "disk full" in report.exit_reason
    assert report.out_dir is None
    assert report.result.exit_code == 3 and report.result.exit_reason == report.exit_reason
