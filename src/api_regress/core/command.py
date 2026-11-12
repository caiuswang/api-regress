"""各命令共用的返回值基类和提前结束的方式：suite 或参数有问题（2）、工具自身或文件出错（3）。

service 里的 validate 以外的命令和 shapeflow 都用这里的函数，保证退出码、原因和日志的写法一致。"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from pydantic import BaseModel

from api_regress.core.config import ConfigIssue
from api_regress.core.result import ExitState, exit_code

log = logging.getLogger(__name__)


class CommandReport(BaseModel):
    """各命令返回值的公共部分。suite 或参数有问题时 errors 列出问题。"""

    source: str
    exit_code: int
    exit_reason: str
    errors: list[ConfigIssue] = []


class Stop(Exception):
    """提前结束一个命令：带着已经算好的返回值。"""

    def __init__(self, report: CommandReport):
        self.report = report


def finish_log(command: str, code: int, reason: str) -> None:
    log.info("%s 结束：退出码 %d（%s）", command, code, reason)


def usage_issue(source: str, location: str, message: str) -> ConfigIssue:
    return ConfigIssue(source=source, location=location, message=message)


def only_issues(source: str, names: Iterable[str], only: Iterable[str]) -> list[ConfigIssue]:
    """--only 的 name 必须存在。"""
    known = set(names)
    return [usage_issue(source, "--only", f"没有名为 {name!r} 的请求") for name in only if name not in known]


def suite_error(command: str, source: str, issues: list[ConfigIssue], report: type[CommandReport]) -> Stop:
    code, reason = exit_code(ExitState(suite_error=True))
    for issue in issues:
        log.error("%s", issue)
    finish_log(command, code, f"{reason}，{len(issues)} 个错误")
    return Stop(report(source=source, exit_code=code, exit_reason=reason, errors=issues))


def internal_error(command: str, source: str, detail: str, report: type[CommandReport]) -> Stop:
    code, reason = exit_code(ExitState(internal_error=True))
    reason = f"{reason}：{detail}"
    log.error("%s", detail)
    finish_log(command, code, reason)
    return Stop(report(source=source, exit_code=code, exit_reason=reason))
