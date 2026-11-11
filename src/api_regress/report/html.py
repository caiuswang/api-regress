"""HTML 报告（`report.html`）：单个文件，不依赖外部资源，不带 JS。

Jinja2 开启 autoescape，响应里的内容一律转义；两侧 body 的对照由 `report/sidediff.py` 生成（它自己会转义），
过大时截断，免得报告太大打不开。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import jinja2
from markupsafe import Markup

from api_regress.core.reportdata import ReportInput, RequestExtras
from api_regress.core.result import RequestResult
from api_regress.report import common, sidediff

VALUE_LIMIT = 300
DIFF_MAX_LINES = 1500
DIFF_MAX_CHARS = 200_000


@dataclass(frozen=True)
class BodyDiff:
    table: Markup | None
    note: str | None


def _clip_body(text: str) -> tuple[list[str], bool]:
    clipped = len(text) > DIFF_MAX_CHARS
    lines = text[:DIFF_MAX_CHARS].splitlines()
    if len(lines) > DIFF_MAX_LINES:
        lines, clipped = lines[:DIFF_MAX_LINES], True
    return lines, clipped


def body_diff(extras: RequestExtras | None) -> BodyDiff | None:
    """两侧都有响应时生成对照；完全相同时只给一句说明。"""
    if extras is None or len(extras.samples) < 2:
        return None
    base, base_clipped = _clip_body(common.body_text(extras.samples["baseline"]))
    cand, cand_clipped = _clip_body(common.body_text(extras.samples["candidate"]))
    note = f"body 过大，只对比前 {DIFF_MAX_LINES} 行 / {DIFF_MAX_CHARS} 个字符" if base_clipped or cand_clipped else None
    if base == cand:
        return BodyDiff(None, note or "两侧 body 文本相同")
    return BodyDiff(sidediff.table(base, cand, "基线", "候选"), note)


@dataclass(frozen=True)
class FindingRow:
    level: str
    type: str
    path: str
    generalized_path: str
    base: str
    cand: str
    rules: str
    message: str | None


@dataclass(frozen=True)
class RequestView:
    req: RequestResult
    path: str
    codes: str
    sides: list[common.SideView]
    findings: list[FindingRow]
    diff: BodyDiff | None
    anchor: str


def _rows(req: RequestResult, extras: RequestExtras | None) -> list[FindingRow]:
    rows = []
    for f in common.sorted_findings(req.findings):
        base, cand = common.finding_values(f, VALUE_LIMIT)
        rows.append(FindingRow(f.level, f.type, common.finding_path(f), f.generalized_path, base, cand,
                               common.rules_text(f, extras), common.finding_note(f, extras)))  # fmt: skip
    return rows


def _view(index: int, req: RequestResult, extras: RequestExtras | None) -> RequestView:
    return RequestView(
        req=req, path=extras.path if extras else "", codes=common.status_codes(req), sides=common.side_views(req, extras),
        findings=_rows(req, extras), diff=body_diff(extras), anchor=f"req-{index}",
    )  # fmt: skip


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["elapsed"] = common.elapsed_text
    env.globals["SIDE_TEXT"] = common.SIDE_TEXT
    return env


def _template() -> jinja2.Template:
    source = resources.files("api_regress.report").joinpath("templates/report.html.j2").read_text(encoding="utf-8")
    return _environment().from_string(source)


def render(data: ReportInput) -> str:
    result = data.result
    views = [_view(i, req, data.extras.get(req.name)) for i, req in enumerate(result.requests)]
    suggestions = [(req.name, s) for req in result.requests for s in req.suggestions]
    return _template().render(
        title=common.title(result), result=result, compare=common.COMPARE_TEXT[data.compare],
        cells=common.summary_cells(result), requests=views,
        suggestions=suggestions, unmatched=common.unmatched_text(data), warnings=common.all_warnings(result),
    )  # fmt: skip
