"""命令行入口（Click）。只负责解析参数、初始化日志、调用 core.service、打印摘要。"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, get_args

import click

from api_regress import __version__
from api_regress.core import service
from api_regress.core.config import format_where
from api_regress.core.result import FailOn

log = logging.getLogger("api_regress")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
FAIL_ON = get_args(FailOn)
EXIT_SUITE_ERROR = 2
EXIT_INTERNAL_ERROR = 3


def setup_logging(level: str, log_file: str | None) -> None:
    """日志输出到 stderr，可选同时写文件。重复调用会替换之前的 handler。"""
    root = logging.getLogger("api_regress")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def common_options(func: Callable[..., int]) -> Callable[..., int]:
    """所有子命令共用的参数；初始化日志，并把未捕获的异常记为内部错误（退出码 3）。"""

    @click.option(
        "--log-level", default="INFO", show_default=True, type=click.Choice(LOG_LEVELS, case_sensitive=False)
    )
    @click.option("--log-file", type=click.Path(dir_okay=False, writable=True), help="同时写入日志文件")
    @functools.wraps(func)
    def wrapper(*args: Any, log_level: str, log_file: str | None, **kwargs: Any) -> int:
        try:
            setup_logging(log_level, log_file)
        except OSError as exc:
            click.echo(f"无法打开日志文件 {log_file}：{exc.strerror or exc}", err=True)
            return EXIT_INTERNAL_ERROR
        try:
            return func(*args, **kwargs)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise  # 交给 main 按 Click 的约定处理
        except Exception:
            log.exception("内部错误")
            return EXIT_INTERNAL_ERROR

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="api-regress")
def cli() -> None:
    """对比基线与候选的 JSON API 响应。"""


def _print_validation(report: service.ValidationReport) -> None:
    if report.ok:
        click.echo(f"OK, {report.request_count} requests")
    else:
        click.echo(f"INVALID, {len(report.errors)} error(s)")
        for issue in report.errors:
            click.echo(f"  {issue}")
    if report.warnings:
        click.echo(f"warnings ({len(report.warnings)}):")
        for w in report.warnings:
            click.echo(f"  [{w.code}] {format_where(report.source, w.line, w.location)} {w.message}")


@cli.command()
@click.argument("suite", type=click.Path(dir_okay=False))
@common_options
def validate(suite: str) -> int:
    """只做静态检查，不发请求。"""
    report = service.validate_suite_sync(suite)
    _print_validation(report)
    log.info("validate 结束：退出码 %d（%s）", report.exit_code, "通过" if report.ok else "suite 有问题")
    return report.exit_code


def _print_issues(report: service.RunReport) -> None:
    click.echo(f"INVALID, {len(report.errors)} error(s)")
    for issue in report.errors:
        click.echo(f"  {issue}")


def _print_run(report: service.RunReport) -> None:
    result = report.result
    assert result is not None
    s = result.summary
    click.echo(
        f"requests {s.requests} | ok {s.ok} | error {s.error} | inconclusive {s.inconclusive} | unsupported {s.unsupported}"
    )
    click.echo(f"findings: breaking {s.breaking} | suspicious {s.suspicious} | info {s.info}")
    for req in result.requests:
        if req.status != "ok":
            click.echo(f"  {req.name}: {req.status}（{req.reason}）")
    suggestions = sum(len(req.suggestions) for req in result.requests)
    extras = [f"warnings {s.warnings}", f"建议 ignore {suggestions}", f"本次未命中的规则 {len(result.unmatched_rules)}"]
    click.echo(" | ".join(extras))
    click.echo(f"result: {Path(report.out_dir) / 'result.json'}" if report.out_dir else "result: 未写入")


@cli.command()
@click.argument("suite", type=click.Path(dir_okay=False))
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), help="run 目录，默认 ./api-regress-out/<时间>/")
@click.option("--fail-on", type=click.Choice(FAIL_ON), default="breaking", show_default=True)
@click.option("--baseline-url", help="覆盖 suite 里基线的 base_url")
@click.option("--candidate-url", help="覆盖 suite 里候选的 base_url")
@click.option("--only", multiple=True, help="只处理指定的请求，可以重复")
@common_options
def run(suite: str, out: Path | None, fail_on: str, baseline_url: str | None, candidate_url: str | None,
        only: tuple[str, ...]) -> int:  # fmt: skip
    """两侧都实时请求并对比：基线 2 次、候选 1 次。"""
    log_file = click.get_current_context().params.get("log_file")
    options = service.RunOptions(
        out=out, fail_on=fail_on, baseline_url=baseline_url, candidate_url=candidate_url, only=list(only),
        log_file=Path(log_file) if log_file else None,
    )  # fmt: skip
    report = service.run_sync(suite, options)
    if report.errors:
        _print_issues(report)
    elif report.result is not None:
        _print_run(report)
    click.echo(f"exit {report.exit_code}: {report.exit_reason}")
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """控制台入口。用 standalone_mode=False 拿到子命令返回的退出码。"""
    try:
        # standalone_mode=False 时，Click 把 ctx.exit(code) / --version 的退出码作为返回值
        result = cli.main(args=list(argv) if argv is not None else None, prog_name="api-regress", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return EXIT_SUITE_ERROR
    except click.Abort:
        click.echo("已中止", err=True)
        return EXIT_INTERNAL_ERROR
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
