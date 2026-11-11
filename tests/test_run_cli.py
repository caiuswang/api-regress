"""run 命令端到端：真实的本地 HTTP 服务（pytest-httpserver），检查 stdout、日志、哨兵 token。"""

import logging
import socket
from pathlib import Path

import pytest

from api_regress.cli import main

SENTINEL = "SENTINEL-TOKEN-7d1e4b"


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


def _suite(tmp_path: Path, base_url: str, cand_url: str, requests: str) -> Path:
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "version: 1\n"
        "targets:\n"
        f"  baseline:\n    base_url: {base_url}\n    headers:\n      Authorization: \"Bearer ${{BASE_TOKEN}}\"\n"
        f"  candidate:\n    base_url: {cand_url}\n    headers:\n      Authorization: \"Bearer ${{CAND_TOKEN}}\"\n"
        "defaults:\n  retries: 1\n  timeout: 2\n  headers:\n    X-Api-Key: \"${API_KEY}\"\n"
        f"requests:\n{requests}"
    )
    return suite


REQUESTS = (
    "  - name: detail\n    method: GET\n    path: /detail\n    query: {id: \"1\"}\n    compare_headers: [Content-Type]\n"
    "  - name: health\n    method: HEAD\n    path: /health\n"
)


def _serve(httpserver, prefix: str, detail: dict):
    httpserver.expect_request(f"{prefix}/detail").respond_with_json(detail)
    httpserver.expect_request(f"{prefix}/health", method="HEAD").respond_with_data("")


def test_run_end_to_end_no_header_values_leak(httpserver, tmp_path, monkeypatch, capsys):
    """哨兵 token 放在请求头里：stdout、stderr、日志文件、run 目录里都不出现；INFO 日志包含各关键节点。"""
    for var in ("BASE_TOKEN", "CAND_TOKEN", "API_KEY"):
        monkeypatch.setenv(var, SENTINEL)
    _serve(httpserver, "/base", {"code": 0, "data": {"a": 1, "gone": 2}})
    _serve(httpserver, "/cand", {"code": 0, "data": {"a": 1, "new": 3}})
    suite = _suite(tmp_path, httpserver.url_for("/base"), httpserver.url_for("/cand"), REQUESTS)
    out, log_file = tmp_path / "out", tmp_path / "run.log"
    code = main(["run", str(suite), "--out", str(out), "--log-level", "DEBUG", "--log-file", str(log_file)])
    captured = capsys.readouterr()
    assert code == 1
    lines = captured.out.splitlines()
    assert lines[0] == "requests 2 | ok 2 | error 0 | inconclusive 0 | unsupported 0"
    assert lines[1] == "findings: breaking 1 | suspicious 0 | info 1"
    assert lines[-2] == f"result: {out / 'result.json'}"
    assert lines[-1] == "exit 1: 存在 1 条达到 breaking 级别的 finding"

    # 请求确实带上了 token
    sent = [dict(req.headers) for req, _ in httpserver.log]
    assert all(h["Authorization"] == f"Bearer {SENTINEL}" and h["X-Api-Key"] == SENTINEL for h in sent)

    log_text = log_file.read_text()
    everything = captured.out + captured.err + log_text
    everything += "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert SENTINEL not in everything

    for node in (
        "suite 加载完成",
        "→ detail baseline GET",
        "← detail candidate GET",
        "200，",
        "重试 0 次",
        "detail：ok，2 条 finding",
        "汇总：请求 2",
        f"写入 {out / 'result.json'}",
        "run 结束：退出码 1",
    ):
        assert node in log_text, node
    assert "请求头：Authorization, X-Api-Key" in log_text or "请求头：X-Api-Key, Authorization" in log_text


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connection_refused_is_error_and_retried(httpserver, tmp_path, monkeypatch, capsys):
    for var in ("BASE_TOKEN", "CAND_TOKEN", "API_KEY"):
        monkeypatch.setenv(var, "t")
    _serve(httpserver, "/base", {"a": 1})
    suite = _suite(tmp_path, httpserver.url_for("/base"), f"http://127.0.0.1:{_closed_port()}", REQUESTS)
    assert main(["run", str(suite), "--out", str(tmp_path / "out")]) == 4
    out = capsys.readouterr()
    assert "error 2" in out.out
    assert "detail: error（候选请求失败：ConnectError" in out.out and "已重试 1 次" in out.out
    assert "第 1 次重试" in out.err


def test_run_missing_var_exit_2_with_line(tmp_path, monkeypatch, capsys):
    """run 需要两侧的变量：缺少时返回 2 并指出出错行。"""
    monkeypatch.setenv("BASE_TOKEN", "t")
    monkeypatch.setenv("API_KEY", "t")
    monkeypatch.delenv("CAND_TOKEN", raising=False)
    suite = _suite(tmp_path, "http://127.0.0.1:1", "http://127.0.0.1:1", REQUESTS)
    assert main(["run", str(suite)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("INVALID, 1 error(s)")
    assert f"{suite}:10: targets.candidate.headers.Authorization: 缺少环境变量 CAND_TOKEN" in out
    assert out.splitlines()[-1] == "exit 2: suite 或命令行参数有问题"


def test_run_bad_option_exit_2(tmp_path, capsys):
    assert main(["run", str(tmp_path / "s.yaml"), "--fail-on", "nope"]) == 2


@pytest.mark.parametrize("token", [f"{SENTINEL} ", f"{SENTINEL}\r\n", f"{SENTINEL}\t", f"{SENTINEL}\nX", f"中{SENTINEL}"])
def test_illegal_header_value_does_not_leak(httpserver, tmp_path, monkeypatch, capsys, token):
    """请求头的值有首尾空白、换行或非 ASCII 字符时 httpx 拒绝发送，它的异常消息里带着这个值，不能原样记下来。"""
    monkeypatch.setenv("BASE_TOKEN", token)
    monkeypatch.setenv("CAND_TOKEN", "t")
    monkeypatch.setenv("API_KEY", "k")
    _serve(httpserver, "/cand", {"a": 1})
    suite = _suite(tmp_path, httpserver.url_for("/base"), httpserver.url_for("/cand"), REQUESTS)
    out, log_file = tmp_path / "out", tmp_path / "run.log"
    code = main(["run", str(suite), "--out", str(out), "--log-level", "DEBUG", "--log-file", str(log_file)])
    captured = capsys.readouterr()
    assert code == 4
    assert "detail: error（基线请求失败：" in captured.out
    everything = captured.out + captured.err + log_file.read_text()
    everything += "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert SENTINEL not in everything


def test_log_file_inside_out_dir(httpserver, tmp_path, monkeypatch, capsys):
    """日志文件放在 --out 目录里时，这个目录不算"有无关文件"，照常写入，日志也保留。"""
    for var in ("BASE_TOKEN", "CAND_TOKEN", "API_KEY"):
        monkeypatch.setenv(var, "t")
    _serve(httpserver, "/base", {"a": 1})
    _serve(httpserver, "/cand", {"a": 1})
    suite = _suite(tmp_path, httpserver.url_for("/base"), httpserver.url_for("/cand"), REQUESTS)
    out = tmp_path / "out"
    out.mkdir()
    for _ in range(2):  # 第二次是复用之前的 run 目录
        assert main(["run", str(suite), "--out", str(out), "--log-file", str(out / "run.log")]) == 0
    assert (out / "result.json").is_file() and "run 结束：退出码 0" in (out / "run.log").read_text()
    capsys.readouterr()


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://user:secret@127.0.0.1:1", "不能带用户名或密码"),
        ("http://[::1", "完整地址"),
        ("http://127.0.0.1:65536", "完整地址"),
        ("http://host：8080", "完整地址"),
    ],
)
def test_bad_base_url_exit_2(tmp_path, monkeypatch, capsys, url, message):
    """不合法、或带凭据的 base_url：validate 和 run 都按 suite 有问题处理（退出码 2），不发请求。"""
    for var in ("BASE_TOKEN", "CAND_TOKEN", "API_KEY"):
        monkeypatch.setenv(var, "t")
    suite = _suite(tmp_path, url, "http://127.0.0.1:1", REQUESTS)
    assert main(["validate", str(suite)]) == 2
    assert main(["run", str(suite), "--out", str(tmp_path / "out")]) == 2
    out = capsys.readouterr().out
    assert "targets.baseline.base_url: base_url " in out and message in out
    assert "secret" not in out
    good = _suite(tmp_path, "http://127.0.0.1:1", "http://127.0.0.1:1", REQUESTS)
    assert main(["run", str(good), "--candidate-url", url]) == 2
    out = capsys.readouterr().out
    assert "--candidate-url: " in out and message in out
    assert not (tmp_path / "out").exists()
