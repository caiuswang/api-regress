"""报告渲染：HTML、Markdown、JUnit XML。只依赖 core，作为 `Reporter` 传给 `core.service`，由 service 写文件。"""

from __future__ import annotations

from collections.abc import Iterable

from api_regress.core.reportdata import Reporter
from api_regress.core.rundir import REPORT_FILES
from api_regress.report import html, junit, md

FORMATS: dict[str, Reporter] = {
    "html": Reporter(REPORT_FILES["html"], lambda data: html.render(data).encode("utf-8")),
    "md": Reporter(REPORT_FILES["md"], lambda data: md.render(data).encode("utf-8")),
    "junit": Reporter(REPORT_FILES["junit"], junit.render),
}
NONE = "none"
"""`--format none`：只输出 result.json。"""


def reporters(formats: Iterable[str]) -> list[Reporter]:
    """按名字取报告，保持 FORMATS 的顺序，重复的只算一次。"""
    wanted = set(formats)
    return [reporter for name, reporter in FORMATS.items() if name in wanted]
