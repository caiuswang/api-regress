"""各种报告共用的展示逻辑：值怎么显示、curl 复现命令、状态码、规则引用、body 的展示文本。"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import yaml

from api_regress.core.config import Assert, Rule
from api_regress.core.jsonload import load_body
from api_regress.core.models import Absent, Finding, Level, RuleRef, Side, WarningItem
from api_regress.core.reportdata import ReportInput, RequestExtras
from api_regress.core.result import FAIL_ON_LEVELS, RequestResult, RunResult
from api_regress.core.samples import Sample

LEVEL_ORDER: dict[Level, int] = {"breaking": 0, "suspicious": 1, "info": 2}
SIDE_TEXT: dict[Side, str] = {"baseline": "基线", "candidate": "候选"}
ABSENT_TEXT = "(缺失)"
REDACTED = "<redacted>"
COMMAND_TEXT = {"run": "run", "compare": "snapshot compare"}
COMPARE_TEXT = {"structure": "对比模式 structure（只比结构，值只在规则要求时比较）", "values": "对比模式 values（比较值）"}
_ASSERT_PATH = re.compile(r"\$assert\[(\d+)\]")


def _summary_text(values: Any, finding_type: str) -> str:
    """schema 数组的 finding 两侧是类型集合或小数位集合：`{null, object}`、`小数位 {1, 2}`。"""
    text = "{" + ", ".join(str(v) for v in values) + "}"
    return f"小数位 {text}" if finding_type == "scale_changed" else text


def _value_text(value: Any, kind: str | None, finding_type: str) -> str:
    """`kind` 是 finding 里这一侧的 JSON 类型：数字原样显示（`12.50`），字符串加引号（`"12.50"`），缺失显示 (缺失)。
    `kind` 为 None 时值是汇总信息（状态码、类型集合等）；没有值（如 assert_failed）时为空。"""
    if kind == "absent" or isinstance(value, Absent):
        return ABSENT_TEXT
    if kind is None and isinstance(value, (list, tuple, set, frozenset)):
        return _summary_text(value, finding_type)
    if kind is None and value is None:
        return ""
    if kind == "number" and isinstance(value, str):  # 从 result.json 读回的数字是字符串
        return value
    return compact_json(value)


def compact_json(value: Any) -> str:
    """单行 JSON，数字按原文的小数位输出（对象、数组里嵌套的 `Decimal` 也一样，不会变成带引号的字符串）。"""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)}: {compact_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(compact_json(v) for v in value) + "]"
    return _scalar(value)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def finding_values(finding: Finding, limit: int) -> tuple[str, str]:
    return (
        clip(_value_text(finding.base_value, finding.base_type, finding.type), limit),
        clip(_value_text(finding.cand_value, finding.cand_type, finding.type), limit),
    )


def finding_path(finding: Finding) -> str:
    """具体路径；schema 数组的 finding 附上示例下标。"""
    if finding.example_index is None:
        return finding.path
    return f"{finding.path}（示例下标 {finding.example_index}）"


def rule_attrs_text(rule: Rule) -> str:
    """规则除 path 外的属性，写成单行 YAML：`on_value_change: breaking`、`decimal: {}`、`array: {mode: keyed, keys: [sku_id]}`。"""
    attrs = rule.model_dump(exclude={"path"}, exclude_none=True, exclude_defaults=True)
    flow = yaml.safe_dump(attrs, default_flow_style=True, allow_unicode=True, sort_keys=False, width=1 << 30)
    return flow.strip()[1:-1]


def rule_where(scope: str, index: int, file: str | None, line: int | None) -> str:
    """规则写在哪里：`defaults.rules[0]`、`rules[1]`，规则文件里的写 `rules_file <文件>:<行号>`。"""
    if scope == "rules_file":
        return f"rules_file {file}:{line}" if line else f"rules_file {file}"
    return f"defaults.rules[{index}]" if scope == "defaults" else f"rules[{index}]"


def rule_ref_text(ref: RuleRef, extras: RequestExtras | None) -> str:
    where = rule_where(ref.scope, ref.index, ref.file, ref.line)
    rule = extras.rules.get(ref) if extras else None
    return f"{where} {rule.path}（{rule_attrs_text(rule)}）" if rule else where


def _assert_of(finding: Finding, extras: RequestExtras | None) -> tuple[int, Assert] | None:
    """assert 的 finding 用伪路径 `$assert[i]` 指向本请求的第 i 个 assert。"""
    match = _ASSERT_PATH.fullmatch(finding.path)
    if not (match and extras and finding.type in ("assert_failed", "assert_error")):
        return None
    index = int(match.group(1))
    return (index, extras.asserts[index]) if index < len(extras.asserts) else None


def _source_text(finding: Finding, extras: RequestExtras | None) -> str:
    found = _assert_of(finding, extras)
    if found is not None:
        return f"asserts[{found[0]}] {found[1].expr}"
    if finding.type == "business_error" and extras and extras.success:
        return f"{extras.success.where} {extras.success.expr}"
    return ""


def rules_text(finding: Finding, extras: RequestExtras | None) -> str:
    """报出这条 finding 的 suite 配置：命中的规则（位置、路径、内容），或 assert / success 的表达式。"""
    if finding.rule_refs:
        return "；".join(rule_ref_text(ref, extras) for ref in finding.rule_refs)
    return _source_text(finding, extras)


def finding_note(finding: Finding, extras: RequestExtras | None) -> str | None:
    """finding 的说明。没配 message 的 assert，说明就是表达式本身，已经写在「命中的规则」里，不再重复。"""
    found = _assert_of(finding, extras)
    if found is not None and finding.message == found[1].expr:
        return None
    return finding.message


def sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: LEVEL_ORDER[f.level])


def failing(req: RequestResult, result: RunResult) -> list[Finding]:
    levels = FAIL_ON_LEVELS[result.fail_on]
    return [f for f in req.findings if f.level in levels]


def status_codes(req: RequestResult) -> str:
    """`200 → 500`；没有响应时写 `-`。"""
    base = req.baseline[0].status if req.baseline else None
    cand = req.candidate.status if req.candidate else None
    return f"{_code(base)} → {_code(cand)}"


def _code(status: int | None) -> str:
    return "-" if status is None else str(status)


def curl_command(method: str, url: str, header_names: list[str]) -> str:
    """可以直接复现的 curl。请求头只给名字，值一律写成 <redacted>，复现前自己填上。"""
    parts = ["curl", "-sS", "-I" if method == "HEAD" else "-i", shlex.quote(url)]
    for name in header_names:
        parts += ["-H", shlex.quote(f"{name}: {REDACTED}")]
    return " ".join(parts)


@dataclass(frozen=True)
class SideView:
    """一侧参与对比的那次请求，给报告展示用。"""

    side: Side
    url: str
    status: int | None
    elapsed_ms: float | None
    retries: int
    error: str | None
    curl: str


def side_views(req: RequestResult, extras: RequestExtras | None) -> list[SideView]:
    infos = [("baseline", req.baseline[0] if req.baseline else None), ("candidate", req.candidate)]
    views = []
    for side, info in infos:
        if info is None:
            continue
        names = extras.header_names.get(side, []) if extras else []
        views.append(
            SideView(side, info.url, info.status, info.elapsed_ms, info.retries, info.error,
                     curl_command(req.method, info.url, names))
        )  # fmt: skip
    return views


def elapsed_text(ms: float | None) -> str:
    return "-" if ms is None else f"{ms:.0f} ms"


# ---------- body 的展示文本 ----------


def _scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    if isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def body_text(sample: Sample) -> str:
    """JSON 按原文的小数位缩进排版；其他文本原样；不是 UTF-8 时只给字节数。"""
    if sample.failed:
        return f"(请求失败：{sample.error})"
    loaded = load_body(sample.body, sample.content_type)
    if loaded.is_json:
        return pretty_json(loaded.value)
    try:
        return sample.body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"(不是 UTF-8 文本，{len(sample.body)} 字节)"


def pretty_json(value: Any) -> str:
    return "\n".join(_lines(value, ""))


def _lines(value: Any, indent: str) -> list[str]:
    inner = indent + "  "
    if isinstance(value, dict) and value:
        body = [_entry(f"{inner}{json.dumps(k, ensure_ascii=False)}: ", v, inner) for k, v in value.items()]
        return ["{", *_join(body), f"{indent}}}"]
    if isinstance(value, list) and value:
        body = [_entry(inner, v, inner) for v in value]
        return ["[", *_join(body), f"{indent}]"]
    return [_scalar(value)]


def _entry(prefix: str, value: Any, indent: str) -> list[str]:
    lines = _lines(value, indent)
    return [prefix + lines[0], *lines[1:]]


def _join(entries: list[list[str]]) -> list[str]:
    out: list[str] = []
    for i, lines in enumerate(entries):
        if i < len(entries) - 1:
            lines = [*lines[:-1], lines[-1] + ","]
        out += lines
    return out


# ---------- 汇总 ----------


def summary_cells(result: RunResult) -> list[tuple[str, int]]:
    s = result.summary
    return [
        ("请求", s.requests), ("ok", s.ok), ("error", s.error), ("inconclusive", s.inconclusive),
        ("unsupported", s.unsupported), ("breaking", s.breaking), ("suspicious", s.suspicious), ("info", s.info),
        ("warning", s.warnings),
    ]  # fmt: skip


def all_warnings(result: RunResult) -> list[WarningItem]:
    return [w if w.request else w.model_copy(update={"request": req.name}) for req in result.requests for w in req.warnings]


def unmatched_text(data: ReportInput) -> list[str]:
    out = []
    for rule in data.result.unmatched_rules:
        where = rule_where(rule.scope, rule.index, rule.file, rule.line)
        out.append(f"{where} {rule.path}" if rule.scope == "defaults" else f"{rule.request}: {where} {rule.path}")
    return out


def title(result: RunResult) -> str:
    return f"api-regress {COMMAND_TEXT[result.command]} 报告：{result.suite}"
