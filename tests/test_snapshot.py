"""快照、record、compare、accept 的集成测试：mock 服务模拟「发版前」和「发版后」两种状态。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from api_regress.core import snapshot
from api_regress.core.samples import SampleFormatError, from_record, parse_side_document
from tests.mock_api import JSON, Reply, Server, accept, compare, j, only, record, req, suite_text, types

OK = {"code": 0, "data": {"id": 1, "amount": "12.50"}}
SUCCESS = '  success: "body.code == 0"\n'


def _manifest(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "snap" / "manifest.json").read_text())


def _entry(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / "snap" / "requests" / f"{name}.json").read_text())


def _record_ok(tmp_path: Path, text: str, body=OK, **opts):
    report = record(tmp_path, text, Server().on("base", "/x", j(body)).on("base", "/y", j(body)), **opts)
    assert report.exit_code == 0, report.exit_reason
    return report


# ---------- 基本往返 ----------


def test_record_then_compare_same_service_no_findings(tmp_path):
    """先 record 再 compare 同一个固定的服务：0 条 finding，退出码 0。"""
    text = suite_text(req("a") + req("b", "/y"), defaults=SUCCESS)
    rec = _record_ok(tmp_path, text)
    assert sorted(rec.written) == ["a", "b"] and rec.not_written == []
    report = compare(tmp_path, text, Server().on("cand", "/x", j(OK)).on("cand", "/y", j(OK)))
    assert report.exit_code == 0
    assert report.result.command == "compare"
    assert [r.status for r in report.result.requests] == ["ok", "ok"]
    assert sum(len(r.findings) for r in report.result.requests) == 0


def test_snapshot_layout_and_manifest(tmp_path):
    text = suite_text(req("a", extra="    compare_headers: [X-Ver]\n"))
    server = Server().on("base", "/x", j(OK, headers={"X-Ver": "1", "Set-Cookie": "s=1", "X-Other": "o"}))
    report = record(tmp_path, text, server)
    assert report.exit_code == 0
    manifest = _manifest(tmp_path)
    assert manifest["schema_version"] == 1 and manifest["fingerprint_version"] == 1
    assert set(manifest["requests"]) == {"a"}
    meta = manifest["requests"]["a"]
    assert meta["source"] == "record" and meta["samples"] == 2 and len(meta["fingerprint"]) == 64
    entry = _entry(tmp_path, "a")
    assert entry["name"] == "a" and entry["fingerprint"] == meta["fingerprint"]
    sample = entry["samples"][0]
    assert set(sample["headers"]) == {"content-type", "x-ver"}  # 只保存 Content-Type 和 compare_headers
    assert json.loads(sample["body"]) == OK
    assert {"status", "url", "elapsed_ms", "retries"} <= set(sample)


def test_record_keeps_raw_body_and_non_utf8_as_base64(tmp_path):
    raw = b'{"price": 12.50}'
    binary = b"\xff\xfe\x00"
    server = Server().on("base", "/x", Reply(body=raw)).on("base", "/y", Reply(body=binary, content_type="application/octet-stream"))
    report = record(tmp_path, suite_text(req("a") + req("b", "/y")), server)
    assert report.exit_code == 0
    assert _entry(tmp_path, "a")["samples"][0]["body"] == raw.decode()  # 原文，保留 12.50 的末尾 0
    b = _entry(tmp_path, "b")["samples"][0]
    assert "body" not in b and parse_side_document(_entry(tmp_path, "b")).samples[0].body == binary


def test_compare_uses_snapshot_suggestions(tmp_path):
    """快照里有 2 个 sample：compare 照常给出不稳定字段建议。"""
    text = suite_text(req("a"))
    record(tmp_path, text, Server().on("base", "/x", j({"t": 1, "x": 1}), j({"t": 2, "x": 1})))
    report = compare(tmp_path, text, Server().on("cand", "/x", j({"t": 3, "x": 1})))
    assert [s.path for s in only(report).suggestions] == ["t"]


# ---------- 只用一侧（环境变量） ----------


def _env_suite(requests: str) -> str:
    targets = (
        "version: 1\ntargets:\n"
        "  baseline:\n    base_url: http://base.test\n    headers: {Authorization: \"${BASE_TOKEN}\"}\n"
        "  candidate:\n    base_url: http://cand.test\n    headers: {Authorization: \"${CAND_TOKEN}\"}\n"
    )
    return targets + f"requests:\n{requests}"


def test_record_only_needs_baseline_and_compare_only_candidate(tmp_path):
    text = _env_suite(req("a"))
    server = Server().both("/x", j(OK))
    rec = record(tmp_path, text, server, env={"BASE_TOKEN": "b"})
    assert rec.exit_code == 0 and server.count("cand", "/x") == 0 and server.count("base", "/x") == 2
    server = Server().both("/x", j(OK))
    report = compare(tmp_path, text, server, env={"CAND_TOKEN": "c"})
    assert report.exit_code == 0 and server.count("base", "/x") == 0 and server.count("cand", "/x") == 1


def test_record_missing_baseline_var_is_suite_error(tmp_path):
    report = record(tmp_path, _env_suite(req("a")), Server(), env={"CAND_TOKEN": "c"})
    assert report.exit_code == 2 and any("BASE_TOKEN" in e.message for e in report.errors)
    assert not (tmp_path / "snap").exists()


def test_compare_missing_candidate_var_is_suite_error(tmp_path):
    report = compare(tmp_path, _env_suite(req("a")), Server(), env={"BASE_TOKEN": "b"})
    assert report.exit_code == 2 and any("CAND_TOKEN" in e.message for e in report.errors)


# ---------- 指纹、新增请求、accept ----------


def _with_query(name: str, value: str) -> str:
    return req(name, extra=f'    query: {{page: "{value}"}}\n')


def test_query_change_inconclusive_then_accept_then_clean(tmp_path):
    """改了 query：compare 时 inconclusive；accept 后再 compare，0 条 finding。"""
    _record_ok(tmp_path, suite_text(_with_query("a", "1")))
    changed = suite_text(_with_query("a", "2"))
    report = compare(tmp_path, changed, Server().on("cand", "/x", j(OK)))
    r = only(report)
    assert r.status == "inconclusive" and r.snapshot_problem == "fingerprint_mismatch"
    assert "指纹不一致" in r.reason and "record 或 accept" in r.reason
    assert report.exit_code == 4
    assert (tmp_path / "out" / "candidate" / "a.json").is_file()
    acc = accept(tmp_path)
    assert acc.exit_code == 0 and acc.accepted == ["a"]
    assert _manifest(tmp_path)["requests"]["a"]["source"] == "accept"
    assert _manifest(tmp_path)["requests"]["a"]["samples"] == 1
    report = compare(tmp_path / "again", changed, Server().on("cand", "/x", j(OK)), snapshot=tmp_path / "snap")
    assert report.exit_code == 0 and types(only(report)) == []


def test_new_request_missing_then_accept(tmp_path):
    _record_ok(tmp_path, suite_text(req("a")))
    text = suite_text(req("a") + req("b", "/y"))
    server = Server().on("cand", "/x", j(OK)).on("cand", "/y", j(OK))
    report = compare(tmp_path, text, server)
    b = only(report, "b")
    assert b.status == "inconclusive" and b.snapshot_problem == "missing" and b.baseline == []
    assert "快照里没有这条请求" in b.reason
    assert only(report, "a").status == "ok" and report.exit_code == 4
    assert accept(tmp_path, only=["b"]).accepted == ["b"]
    server = Server().on("cand", "/x", j(OK)).on("cand", "/y", j(OK))
    report = compare(tmp_path / "again", text, server, snapshot=tmp_path / "snap")
    assert report.exit_code == 0 and sum(len(r.findings) for r in report.result.requests) == 0


def test_compare_without_snapshot_dir_everything_missing(tmp_path):
    report = compare(tmp_path, suite_text(req("a")), Server().on("cand", "/x", j(OK)), snapshot=tmp_path / "nope")
    assert only(report).snapshot_problem == "missing" and report.exit_code == 4
    assert accept(tmp_path, snapshot=tmp_path / "nope").exit_code == 0  # 快照不存在时新建
    assert (tmp_path / "nope" / "manifest.json").is_file()


def test_compare_missing_snapshot_candidate_error_is_error(tmp_path):
    server = Server().on("cand", "/x", httpx.ConnectError("refused"))
    report = compare(tmp_path, suite_text(req("a")), server)
    assert only(report).status == "error" and "候选请求失败" in only(report).reason


def test_fingerprint_ignores_target_headers_and_base_url(tmp_path):
    """target 级的 headers 和 base_url 不参与指纹：换环境、换 token 后 compare 照常。"""
    rec = record(tmp_path, _env_suite(req("a")), Server().both("/x", j(OK)), env={"BASE_TOKEN": "x"})
    assert rec.exit_code == 0
    report = compare(tmp_path, _env_suite(req("a")), Server().both("/x", j(OK)), env={"CAND_TOKEN": "y"})
    assert only(report).status == "ok"


def test_request_header_name_change_changes_fingerprint(tmp_path):
    _record_ok(tmp_path, suite_text(req("a")))
    text = suite_text(req("a", extra="    headers: {X-Debug: \"1\"}\n"))
    assert only(compare(tmp_path, text, Server().on("cand", "/x", j(OK)))).snapshot_problem == "fingerprint_mismatch"


def test_fingerprint_version_change_is_mismatch(tmp_path):
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    path = tmp_path / "snap" / "manifest.json"
    data = json.loads(path.read_text())
    data["fingerprint_version"] = 0
    path.write_text(json.dumps(data))
    assert only(compare(tmp_path, text, Server().on("cand", "/x", j(OK)))).snapshot_problem == "fingerprint_mismatch"


# ---------- record 的合并、失败、--clean ----------


def test_record_only_updates_only_that_request(tmp_path):
    text = suite_text(req("a") + req("b", "/y"))
    _record_ok(tmp_path, text)
    before_b = (tmp_path / "snap" / "requests" / "b.json").read_bytes()
    meta_b = _manifest(tmp_path)["requests"]["b"]
    server = Server().on("base", "/x", j({"code": 0, "new": 1}))
    report = record(tmp_path, text, server, only=["a"])
    assert report.exit_code == 0 and report.written == ["a"] and server.count("base", "/y") == 0
    assert (tmp_path / "snap" / "requests" / "b.json").read_bytes() == before_b
    assert _manifest(tmp_path)["requests"]["b"] == meta_b
    assert json.loads(_entry(tmp_path, "a")["samples"][0]["body"]) == {"code": 0, "new": 1}


@pytest.mark.parametrize(
    ("reply", "hint"),
    [
        (httpx.ConnectError("refused"), "基线请求失败"),
        (j({}, status=401), "检查基线的 token"),
        (j({}, status=503), "基线不可用"),
        (j({"code": 9}), "基线不满足 success"),
    ],
)
def test_record_failure_keeps_old_sample(tmp_path, reply, hint):
    text = suite_text(req("a") + req("b", "/y"), defaults=SUCCESS)
    _record_ok(tmp_path, text)
    before = (tmp_path / "snap" / "requests" / "a.json").read_bytes()
    report = record(tmp_path, text, Server().on("base", "/x", reply).on("base", "/y", j({"code": 0, "v": 2})))
    assert report.exit_code == 4 and "1 条请求没有写入快照" in report.exit_reason
    assert report.written == ["b"]
    [missing] = report.not_written
    assert missing.name == "a" and hint in missing.reason
    assert (tmp_path / "snap" / "requests" / "a.json").read_bytes() == before
    assert json.loads(_entry(tmp_path, "b")["samples"][0]["body"]) == {"code": 0, "v": 2}


def test_record_all_failed_without_snapshot_writes_nothing(tmp_path):
    report = record(tmp_path, suite_text(req("a")), Server().on("base", "/x", j({}, status=500)))
    assert report.exit_code == 4 and not (tmp_path / "snap").exists()


def test_record_unsupported_and_head_are_written(tmp_path):
    text = suite_text(req("page") + req("health", "/h", method="HEAD"))
    server = Server().on("base", "/x", Reply(body="<html/>", content_type="text/html")).on("base", "/h", Reply(content_type=None))
    report = record(tmp_path, text, server)
    assert report.exit_code == 0 and sorted(report.written) == ["health", "page"]
    assert [r.status for r in report.requests] == ["unsupported", "ok"]


def test_record_second_sample_unusable_keeps_one(tmp_path):
    text = suite_text(req("a"), defaults=SUCCESS)
    report = record(tmp_path, text, Server().on("base", "/x", j({"code": 0}), j({"code": 1})))
    assert report.exit_code == 0 and report.written == ["a"]
    assert [w.code for w in report.requests[0].warnings] == ["second_sample_unavailable"]
    assert len(_entry(tmp_path, "a")["samples"]) == 1


def test_record_suggestions(tmp_path):
    body = lambda t: {"code": 0, "data": {"server_time": t, "list": [{"id": 1}], "extra": {"x": 1} if t == 1 else None}}
    server = Server().on("base", "/x", j(body(1)), j({"code": 0, "data": {"server_time": 2, "list": [{"id": 1}, {"id": 2}]}}))
    report = record(tmp_path, suite_text(req("a")), server)
    paths = {s.path: s.reason for s in report.requests[0].suggestions}
    assert paths == {"data.server_time": "基线两次的值不同", "data.extra": "只在一次基线响应里出现"}  # 数组元素增减不计入


def test_record_clean_drops_other_requests(tmp_path):
    _record_ok(tmp_path, suite_text(req("a") + req("b", "/y")))
    report = record(tmp_path, suite_text(req("a")), Server().on("base", "/x", j(OK)), clean=True)
    assert report.exit_code == 0
    assert set(_manifest(tmp_path)["requests"]) == {"a"}
    assert not (tmp_path / "snap" / "requests" / "b.json").exists()


def test_record_keeps_requests_no_longer_in_suite(tmp_path):
    _record_ok(tmp_path, suite_text(req("a") + req("b", "/y")))
    record(tmp_path, suite_text(req("a")), Server().on("base", "/x", j(OK)))
    assert set(_manifest(tmp_path)["requests"]) == {"a", "b"}


# ---------- 快照目录的保护、版本、损坏 ----------


def test_snapshot_dir_not_ours_refused_before_requests(tmp_path):
    target = tmp_path / "snap"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    server = Server().both("/x", j(OK))
    for report in (
        record(tmp_path, suite_text(req("a")), server),
        record(tmp_path, suite_text(req("a")), server, clean=True),
        accept(tmp_path, run_dir=_a_run_dir(tmp_path)),
    ):
        assert report.exit_code == 3 and "不是快照目录" in report.exit_reason
    assert server.count("base", "/x") == 0
    assert (target / "keep.txt").read_text() == "mine"
    report = compare(tmp_path, suite_text(req("a")), server)
    assert report.exit_code == 3 and server.count("cand", "/x") == 0


def _a_run_dir(tmp_path: Path) -> Path:
    report = compare(tmp_path / "rd", suite_text(req("a")), Server().on("cand", "/x", j(OK)), snapshot=tmp_path / "rd" / "s")
    return Path(report.out_dir)


def test_incompatible_manifest_version_is_exit_2_but_clean_rerecords(tmp_path):
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    path = tmp_path / "snap" / "manifest.json"
    data = json.loads(path.read_text())
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    server = Server().both("/x", j(OK))
    assert compare(tmp_path, text, server).exit_code == 2
    rec = record(tmp_path, text, server)
    assert rec.exit_code == 2 and "schema_version" in rec.errors[0].message
    assert server.count("base", "/x") == 0 and server.count("cand", "/x") == 0
    assert record(tmp_path, text, server, clean=True).exit_code == 0
    assert _manifest(tmp_path)["schema_version"] == 1


@pytest.mark.parametrize(
    ("damage", "record_code"),
    [
        (lambda snap: (snap / "manifest.json").write_text("{not json"), 3),
        (lambda snap: (snap / "requests" / "a.json").unlink(), 3),
        # 请求文件的内容坏了：record 不预先解析（没变的文件按字节复制），这次重录 a 正好把它换掉
        (lambda snap: (snap / "requests" / "a.json").write_text('{"name": "a", "samples": "x"}'), 0),
        (
            lambda snap: (snap / "requests" / "a.json").write_text(
                '{"name": "a", "fingerprint": "f", "fingerprint_version": 1, "samples": [{"url": "u", "retries": "x"}]}'
            ),
            0,
        ),
    ],
)
def test_corrupt_snapshot(tmp_path, damage, record_code):
    """compare 读到损坏的快照：退出码 3，不发请求。record 只检查 manifest 和文件是否存在。"""
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    damage(tmp_path / "snap")
    server = Server().both("/x", j(OK))
    report = compare(tmp_path, text, server)
    assert report.exit_code == 3 and "快照损坏" in report.exit_reason and server.count("cand", "/x") == 0
    assert record(tmp_path, text, server).exit_code == record_code
    assert record(tmp_path, text, server, clean=True).exit_code == 0  # --clean 可以重录损坏的快照
    assert compare(tmp_path, text, Server().both("/x", j(OK))).exit_code == 0


def test_record_only_copies_unchanged_files_byte_for_byte(tmp_path):
    """没变的请求文件不解析、不重新序列化，按字节原样复制。"""
    text = suite_text(req("a") + req("b", "/y"))
    _record_ok(tmp_path, text)
    path = tmp_path / "snap" / "requests" / "b.json"
    path.write_bytes(path.read_bytes() + b"\n\n")  # 格式上的改动，内容仍合法
    assert record(tmp_path, text, Server().on("base", "/x", j(OK)), only=["a"]).exit_code == 0
    assert path.read_bytes().endswith(b"}\n\n\n")


# ---------- 原子写入与残留 ----------


def test_write_failure_leaves_old_snapshot_intact(tmp_path, monkeypatch):
    text = suite_text(req("a") + req("b", "/y"))
    _record_ok(tmp_path, text)
    before = {p.name: p.read_bytes() for p in (tmp_path / "snap").rglob("*.json")}
    real = snapshot.side_document
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot, "side_document", failing)
    report = record(tmp_path, text, Server().on("base", "/x", j({"v": 1})).on("base", "/y", j({"v": 2})))
    assert report.exit_code == 3 and "无法写入快照" in report.exit_reason
    assert {p.name: p.read_bytes() for p in (tmp_path / "snap").rglob("*.json")} == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".snap")) == []


def test_interrupted_swap_compare_reads_old_record_restores(tmp_path):
    """中断在 snap → snap.old 之后、新目录 → snap 之前：compare 只读 snap.old、什么都不动；record 时恢复。"""
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    snap = tmp_path / "snap"
    snap.rename(tmp_path / "snap.old")
    (tmp_path / ".snap.tmp-abc").mkdir()
    report = compare(tmp_path, text, Server().on("cand", "/x", j(OK)))
    assert report.exit_code == 0 and only(report).status == "ok"
    assert not snap.exists() and (tmp_path / "snap.old").is_dir() and (tmp_path / ".snap.tmp-abc").is_dir()
    _record_ok(tmp_path, text)
    assert snap.is_dir() and not (tmp_path / "snap.old").exists() and not (tmp_path / ".snap.tmp-abc").exists()


def test_compare_is_read_only(tmp_path):
    """compare 不清理残留：另一个进程正在写的临时目录、旧快照都不能被它删掉。"""
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    stale = tmp_path / "snap.old"
    stale.mkdir()
    (stale / "manifest.json").write_text("{}")
    (tmp_path / ".snap.tmp-busy").mkdir()
    before = sorted(str(p) for p in tmp_path.rglob("*") if "snap" in str(p))
    assert compare(tmp_path, text, Server().on("cand", "/x", j(OK))).exit_code == 0
    assert sorted(str(p) for p in tmp_path.rglob("*") if "snap" in str(p)) == before


def test_record_clean_with_nothing_recorded_keeps_snapshot(tmp_path, caplog):
    """--clean 但基线全部失败（例如 token 过期）：不能把好的快照清空。"""
    text = suite_text(req("a") + req("b", "/y"))
    _record_ok(tmp_path, text)
    before = {p: p.read_bytes() for p in (tmp_path / "snap").rglob("*") if p.is_file()}
    server = Server().on("base", "/x", j({}, status=401)).on("base", "/y", httpx.ConnectError("refused"))
    report = record(tmp_path, text, server, clean=True)
    assert report.exit_code == 4 and report.written == []
    assert {p: p.read_bytes() for p in (tmp_path / "snap").rglob("*") if p.is_file()} == before
    assert "--clean 不生效" in caplog.text


def test_recover_drops_stale_old(tmp_path):
    _record_ok(tmp_path, suite_text(req("a")))
    stale = tmp_path / "snap.old"
    stale.mkdir()
    (stale / "manifest.json").write_text("{}")
    _record_ok(tmp_path, suite_text(req("a")))
    assert not stale.exists() and (tmp_path / "snap" / "manifest.json").is_file()


def test_recover_refuses_foreign_old_dir(tmp_path):
    (tmp_path / "snap.old").mkdir()
    (tmp_path / "snap.old" / "x.txt").write_text("mine")
    report = record(tmp_path, suite_text(req("a")), Server().on("base", "/x", j(OK)))
    assert report.exit_code == 3 and "手动处理" in report.exit_reason
    assert (tmp_path / "snap.old" / "x.txt").exists()


def test_rewrite_existing_snapshot_replaces_atomically(tmp_path):
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    _record_ok(tmp_path, text, body={"v": 2})
    assert json.loads(_entry(tmp_path, "a")["samples"][0]["body"]) == {"v": 2}
    assert [p.name for p in tmp_path.iterdir() if "snap" in p.name] == ["snap"]


# ---------- accept 的判定 ----------


def _compare_dir(tmp_path: Path, text: str, cand: Server, base_body=OK) -> Path:
    record(tmp_path, text, Server().on("base", "/x", j(base_body)).on("base", "/y", j(base_body)))
    report = compare(tmp_path, text, cand)
    return Path(report.out_dir)


def test_accept_only_updates_that_request(tmp_path):
    text = suite_text(req("a") + req("b", "/y"))
    new = {"code": 0, "data": {"id": 2}}
    _compare_dir(tmp_path, text, Server().on("cand", "/x", j(new)).on("cand", "/y", j(new)))
    before_b = (tmp_path / "snap" / "requests" / "b.json").read_bytes()
    report = accept(tmp_path, only=["a"])
    assert report.exit_code == 0 and report.accepted == ["a"] and report.rejected == []
    assert json.loads(_entry(tmp_path, "a")["samples"][0]["body"]) == new
    assert (tmp_path / "snap" / "requests" / "b.json").read_bytes() == before_b


def test_accept_rejects_error_exit_4(tmp_path):
    text = suite_text(req("a") + req("b", "/y"))
    _compare_dir(tmp_path, text, Server().on("cand", "/x", httpx.ConnectError("refused")).on("cand", "/y", j(OK)))
    report = accept(tmp_path)
    assert report.exit_code == 4 and report.accepted == ["b"]
    assert [(r.name, r.reason.startswith("请求是 error")) for r in report.rejected] == [("a", True)]


@pytest.mark.parametrize(
    ("cand", "reason"),
    [
        (j({}, status=401), "请求是 inconclusive"),  # 有快照：鉴权导致 inconclusive
        (j({"code": 0}, status=500), "候选返回 500，不能作为基线"),  # ok + status_changed
        (j({"code": 7}), "候选不满足 success"),  # business_error
    ],
)
def test_accept_rejections_with_snapshot(tmp_path, cand, reason):
    text = suite_text(req("a"), defaults=SUCCESS)
    _compare_dir(tmp_path, text, Server().on("cand", "/x", cand), base_body={"code": 0})
    before = (tmp_path / "snap" / "requests" / "a.json").read_bytes()
    report = accept(tmp_path)
    assert report.exit_code == 4 and report.rejected[0].reason.startswith(reason)
    assert (tmp_path / "snap" / "requests" / "a.json").read_bytes() == before


@pytest.mark.parametrize(
    ("cand", "reason"),
    [
        (j({}, status=403), "候选返回 403，检查候选的 token 或限流"),
        (j({}, status=502), "候选返回 502，不能作为基线"),
        (j({"code": 7}), "候选不满足 success"),
    ],
)
def test_accept_checks_candidate_when_snapshot_missing(tmp_path, cand, reason):
    """快照缺失时 compare 只判 inconclusive，accept 还要检查候选本身能不能当基线。"""
    report = compare(tmp_path, suite_text(req("a"), defaults=SUCCESS), Server().on("cand", "/x", cand))
    assert only(report).status == "inconclusive"
    acc = accept(tmp_path)
    assert acc.exit_code == 4 and acc.rejected[0].reason == reason
    assert not (tmp_path / "snap").exists()


def test_accept_candidate_success_recorded_in_result(tmp_path):
    report = compare(tmp_path, suite_text(req("a"), defaults=SUCCESS), Server().on("cand", "/x", j({"code": 0})))
    assert only(report).candidate_success is True
    saved = json.loads((tmp_path / "out" / "result.json").read_text())
    assert saved["requests"][0]["snapshot_problem"] == "missing" and saved["requests"][0]["candidate_success"] is True
    schema = json.loads((Path(__file__).parent.parent / "schema" / "result.schema.json").read_text())
    jsonschema.validate(saved, schema)


def test_accept_from_run_dir(tmp_path):
    """run 的 run 目录也能 accept（两侧都在线时建快照）。"""
    from tests.mock_api import run

    report = run(tmp_path, suite_text(req("a")), Server().both("/x", j(OK)))
    assert accept(tmp_path, run_dir=report.out_dir).accepted == ["a"]


def test_accept_unsupported_is_accepted(tmp_path):
    html = Reply(body="<html/>", content_type="text/html")
    _compare_dir(tmp_path, suite_text(req("a")), Server().on("cand", "/x", html), base_body="<html/>")
    assert accept(tmp_path).accepted == ["a"]


def test_accept_bad_run_dir_exit_3(tmp_path):
    (tmp_path / "out").mkdir()
    report = accept(tmp_path)
    assert report.exit_code == 3 and "不是 api-regress 的 run 目录" in report.exit_reason


def test_accept_unknown_only_exit_2(tmp_path):
    _compare_dir(tmp_path, suite_text(req("a")), Server().on("cand", "/x", j(OK)))
    report = accept(tmp_path, only=["zzz"])
    assert report.exit_code == 2 and report.errors[0].location == "--only"


def test_accept_corrupt_candidate_file_exit_3(tmp_path):
    out = _compare_dir(tmp_path, suite_text(req("a") + req("b", "/y")), Server().on("cand", "/x", j(OK)).on("cand", "/y", j(OK)))
    (out / "candidate" / "a.json").write_text("{bad")
    report = accept(tmp_path)
    assert report.exit_code == 3 and report.accepted == ["b"] and "损坏" in report.rejected[0].reason


# ---------- S2 走查：发版前 record，发版后 compare / accept ----------

S2_SUITE = suite_text(
    "  - name: health\n    method: HEAD\n    path: /health\n"
    "  - name: order-list\n    method: GET\n    path: /v1/orders\n    query: {page: \"1\", size: \"20\"}\n"
    "  - name: order-detail\n    method: GET\n    path: /v1/orders/10086\n    compare_headers: [Content-Type]\n"
    "    rules:\n      - path: data.items\n        array: {keys: [sku_id]}\n      - path: data.coupon\n        nullable: true\n",
    defaults=SUCCESS + '  rules:\n    - path: "**.updated_at"\n      ignore: true\n    - path: "**.price"\n      decimal: {}\n',
)

LIST = '{"code": 0, "data": {"list": [{"id": 1, "price": 12.50}, {"id": 2, "price": 3.00}]}}'
DETAIL = (
    '{"code": 0, "data": {"id": 10086, "coupon_amount": "5.00", "coupon": null, "updated_at": "%s",'
    ' "items": [{"sku_id": "A", "n": 1}]}, "server_time": %d}'
)


def _s2_server(side: str, list_body: str = LIST, detail: str | None = None, status: int = 200) -> Server:
    server = Server().on(side, "/health", Reply(content_type=None, status=status))
    server.on(side, "/v1/orders", j(list_body, status=status))
    details = [j(detail or DETAIL % ("t1", 1), status=status), j(detail or DETAIL % ("t2", 2), status=status)]
    return server.on(side, "/v1/orders/10086", *details)


@pytest.fixture
def s2(tmp_path):
    """步骤 2：发版前 record。3 条都写入，建议 ignore server_time（updated_at 已被 ignore）。"""
    rec = record(tmp_path, S2_SUITE, _s2_server("base"))
    assert rec.exit_code == 0 and sorted(rec.written) == ["health", "order-detail", "order-list"]
    detail = next(r for r in rec.requests if r.name == "order-detail")
    assert [s.path for s in detail.suggestions] == ["server_time"]
    return tmp_path


def _s2_compare(root: Path, step: str, server: Server):
    return compare(root / step, S2_SUITE, server, snapshot=root / "snap")


def test_s2_3a_field_renamed(s2):
    renamed = DETAIL.replace("coupon_amount", "discount_amount") % ("t3", 3)
    report = _s2_compare(s2, "3a", _s2_server("cand", detail=renamed))
    assert {(f.type, f.level, f.path) for f in only(report, "order-detail").findings} == {
        ("field_removed", "breaking", "data.coupon_amount"),
        ("field_added", "info", "data.discount_amount"),
    }
    assert report.exit_code == 1


def test_s2_3b_price_scale(s2):
    report = _s2_compare(s2, "3b", _s2_server("cand", list_body=LIST.replace("12.50", "12.5")))
    [f] = only(report, "order-list").findings
    assert (f.type, f.level, f.generalized_path, f.example_index) == ("scale_changed", "breaking", "data.list[*].price", 0)
    assert report.exit_code == 1


def test_s2_3c_candidate_token_expired(s2):
    report = _s2_compare(s2, "3c", _s2_server("cand", status=401))
    assert [r.status for r in report.result.requests] == ["inconclusive"] * 3
    assert all("检查候选的 token" in r.reason for r in report.result.requests)
    assert report.exit_code == 4


def test_s2_3d_list_order_changed(s2):
    shuffled = '{"code": 0, "data": {"list": [{"id": 2, "price": 3.00}, {"id": 1, "price": 12.50}]}}'
    report = _s2_compare(s2, "3d", _s2_server("cand", list_body=shuffled))
    assert report.exit_code == 0 and sum(len(r.findings) for r in report.result.requests) == 0


def test_s2_3e_accept_intended_change(s2):
    changed = DETAIL.replace('"n": 1', '"n": 1, "gift": true') % ("t4", 4)
    report = _s2_compare(s2, "3e", _s2_server("cand", detail=changed))
    assert types(only(report, "order-detail")) == ["field_added"]
    acc = accept(s2, run_dir=report.out_dir, only=["order-detail"])
    assert acc.exit_code == 0 and acc.accepted == ["order-detail"]
    assert _s2_compare(s2, "3e2", _s2_server("cand", detail=changed)).exit_code == 0


# ---------- sample 的读取（坏数据统一报 SampleFormatError） ----------


@pytest.mark.parametrize(
    "record_",
    [
        None,
        {"url": 1},
        {"url": "u", "status": True},
        {"url": "u", "status": "200"},
        {"url": "u", "retries": "x"},
        {"url": "u", "retries": -1},
        {"url": "u", "elapsed_ms": "1"},
        {"url": "u", "error": 3},
        {"url": "u", "headers": []},
        {"url": "u", "body": 1},
        {"url": "u", "body": "\ud800"},
        {"url": "u", "body_b64": "!!"},
        {"url": "u", "body_b64": 3},
    ],
)
def test_from_record_bad_data_raises_sample_format_error(record_):
    with pytest.raises(SampleFormatError):
        from_record(record_)


def test_from_record_round_trip():
    sample = from_record({"url": "u", "status": 200, "headers": {"Content-Type": JSON}, "body": "{}", "elapsed_ms": 1.5})
    assert (sample.status, sample.content_type, sample.body, sample.elapsed_ms) == (200, JSON, b"{}", 1.5)


# ---------- code review 修复的回归用例 ----------


@pytest.mark.parametrize("cand", [j({"code": 500}, status=201), j({"msg": "x"}, status=404)])
def test_accept_rejects_status_changed_candidate_failing_success(tmp_path, cand):
    """状态码变了，流程在 status_changed 就结束，但仍要求出候选的 success（不产生 warning），accept 据此拒绝。"""
    text = suite_text(req("a"), defaults=SUCCESS)
    _compare_dir(tmp_path, text, Server().on("cand", "/x", cand), base_body={"code": 0})
    r = json.loads((tmp_path / "out" / "result.json").read_text())["requests"][0]
    assert [f["type"] for f in r["findings"]] == ["status_changed"]
    assert r["candidate_success"] is False and r["warnings"] == []
    report = accept(tmp_path)
    assert report.exit_code == 4 and report.rejected[0].reason == "候选不满足 success"


def test_accept_status_changed_candidate_passing_success_accepted(tmp_path):
    text = suite_text(req("a"), defaults=SUCCESS)
    _compare_dir(tmp_path, text, Server().on("cand", "/x", j({"code": 0}, status=201)), base_body={"code": 0})
    assert accept(tmp_path).accepted == ["a"]


@pytest.mark.parametrize("name", ["../../evil", "a/b", ""])
def test_accept_rejects_tampered_request_name(tmp_path, name):
    """run 目录的 result.json 被改过、请求名能穿出目录：整个 run 目录按损坏处理，不读也不写任何文件。"""
    out = _compare_dir(tmp_path, suite_text(req("a")), Server().on("cand", "/x", j(OK)))
    data = json.loads((out / "result.json").read_text())
    data["requests"][0]["name"] = name
    (out / "result.json").write_text(json.dumps(data))
    before = {p: p.read_bytes() for p in (tmp_path / "snap").rglob("*") if p.is_file()}
    report = accept(tmp_path)
    assert report.exit_code == 3 and "不合法的请求名" in report.exit_reason
    assert {p: p.read_bytes() for p in (tmp_path / "snap").rglob("*") if p.is_file()} == before
    assert not (tmp_path / "evil.json").exists()


def test_manifest_with_tampered_request_name_is_corrupt(tmp_path):
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    path = tmp_path / "snap" / "manifest.json"
    data = json.loads(path.read_text())
    data["requests"]["../x"] = data["requests"]["a"]
    path.write_text(json.dumps(data))
    report = compare(tmp_path, text, Server().on("cand", "/x", j(OK)))
    assert report.exit_code == 3 and "不合法的请求名" in report.exit_reason


def test_old_snapshot_cleanup_failure_is_only_warning(tmp_path, monkeypatch, caplog):
    """两次 rename 都成功、只是删 snap.old 失败：新快照已就位，退出码 0；下次运行再清理。"""
    text = suite_text(req("a"))
    _record_ok(tmp_path, text)
    real = snapshot.shutil.rmtree

    def failing(path, *args, **kwargs):
        if str(path).endswith("snap.old"):
            raise OSError(13, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(snapshot.shutil, "rmtree", failing)
    report = _record_ok(tmp_path, text, body={"v": 2})
    assert report.exit_code == 0 and "删除旧快照" in caplog.text
    assert json.loads(_entry(tmp_path, "a")["samples"][0]["body"]) == {"v": 2}
    assert (tmp_path / "snap.old").is_dir()
    monkeypatch.setattr(snapshot.shutil, "rmtree", real)
    _record_ok(tmp_path, text)
    assert not (tmp_path / "snap.old").exists()
