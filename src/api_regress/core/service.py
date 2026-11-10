"""对外的编排入口。cli 和以后的界面都只调用这里。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from api_regress.core.config import ConfigError, ConfigIssue, MissingVar, UnverifiedSuite, load_suite
from api_regress.core.models import WarningItem
from api_regress.core.rules import static_warnings

log = logging.getLogger(__name__)

_SIDE_LABEL = {"baseline": "baseline", "candidate": "candidate", "shared": "baseline 和 candidate"}


class ValidationReport(BaseModel):
    source: str
    ok: bool
    request_count: int = 0
    errors: list[ConfigIssue] = []
    warnings: list[WarningItem] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 2


def _missing_var_warnings(missing_vars: list[MissingVar]) -> list[WarningItem]:
    order = {"baseline": 0, "candidate": 1, "shared": 2}
    return [
        WarningItem(
            code="env_var_missing",
            message=f"{_SIDE_LABEL[m.side]} 缺少环境变量 {m.name}",
            location=m.location,
            line=m.line,
        )
        for m in sorted(missing_vars, key=lambda m: order[m.side])
    ]


def _unverified_warnings(exc: UnverifiedSuite) -> list[WarningItem]:
    fields = [
        WarningItem(code="env_var_missing", message=f.message, location=f.location, line=f.line) for f in exc.fields
    ]
    note = WarningItem(code="env_var_missing", message="有字段的值来自缺失的环境变量，本次跳过了规则检查")
    return [*_missing_var_warnings(exc.missing), *fields, note]


def _report(source: str, request_count: int, warnings: list[WarningItem]) -> ValidationReport:
    for w in warnings:
        log.warning("[%s] %s", w.code, w.message)
    return ValidationReport(source=source, ok=True, request_count=request_count, warnings=warnings)


def validate_suite_sync(path: str | Path, env: Mapping[str, str] | None = None) -> ValidationReport:
    """静态检查 suite：语法、字段、路径；缺少的环境变量只作为 warning。"""
    source = str(path)
    try:
        loaded = load_suite(path, sides=(), env=env)
    except ConfigError as exc:
        log.info("suite 校验失败：%s，%d 个错误", source, len(exc.issues))
        return ValidationReport(source=source, ok=False, errors=exc.issues)
    except UnverifiedSuite as exc:
        return _report(source, exc.request_count, _unverified_warnings(exc))
    warnings = _missing_var_warnings(loaded.missing_vars) + static_warnings(loaded.suite, loaded.line_of)
    return _report(source, len(loaded.suite.requests), warnings)


async def validate_suite(path: str | Path, env: Mapping[str, str] | None = None) -> ValidationReport:
    return await asyncio.to_thread(validate_suite_sync, path, env)
