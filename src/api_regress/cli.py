"""命令行入口（Click）。只负责解析参数、初始化日志、调用 core.service、打印摘要。"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, get_args

import click

from api_regress import __version__, demo as demo_pkg, report as report_pkg
from api_regress.core import service
from api_regress.core.config import format_where
from api_regress.core.result import FailOn, RequestResult
from api_regress.demo import server as demo_server

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
            click.echo(f"  [{w.code}] {format_where(w.file or report.source, w.line, w.location)} {w.message}")


@cli.command()
@click.argument("suite", type=click.Path(dir_okay=False))
@common_options
def validate(suite: str) -> int:
    """只做静态检查，不发请求。"""
    report = service.validate_suite_sync(suite)
    _print_validation(report)
    log.info("validate 结束：退出码 %d（%s）", report.exit_code, "通过" if report.ok else "suite 有问题")
    return report.exit_code


def _print_issues(report: service.CommandReport) -> None:
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
    if report.reports:
        click.echo(f"reports: {', '.join(report.reports)}")


def _finish(report: service.CommandReport, printer: Callable[[Any], None]) -> int:
    """suite 或参数有问题时打印错误，否则打印摘要；最后一行总是退出码和原因。"""
    if report.errors:
        _print_issues(report)
    else:
        printer(report)
    click.echo(f"exit {report.exit_code}: {report.exit_reason}")
    return report.exit_code


def _print_paired(report: service.RunReport) -> None:
    if report.result is not None:
        _print_run(report)


def _log_file() -> Path | None:
    log_file = click.get_current_context().params.get("log_file")
    return Path(log_file) if log_file else None


_SNAPSHOT = click.option(
    "-d", "--dir", "snapshot", required=True, type=click.Path(file_okay=False, path_type=Path), help="快照目录"
)
_ONLY = click.option("--only", multiple=True, help="只处理指定的请求，可以重复")
_OUT = click.option("--out", type=click.Path(file_okay=False, path_type=Path), help="run 目录，默认 ./api-regress-out/<时间>/")
_FAIL_ON = click.option("--fail-on", type=click.Choice(FAIL_ON), default="breaking", show_default=True)
_SUITE = click.argument("suite", type=click.Path(dir_okay=False))


def _format_problem(names: list[str]) -> str | None:
    allowed = [*report_pkg.FORMATS, report_pkg.NONE]
    if not names or any(name not in allowed for name in names):
        return f"只能是 {', '.join(allowed)}，可以逗号分隔或重复"
    if report_pkg.NONE in names and len(set(names)) > 1:
        return f"{report_pkg.NONE} 不能和其他格式一起用"
    return None


def _parse_formats(ctx: click.Context, param: click.Parameter, values: tuple[str, ...]) -> list[str]:
    """`--format` 可以重复，也可以逗号分隔；不给时全部输出；`none` 只输出 result.json，不能和其他格式一起用。"""
    if not values:
        return list(report_pkg.FORMATS)
    names = [part.strip() for value in values for part in value.split(",")]
    names = [name for name in names if name]
    problem = _format_problem(names)
    if problem:
        raise click.BadParameter(problem)
    return [name for name in names if name != report_pkg.NONE]


_FORMAT = click.option(
    "--format", "formats", multiple=True, callback=_parse_formats, metavar="FORMATS",
    help="报告格式：html、md、junit，逗号分隔或重复，默认全部；none 表示只输出 result.json",
)


@cli.command()
@_SUITE
@_OUT
@_FAIL_ON
@_FORMAT
@click.option("--baseline-url", help="覆盖 suite 里基线的 base_url")
@click.option("--candidate-url", help="覆盖 suite 里候选的 base_url")
@_ONLY
@common_options
def run(suite: str, out: Path | None, fail_on: str, formats: list[str], baseline_url: str | None,
        candidate_url: str | None, only: tuple[str, ...]) -> int:  # fmt: skip
    """两侧都实时请求并对比：基线 2 次、候选 1 次。"""
    options = service.RunOptions(
        out=out, fail_on=fail_on, baseline_url=baseline_url, candidate_url=candidate_url, only=list(only),
        log_file=_log_file(), reporters=report_pkg.reporters(formats),
    )  # fmt: skip
    return _finish(service.run_sync(suite, options), _print_paired)


@cli.group()
def snapshot() -> None:
    """快照：只有一个环境时，发版前录基线（record），发版后和快照对比（compare），差异是预期的就收进快照（accept）。"""


@snapshot.command()
@_SUITE
@_SNAPSHOT
@_OUT
@_FAIL_ON
@_FORMAT
@click.option("--candidate-url", help="覆盖 suite 里候选的 base_url")
@_ONLY
@common_options
def compare(suite: str, snapshot: Path, out: Path | None, fail_on: str, formats: list[str],
            candidate_url: str | None, only: tuple[str, ...]) -> int:  # fmt: skip
    """基线读快照，候选实时请求 1 次并对比。只需要 candidate 的环境变量。"""
    options = service.CompareOptions(
        snapshot=snapshot, out=out, fail_on=fail_on, candidate_url=candidate_url, only=list(only), log_file=_log_file(),
        reporters=report_pkg.reporters(formats),
    )
    return _finish(service.compare_sync(suite, options), _print_paired)


def _print_suggestions(requests: list[RequestResult]) -> None:
    items = [(req.name, s) for req in requests for s in req.suggestions]
    if not items:
        return
    click.echo(f"建议 ignore（{len(items)}）：")
    for name, suggestion in items:
        click.echo(f"  {name}: {suggestion.path}（{suggestion.reason}）")
        click.echo("".join(f"    {line}\n" for line in suggestion.yaml.splitlines()), nl=False)


def _print_record(report: service.RecordReport) -> None:
    missing = report.not_written
    click.echo(f"recorded {len(report.written)} | not recorded {len(missing)}")
    for req in missing:
        click.echo(f"  {req.name}: {req.status}（{req.reason}）")
    warnings = [w for req in report.requests for w in req.warnings]
    for w in warnings:
        click.echo(f"  [{w.code}] {w.request}: {w.message}")
    _print_suggestions(report.requests)
    click.echo(f"snapshot: {report.snapshot}")


@snapshot.command()
@_SUITE
@_SNAPSHOT
@click.option("--clean", is_flag=True, help="先清空快照再录：新快照只包含本次写入的请求")
@click.option("--baseline-url", help="覆盖 suite 里基线的 base_url")
@_ONLY
@common_options
def record(suite: str, snapshot: Path, clean: bool, baseline_url: str | None, only: tuple[str, ...]) -> int:
    """只请求基线（每条 2 次），按请求合并写入快照。只需要 baseline 的环境变量。"""
    options = service.RecordOptions(snapshot=snapshot, clean=clean, baseline_url=baseline_url, only=list(only))
    return _finish(service.record_sync(suite, options), _print_record)


def _print_accept(report: service.AcceptReport) -> None:
    click.echo(f"accepted {len(report.accepted)} | rejected {len(report.rejected)}")
    for item in report.rejected:
        click.echo(f"  {item.name}: 拒绝（{item.reason}）")
    if report.snapshot is not None:
        click.echo(f"snapshot: {report.snapshot}")


@snapshot.command()
@click.argument("run_dir", type=click.Path(file_okay=False, path_type=Path))
@_SNAPSHOT
@_ONLY
@common_options
def accept(run_dir: Path, snapshot: Path, only: tuple[str, ...]) -> int:
    """把 run 目录里的候选响应按请求合并进快照（不存在时新建）。"""
    report = service.accept_sync(run_dir, service.AcceptOptions(snapshot=snapshot, only=list(only)))
    return _finish(report, _print_accept)


def _print_shape(report: service.ShapeReport) -> None:
    for line in report.lines:
        click.echo(line)


@cli.command()
@_SUITE
@click.option(
    "--from", "source", required=True, type=click.Path(path_type=Path), help="样本从哪里读：快照目录或 run 目录"
)
@_ONLY
@click.option("--max-keys", type=int, default=service.DEFAULT_MAX_KEYS, show_default=True, help="一个对象最多列出多少个 key")
@click.option(
    "--write", type=click.Path(file_okay=False, path_type=Path), help="不打印字段树，改为写出规则文件骨架 DIR/<name>.yaml"
)
@common_options
def shape(suite: str, source: Path, only: tuple[str, ...], max_keys: int, write: Path | None) -> int:
    """列出每条请求的字段树和生效的规则（读已有的样本，不发请求）。"""
    options = service.ShapeOptions(source=source, only=list(only), max_keys=max_keys, write=write)
    return _finish(service.shape_sync(suite, options), _print_shape)


def _serve_demo(host: str, port: int) -> int:
    try:
        server = demo_server.make_server(host, port)
    except OSError as exc:
        click.echo(f"demo 服务启动失败：{host}:{port}（{exc.strerror or exc}）", err=True)
        return EXIT_INTERNAL_ERROR
    url = demo_server.base_url(server)
    click.echo(f"demo 服务已启动：{url}（基线 {url}/v1，候选 {url}/v2），Ctrl+C 停止")
    if url != f"http://127.0.0.1:{demo_server.DEFAULT_PORT}":
        click.echo(f"端口不是默认值，运行 suite 前先设置：export DEMO_URL={url}")
    log.info("demo 服务启动：%s", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("demo 服务已停止")
    finally:
        server.server_close()
    return 0


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", type=click.IntRange(0, 65535), default=demo_server.DEFAULT_PORT, show_default=True, help="监听端口，0 表示随机")
@click.option(
    "--write-suites", type=click.Path(file_okay=False, path_type=Path), help="把示例 suite 写到这个目录后退出，不启动服务"
)
@common_options
def demo(host: str, port: int, write_suites: Path | None) -> int:
    """启动内置 demo 服务（固定数据，/v1 基线、/v2 候选），用于不联网试用。"""
    if write_suites is None:
        return _serve_demo(host, port)
    try:
        paths = demo_pkg.write_suites(write_suites)
    except FileExistsError as exc:
        raise click.UsageError(f"文件已存在，不覆盖：{exc}") from None
    for path in paths:
        click.echo(f"wrote {path}")
    return 0


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
