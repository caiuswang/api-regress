import logging
import subprocess
import sys
from pathlib import Path

import pytest

from api_regress.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "shop_suite.yaml"
SENTINEL = "SENTINEL-TOKEN-9f3c2a"


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


def test_validate_spec_example(monkeypatch, capsys):
    """spec §12.1 第 1 步：OK, 3 requests，退出码 0。"""
    monkeypatch.setenv("BASE_TOKEN", "b")
    monkeypatch.setenv("CAND_TOKEN", "c")
    assert main(["validate", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "OK, 3 requests"
    assert "warnings" not in out


def test_validate_lists_missing_vars_per_target(monkeypatch, capsys):
    """AC-01：validate 只把缺少的变量列为 warning，并按 target 分开。"""
    monkeypatch.delenv("BASE_TOKEN", raising=False)
    monkeypatch.delenv("CAND_TOKEN", raising=False)
    assert main(["validate", str(FIXTURE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "OK, 3 requests"
    assert lines[1] == "warnings (2):"
    assert "[env_var_missing]" in lines[2] and "baseline 缺少环境变量 BASE_TOKEN" in lines[2]
    assert ":7: targets.baseline.headers.Authorization:" in lines[2]
    assert "candidate 缺少环境变量 CAND_TOKEN" in lines[3]


def test_validate_invalid_suite_exit_2(tmp_path, capsys):
    suite = tmp_path / "s.yaml"
    suite.write_text(FIXTURE.read_text().replace("name: order-list\n    method: GET", "name: order-list\n    method: POST"))
    assert main(["validate", str(suite)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("INVALID, 1 error(s)")
    assert f"{suite}:35: requests[1].method:" in out


def test_validate_rule_warning_has_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BASE_TOKEN", "b")
    monkeypatch.setenv("CAND_TOKEN", "c")
    suite = tmp_path / "s.yaml"
    suite.write_text(FIXTURE.read_text() + "      - path: data.items[0].sku_id\n        nullable: true\n")
    assert main(["validate", str(suite)]) == 0
    out = capsys.readouterr().out
    assert "[index_rule_ineffective]" in out and f"{suite}:51: requests[2].rules[3]:" in out


def test_header_values_never_logged(tmp_path, monkeypatch, capsys):
    """AC-35 的 validate 部分：请求头里的哨兵 token 不出现在 stdout、stderr、日志文件。"""
    monkeypatch.setenv("BASE_TOKEN", SENTINEL)
    monkeypatch.setenv("CAND_TOKEN", SENTINEL)
    log_file = tmp_path / "run.log"
    suite = tmp_path / "s.yaml"
    # 再制造一个和 header 相关的错误，确保错误信息也不带值
    suite.write_text(FIXTURE.read_text().replace("  headers:\n    Accept:", "  headers:\n    X-Num: 5\n    Accept:"))
    assert main(["validate", str(suite), "--log-level", "DEBUG", "--log-file", str(log_file)]) == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert SENTINEL not in log_file.read_text()
    assert "suite 校验失败" in log_file.read_text()


def test_log_file_and_level(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASE_TOKEN", "b")
    monkeypatch.setenv("CAND_TOKEN", "c")
    log_file = tmp_path / "x.log"
    main(["validate", str(FIXTURE), "--log-file", str(log_file)])
    text = log_file.read_text()
    assert "suite 加载完成" in text and "3 条请求" in text
    assert "validate 结束：退出码 0" in text
    main(["validate", str(FIXTURE), "--log-level", "warning", "--log-file", str(log_file)])
    assert "suite 加载完成" not in log_file.read_text()[len(text) :]


def test_missing_file_exit_2(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope.yaml")]) == 2


def test_console_script_entry(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "api_regress.cli", "validate", str(FIXTURE)],
        capture_output=True,
        text=True,
        env={"BASE_TOKEN": "b", "CAND_TOKEN": "c", "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK, 3 requests")


def test_version_and_help(capsys):
    assert main(["--version"]) == 0
    assert "api-regress" in capsys.readouterr().out
    assert main(["validate", "--help"]) == 0
    assert "--log-level" in capsys.readouterr().out


def test_usage_errors_exit_2(capsys):
    assert main(["validate"]) == 2  # 缺少 suite 参数
    assert main(["validate", str(FIXTURE), "--log-level", "LOUD"]) == 2
    assert main(["no-such-command"]) == 2
    assert "Usage" in capsys.readouterr().err


def test_internal_error_exit_3(monkeypatch, capsys):
    from api_regress.core import service

    def boom(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "validate_suite_sync", boom)
    assert main(["validate", str(FIXTURE)]) == 3
    assert "内部错误" in capsys.readouterr().err


def test_unopenable_log_file_exit_3(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BASE_TOKEN", "b")
    monkeypatch.setenv("CAND_TOKEN", "c")
    assert main(["validate", str(FIXTURE), "--log-file", str(tmp_path / "no-dir" / "run.log")]) == 3
    assert "无法打开日志文件" in capsys.readouterr().err


def test_click_exceptions_inside_command_keep_click_semantics(monkeypatch, capsys):
    import click

    from api_regress.core import service

    def usage(path):
        raise click.UsageError("--only 指定的请求不存在")

    def exit4(path):
        raise click.exceptions.Exit(4)

    monkeypatch.setattr(service, "validate_suite_sync", usage)
    assert main(["validate", str(FIXTURE)]) == 2
    assert "--only 指定的请求不存在" in capsys.readouterr().err
    monkeypatch.setattr(service, "validate_suite_sync", exit4)
    assert main(["validate", str(FIXTURE)]) == 4


def test_sentinel_not_leaked_on_success_path(tmp_path, monkeypatch, capsys):
    """AC-35 的 validate 部分（成功路径）：带缺失变量 warning 和规则 warning 时也不泄露。"""
    monkeypatch.setenv("BASE_TOKEN", SENTINEL)
    monkeypatch.delenv("CAND_TOKEN", raising=False)
    log_file = tmp_path / "ok.log"
    suite = tmp_path / "s.yaml"
    suite.write_text(FIXTURE.read_text() + "      - path: data.items[0].sku_id\n        nullable: true\n")
    assert main(["validate", str(suite), "--log-level", "DEBUG", "--log-file", str(log_file)]) == 0
    captured = capsys.readouterr()
    assert "OK, 3 requests" in captured.out and "CAND_TOKEN" in captured.out
    assert SENTINEL not in captured.out + captured.err + log_file.read_text()


def test_yaml_error_does_not_echo_hardcoded_header(tmp_path, capsys):
    suite = tmp_path / "s.yaml"
    suite.write_text(FIXTURE.read_text().replace('"Bearer ${BASE_TOKEN}"', f"Bearer x: {SENTINEL}"))
    assert main(["validate", str(suite)]) == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


def test_validate_with_unverified_fields(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("T", raising=False)
    suite = tmp_path / "s.yaml"
    suite.write_text(FIXTURE.read_text().replace("timeout: 10 ", 'timeout: "${T}" '))
    assert main(["validate", str(suite)]) == 0
    out = capsys.readouterr().out
    assert "OK, 3 requests" in out
    assert "defaults.timeout: 值来自缺失的环境变量" in out
    assert "跳过了规则检查" in out
