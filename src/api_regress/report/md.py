"""Markdown 报告（`report.md`）：适合贴到 PR 或 IM。

只展开有 finding 或状态不是 ok 的请求（unsupported 也展开，附原因）；没有差异的请求合成一行。两侧 body 的对照只在 HTML 报告里有。"""

from __future__ import annotations


from api_regress.core.models import Finding
from api_regress.core.reportdata import ReportInput, RequestExtras
from api_regress.core.result import RequestResult
from api_regress.report import common

VALUE_LIMIT = 80
_ESCAPES = str.maketrans(
    {"|": "\\|", "<": "&lt;", ">": "&gt;", "\n": " ", "\r": " ", "`": "\\`", "*": "\\*", "_": "\\_"}
)


def cell(text: str) -> str:
    """表格单元格和列表项：转义 `|`、尖括号、反引号、`*`、`_`，换行变空格，免得破坏表格、被当成 HTML，
    或者把 `data.list[*].items[*]`、`**.price` 里的星号渲染成斜体、粗体。"""
    return text.translate(_ESCAPES)


def _finding_row(finding: Finding, extras: RequestExtras | None) -> str:
    base, cand = common.finding_values(finding, VALUE_LIMIT)
    note = common.finding_note(finding, extras)
    kind = f"{finding.type}（{note}）" if note else finding.type
    cells = [finding.level, kind, common.finding_path(finding), base, cand, common.rules_text(finding, extras)]
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def _request_section(req: RequestResult, extras: RequestExtras | None) -> list[str]:
    path = extras.path if extras else ""
    lines = [f"### {cell(req.name)}  {req.method} {cell(path)}  {common.status_codes(req)}", ""]
    if req.status != "ok":
        lines += [f"**{req.status}**：{cell(req.reason or '')}", ""]
    if req.findings:
        lines += ["| 级别 | 类型 | 路径 | 基线 | 候选 | 命中的规则 |", "|---|---|---|---|---|---|"]
        lines += [_finding_row(f, extras) for f in common.sorted_findings(req.findings)]
        lines.append("")
    for view in common.side_views(req, extras):
        lines.append(f"- {common.SIDE_TEXT[view.side]}：`{view.curl}`")
    return [*lines, ""]


def _summary(data: ReportInput) -> list[str]:
    result = data.result
    counts = " | ".join(f"{name} {n}" for name, n in common.summary_cells(result))
    head = f"## 汇总  退出码 {result.exit_code}：{result.exit_reason}"
    when = f"{result.started_at:%Y-%m-%d %H:%M:%S}，--fail-on {result.fail_on}，{common.COMPARE_TEXT[data.compare]}"
    when += "，已取消" if result.cancelled else ""
    return [f"# {common.title(result)}", "", head, counts, "", when, ""]


def _quiet(requests: list[RequestResult]) -> list[str]:
    names = [cell(req.name) for req in requests if req.status == "ok" and not req.findings]
    return [f"没有差异：{', '.join(names)}", ""] if names else []


def _suggestions(data: ReportInput) -> list[str]:
    items = [(req.name, s) for req in data.result.requests for s in req.suggestions]
    if not items:
        return []
    lines = ["### 建议 ignore", "", "不会自动生效；确认是不稳定字段后，把片段加进对应请求或 defaults 的 rules。", ""]
    for name, s in items:
        lines.append(f"- {cell(name)}: {cell(s.path)}（{cell(s.reason)}）")
        lines.append("  ```yaml")
        lines += [f"  {line}" for line in s.yaml.splitlines()]
        lines.append("  ```")
    return [*lines, ""]


def _unmatched(data: ReportInput) -> list[str]:
    items = common.unmatched_text(data)
    if not items:
        return []
    return ["### 本次未命中的规则", "", "在本次请求的 body 里一个路径都没匹配到（不一定是过期了）：", "",
            *[f"- {cell(item)}" for item in items], ""]  # fmt: skip


def _warnings(data: ReportInput) -> list[str]:
    items = common.all_warnings(data.result)
    if not items:
        return []
    lines = ["### warning", ""]
    lines += [f"- {cell(f'[{w.code}] {w.request}: {w.message}')}" for w in items]
    return [*lines, ""]


def render(data: ReportInput) -> str:
    requests = data.result.requests
    lines = _summary(data)
    for req in requests:
        if req.findings or req.status != "ok":
            lines += _request_section(req, data.extras.get(req.name))
    lines += _quiet(requests) + _suggestions(data) + _unmatched(data) + _warnings(data)
    return "\n".join(lines).rstrip("\n") + "\n"
