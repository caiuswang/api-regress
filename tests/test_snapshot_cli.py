"""snapshot record / compare / accept 命令端到端：真实的本地 HTTP 服务，检查 stdout、日志和哨兵 token。"""

from __future__ import annotations

import json
import logging

import pytest
from werkzeug import Response

from api_regress.cli import main
from tests.test_run_cli import SENTINEL, _suite

REQUESTS = (
    "  - name: detail\n    method: GET\n    path: /detail\n    compare_headers: [Content-Type]\n"
    "  - name: health\n    method: HEAD\n    path: /health\n"
)


@pytest.fixture(autouse=True)
def _reset_logging():
    logger = logging.getLogger("api_regress")
    level, propagate = logger.level, logger.propagate
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = propagate


def _serve(httpserver, prefix: str, detail: dict, cookie: bool = False):
    headers = {"Set-Cookie": f"sid={SENTINEL}"} if cookie else {}
    httpserver.expect_request(f"{prefix}/detail").respond_with_json(detail, headers=headers)
    httpserver.expect_request(f"{prefix}/health", method="HEAD").respond_with_data("")


def _files_text(*roots) -> str:
    return "".join(p.read_text() for root in roots for p in root.rglob("*") if p.is_file())


def _command(argv: list[str], code: int, capsys) -> list[str]:
    assert main(argv) == code
    return capsys.readouterr().out.splitlines()


LOG_NODES = (
    "record 开始：2 条请求", "写入快照", "record 结束：退出码 0", "读取快照", "compare 结束：退出码 1", "accept 结束：退出码 0",
)  # fmt: skip


def test_record_compare_accept_end_to_end(httpserver, tmp_path, monkeypatch, capsys):
    """S2：record（只用基线变量）→ compare（只用候选变量）→ accept → compare。哨兵 token 不落盘、不进日志。"""
    monkeypatch.setenv("BASE_TOKEN", SENTINEL)
    monkeypatch.setenv("API_KEY", SENTINEL)
    monkeypatch.delenv("CAND_TOKEN", raising=False)
    _serve(httpserver, "/base", {"code": 0, "data": {"a": 1, "gone": 2}}, cookie=True)
    _serve(httpserver, "/cand", {"code": 0, "data": {"a": 1, "new": 3}})
    suite = _suite(tmp_path, httpserver.url_for("/base"), httpserver.url_for("/cand"), REQUESTS)
    snap, out, log_file = tmp_path / "snap", tmp_path / "out", tmp_path / "all.log"
    logs = ["--log-level", "DEBUG", "--log-file", str(log_file)]

    lines = _command(["snapshot", "record", str(suite), "-d", str(snap), *logs], 0, capsys)
    assert lines[0] == "recorded 2 | not recorded 0"
    assert lines[-2:] == [f"snapshot: {snap}", "exit 0: 全部通过"]
    assert not any("/cand" in req.url for req, _ in httpserver.log)

    monkeypatch.delenv("BASE_TOKEN")
    monkeypatch.setenv("CAND_TOKEN", SENTINEL)
    lines = _command(["snapshot", "compare", str(suite), "-d", str(snap), "--out", str(out), *logs], 1, capsys)
    assert lines[1] == "findings: breaking 1 | suspicious 0 | info 1"
    assert lines[-1] == "exit 1: 存在 1 条达到 breaking 级别的 finding"
    assert json.loads((out / "result.json").read_text())["command"] == "compare"

    lines = _command(["snapshot", "accept", str(out), "-d", str(snap), "--only", "detail", *logs], 0, capsys)
    assert lines == ["accepted 1 | rejected 0", f"snapshot: {snap}", "exit 0: 全部通过"]
    _command(["snapshot", "compare", str(suite), "--dir", str(snap), "--out", str(out), *logs], 0, capsys)

    log_text = log_file.read_text()
    assert SENTINEL not in log_text + _files_text(snap, out)
    assert [node for node in LOG_NODES if node not in log_text] == []


def test_record_prints_not_written_and_suggestions(httpserver, tmp_path, monkeypatch, capsys):
    for var in ("BASE_TOKEN", "CAND_TOKEN", "API_KEY"):
        monkeypatch.setenv(var, "t")
    seq = iter([{"code": 0, "t": 1}, {"code": 0, "t": 2}])
    httpserver.expect_request("/base/detail").respond_with_handler(
        lambda request: Response(json.dumps(next(seq)), content_type="application/json")
    )
    httpserver.expect_request("/base/health", method="HEAD").respond_with_data("", status=503)
    suite = _suite(tmp_path, httpserver.url_for("/base"), "http://127.0.0.1:1", REQUESTS)
    assert main(["snapshot", "record", str(suite), "--dir", str(tmp_path / "snap")]) == 4
    out = capsys.readouterr().out
    assert "recorded 1 | not recorded 1" in out
    assert "  health: inconclusive（基线返回 503，基线不可用）" in out
    assert "建议 ignore（1）：\n  detail: t（基线两次的值不同）\n    - path: t\n      ignore: true\n" in out
    assert out.splitlines()[-1] == "exit 4: 1 条请求没有写入快照"


def test_accept_prints_rejected(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    assert main(["snapshot", "accept", str(out), "--dir", str(tmp_path / "snap")]) == 3
    assert "不是 api-regress 的 run 目录" in capsys.readouterr().out


def test_snapshot_option_required(tmp_path, capsys):
    assert main(["snapshot", "record", str(tmp_path / "s.yaml")]) == 2
    assert main(["snapshot", "compare", str(tmp_path / "s.yaml")]) == 2
    assert main(["snapshot", "accept", str(tmp_path)]) == 2


def test_snapshot_commands_only_under_group(capsys):
    """快照相关命令只在 snapshot 命令组下，顶层没有 record / compare / accept。"""
    for name in ("record", "compare", "accept"):
        assert main([name, "--help"]) == 2
    capsys.readouterr()
    assert main(["snapshot", "--help"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ("record", "compare", "accept"))
