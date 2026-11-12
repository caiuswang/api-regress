"""body 单独成文件：快照和 run 目录的 `samples/` 目录、格式化与例外、校验、旧格式、复用 run 目录。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from api_regress.core import jsonload
from api_regress.core.samples import formatted_json, stored_body
from tests.mock_api import JSON, Reply, Server, accept, compare, j, only, record, req, run, suite_text

TRICKY = '﻿{"amount":12.50,"rate":1.50E1,"big":1E2,"tiny":0.0000001,"neg":-0.00,"name":"\\u4e2d","x":NaN,"n":null}'


def _snap_sample(tmp_path: Path, name: str, index: int = 0) -> tuple[dict, Path | None]:
    entry = json.loads((tmp_path / "snap" / "requests" / f"{name}.json").read_text())
    sample = entry["samples"][index]
    if "body_file" not in sample:
        return sample, None
    return sample, tmp_path / "snap" / "samples" / name / sample["body_file"]


# ---------- 格式化与例外 ----------


@pytest.mark.parametrize(
    ("body", "content_type", "formatted", "suffix"),
    [
        (TRICKY.encode(), JSON, True, "json"),
        (b'{"a":[1,{"b":2}],"c":{}}', "application/problem+json; charset=UTF-8", True, "json"),
        (b'{"a":1,"a":2}', JSON, False, "json"),  # 重复 key
        ('{"name":"中文"}'.encode("gbk"), "application/json; charset=gbk", False, "json"),
        (b'{"a":', JSON, False, "json"),  # 声明为 JSON 但解析失败
        (b"<html>\xe4\xb8\xad</html>", "text/html", False, "txt"),
        (b"\xff\xfe\x00", "application/octet-stream", False, "bin"),
        (b'{"a": 1}', None, False, "txt"),  # 没有 Content-Type：不算 JSON 响应
    ],
)
def test_stored_body(body, content_type, formatted, suffix):
    stored = stored_body(body, content_type)
    assert (stored.formatted, stored.suffix) == (formatted, suffix)
    if not formatted:
        assert stored.data == body


def test_formatted_json_keeps_value_and_warnings():
    data = formatted_json(TRICKY.encode(), JSON)
    assert data is not None and data.endswith(b"\n") and b"\n  " in data
    before, after = jsonload.load_body(TRICKY.encode(), JSON), jsonload.load_body(data, JSON)
    assert repr(after.value) == repr(before.value)
    assert [w.code for w in after.warnings] == [w.code for w in before.warnings] == ["nan_or_infinity"]
    assert b'"amount": 12.50' in data and b'"neg": -0.00' in data and "中".encode() in data


def test_formatting_is_idempotent():
    once = formatted_json(TRICKY.encode(), JSON)
    assert formatted_json(once, JSON) == once


# ---------- 写出的文件 ----------


def test_record_writes_each_kind_of_body(tmp_path):
    gbk = '{"name":"中文"}'.encode("gbk")
    server = (
        Server()
        .on("base", "/json", Reply(body=TRICKY.encode()))
        .on("base", "/dup", Reply(body=b'{"a":1,"a":2}'))
        .on("base", "/gbk", Reply(body=gbk, content_type="application/json; charset=gbk"))
        .on("base", "/html", Reply(body=b"<p>x</p>", content_type="text/html"))
        .on("base", "/bin", Reply(body=b"\xff\x00", content_type="application/octet-stream"))
        .on("base", "/head", Reply())
        .on("base", "/empty", Reply(status=204, content_type=None))
    )
    names = ["json", "dup", "gbk", "html", "bin", "empty"]
    text = suite_text("".join(req(n, f"/{n}") for n in names) + req("head", "/head", "HEAD", "    success: null\n"))
    report = record(tmp_path, text, server)
    assert report.exit_code == 0, report.exit_reason
    expect = {
        "json": ("1.json", True), "dup": ("1.json", False), "gbk": ("1.json", False),
        "html": ("1.txt", False), "bin": ("1.bin", False),
    }  # fmt: skip
    for name, (file, formatted) in expect.items():
        sample, path = _snap_sample(tmp_path, name)
        assert (sample["body_file"], sample["body_formatted"]) == (file, formatted), name
        assert "body" not in sample and "body_b64" not in sample
        assert sample["body_size"] == path.stat().st_size
    assert _snap_sample(tmp_path, "gbk")[1].read_bytes() == gbk
    for name in ("head", "empty"):
        sample, path = _snap_sample(tmp_path, name)
        assert path is None and sample["body_size"] == 0 and not (tmp_path / "snap" / "samples" / name).exists()


def test_network_error_sample_has_no_body_file(tmp_path):
    server = Server().on("base", "/x", httpx.ConnectError("refused")).on("cand", "/x", j({"a": 1}))
    report = run(tmp_path, suite_text(req("a")), server)
    assert only(report).status == "error"
    assert not (tmp_path / "out" / "samples" / "baseline").exists()
    assert (tmp_path / "out" / "samples" / "candidate" / "a" / "1.json").exists()


# ---------- 和存原文对比结果相同 ----------

CASES = {
    "tricky": (TRICKY, TRICKY.replace("12.50", "12.5").replace("-0.00", "-0.0")),
    "dup": ('{"a":1,"a":2,"b":1}', '{"a":2,"b":"1"}'),
    "list": ('{"items":[{"id":1,"p":1.10},{"id":2,"p":2.20}]}', '{"items":[{"id":2,"p":2.2},{"id":1,"p":1.10}]}'),
    "badjson": ('{"a":', '{"a":1}'),
}
RULES = '  rules:\n    - {path: "**.p", decimal: {}}\n    - {path: amount, decimal: {}, on_value_change: info}\n'


def _view(report):
    return [
        (r.name, r.status, [(f.type, f.level, f.path, str(f.base_value), str(f.cand_value)) for f in r.findings],
         sorted((w.code, w.message) for w in r.warnings))
        for r in report.result.requests
    ]  # fmt: skip


def test_compare_from_formatted_snapshot_equals_live_run(tmp_path):
    """同样的基线和候选：run（基线是实时的原始字节）和 compare（基线读格式化后的快照）结果完全相同。"""
    server = Server()
    for name, (base, cand) in CASES.items():
        server.on("base", f"/{name}", Reply(body=base.encode())).on("cand", f"/{name}", Reply(body=cand.encode()))
    text = suite_text("".join(req(n, f"/{n}") for n in CASES), defaults=RULES)
    live = run(tmp_path / "run", text, server)
    assert record(tmp_path, text, server).exit_code == 0
    from_snapshot = compare(tmp_path, text, server)
    assert _view(from_snapshot) == _view(live)
    assert any(r.findings for r in live.result.requests)


# ---------- 原样复制与 accept ----------


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_record_only_copies_other_requests_byte_for_byte(tmp_path):
    text = suite_text(req("a") + req("b", "/y"))
    record(tmp_path, text, Server().on("base", "/x", j({"v": 1})).on("base", "/y", j({"v": 2})))
    before = {k: v for k, v in _tree(tmp_path / "snap").items() if "/b" in "/" + k}
    assert set(before) == {"requests/b.json", "samples/b/1.json", "samples/b/2.json"}
    record(tmp_path, text, Server().on("base", "/x", j({"v": 9})), only=["a"])
    after = _tree(tmp_path / "snap")
    assert {k: after[k] for k in before} == before
    assert json.loads(after["samples/a/1.json"]) == {"v": 9}


def test_accept_writes_candidate_body_byte_for_byte(tmp_path):
    text = suite_text(req("a"))
    server = Server().on("cand", "/x", Reply(body=TRICKY.encode()))
    compare(tmp_path, text, server)  # 快照不存在：inconclusive，候选照样写进 run 目录
    report = accept(tmp_path)
    assert report.exit_code == 0 and report.accepted == ["a"]
    cand = tmp_path / "out" / "samples" / "candidate" / "a" / "1.json"
    assert _snap_sample(tmp_path, "a")[1].read_bytes() == cand.read_bytes()
    assert compare(tmp_path, text, server).exit_code == 0


# ---------- 损坏 ----------


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda path, entry: path.unlink(), "缺少 body 文件"),
        (lambda path, entry: path.write_bytes(path.read_bytes() + b" "), "SHA-256"),
        (lambda path, entry: entry["samples"][0].update(body_file="../../x.json"), "不合法"),
        (lambda path, entry: entry["samples"][0].update(body_file="2.json"), "不合法"),
    ],
)
def test_compare_damaged_body_exit_3(tmp_path, damage, message):
    text = suite_text(req("a"))
    record(tmp_path, text, Server().on("base", "/x", j({"v": 1})))
    entry_path = tmp_path / "snap" / "requests" / "a.json"
    entry = json.loads(entry_path.read_text())
    damage(tmp_path / "snap" / "samples" / "a" / "1.json", entry)
    entry_path.write_text(json.dumps(entry))
    report = compare(tmp_path, text, Server().on("cand", "/x", j({"v": 1})))
    assert report.exit_code == 3 and "快照损坏" in report.exit_reason and message in report.exit_reason


def test_accept_damaged_candidate_body_exit_3(tmp_path):
    text = suite_text(req("a"))
    compare(tmp_path, text, Server().on("cand", "/x", j({"v": 1})))
    (tmp_path / "out" / "samples" / "candidate" / "a" / "1.json").write_text("{}")
    report = accept(tmp_path)
    assert report.exit_code == 3 and report.accepted == []
    assert "候选响应损坏" in report.rejected[0].reason and "SHA-256" in report.rejected[0].reason


# ---------- 旧格式 ----------


def _make_legacy(entry_path: Path) -> None:
    entry = json.loads(entry_path.read_text())
    for sample in entry["samples"]:
        for key in ("body_file", "body_formatted", "body_size", "body_sha256"):
            sample.pop(key, None)
        sample["body"] = '{"v": 1}'
    entry_path.write_text(json.dumps(entry))


def test_legacy_snapshot_needs_clean_record(tmp_path):
    text = suite_text(req("a"))
    record(tmp_path, text, Server().on("base", "/x", j({"v": 1})))
    _make_legacy(tmp_path / "snap" / "requests" / "a.json")
    cand = Server().on("cand", "/x", j({"v": 1}))
    report = compare(tmp_path, text, cand)
    assert report.exit_code == 3 and "旧格式" in report.exit_reason and "--clean" in report.exit_reason
    assert record(tmp_path, text, Server().on("base", "/x", j({"v": 1})), clean=True).exit_code == 0
    assert json.loads((tmp_path / "snap" / "manifest.json").read_text())["schema_version"] == 1
    assert compare(tmp_path, text, cand).exit_code == 0


def test_accept_legacy_run_dir_exit_3(tmp_path):
    compare(tmp_path, suite_text(req("a")), Server().on("cand", "/x", j({"v": 1})))
    _make_legacy(tmp_path / "out" / "candidate" / "a.json")
    report = accept(tmp_path)
    assert report.exit_code == 3 and "旧格式" in report.rejected[0].reason and "重新运行" in report.rejected[0].reason


# ---------- 复用 run 目录 ----------


def test_reused_run_dir_drops_old_bodies(tmp_path):
    server = Server().both("/x", j({"a": 1})).both("/y", j({"b": 1}))
    run(tmp_path, suite_text(req("a") + req("b", "/y")), server)
    assert (tmp_path / "out" / "samples" / "baseline" / "b").is_dir()
    run(tmp_path, suite_text(req("a")), server)
    assert not (tmp_path / "out" / "samples" / "baseline" / "b").exists()


def test_leftover_with_samples_dir_is_reusable(tmp_path):
    out = tmp_path / "out"
    (out / "candidate").mkdir(parents=True)
    (out / "candidate" / "a.json").write_text("{}")
    (out / "samples" / "candidate" / "a").mkdir(parents=True)
    (out / "samples" / "candidate" / "a" / "1.bin").write_bytes(b"\x00")
    report = run(tmp_path, suite_text(req("a")), Server().both("/x", j({"a": 1})), out=out)
    assert report.exit_code == 0
    assert not (out / "samples" / "candidate" / "a" / "1.bin").exists()


def _foreign_samples_dir(out):
    (out / "samples" / "photos").mkdir(parents=True)
    (out / "samples" / "photos" / "cat.jpg").write_bytes(b"x")


def _foreign_body_file(out):
    (out / "samples" / "baseline" / "a").mkdir(parents=True)
    (out / "samples" / "baseline" / "a" / "notes.txt").write_text("x")


def _samples_is_a_file(out):
    out.mkdir()
    (out / "samples").write_text("x")


def _side_is_a_file(out):
    out.mkdir()
    (out / "baseline").write_text("x")


def _report_is_a_dir(out):
    (out / "report.html" / "keep").mkdir(parents=True)


@pytest.mark.parametrize("make", [_foreign_samples_dir, _foreign_body_file, _samples_is_a_file, _side_is_a_file,
                                  _report_is_a_dir])  # fmt: skip
def test_leftover_rule_rejects_lookalikes_before_sending(tmp_path, make):
    """同名但不是 api-regress 写出的样子：拒绝复用，不发请求，也不删任何东西。"""
    out = tmp_path / "out"
    make(out)
    before = sorted(p.relative_to(out) for p in out.rglob("*"))
    server = Server().both("/x", j({"a": 1}))
    report = run(tmp_path, suite_text(req("a")), server, out=out)
    assert report.exit_code == 3 and not server.calls
    assert sorted(p.relative_to(out) for p in out.rglob("*")) == before
