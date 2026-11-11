"""JUnit XML 报告（`junit.xml`）：每条请求是一个 testcase。

有达到 `--fail-on` 级别的 finding 记 failure；否则 `error` / `inconclusive` 记 error；`unsupported` 记 skipped。
failure 里列出达到级别的 finding，其余 finding 和 warning 写进 system-out。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from api_regress.core.models import Finding
from api_regress.core.reportdata import ReportInput
from api_regress.core.result import RequestResult, RunResult
from api_regress.report import common

VALUE_LIMIT = 200
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_text(text: str) -> str:
    """XML 1.0 不允许的字符（响应里可能有）换成 U+FFFD，否则标准解析器读不了。"""
    return _XML_INVALID.sub("\ufffd", text)


def _finding_line(finding: Finding) -> str:
    base, cand = common.finding_values(finding, VALUE_LIMIT)
    note = f"（{finding.message}）" if finding.message else ""
    return xml_text(f"[{finding.level}] {finding.type} {common.finding_path(finding)}：{base} → {cand}{note}")


def _outcome(req: RequestResult, result: RunResult) -> tuple[str, str] | None:
    """(元素名, message)；通过时为 None。failure 优先于 error，和退出码 1 优先于 4 一致。"""
    bad = common.failing(req, result)
    if bad:
        return "failure", f"{len(bad)} 条达到 {result.fail_on} 级别的 finding"
    if req.status in ("error", "inconclusive"):
        return "error", f"{req.status}：{req.reason or ''}"
    if req.status == "unsupported":
        return "skipped", f"unsupported：{req.reason or ''}"
    return None


def _elapsed(req: RequestResult) -> float:
    ms = req.candidate.elapsed_ms if req.candidate else None
    return (ms or 0.0) / 1000


def _system_out(req: RequestResult, shown: list[Finding]) -> str:
    lines = [_finding_line(f) for f in common.sorted_findings(req.findings) if f not in shown]
    lines += [xml_text(f"[warning {w.code}] {w.message}") for w in req.warnings]
    return "\n".join(lines)


def _testcase(parent: ET.Element, req: RequestResult, result: RunResult, classname: str) -> str | None:
    case = ET.SubElement(parent, "testcase", name=req.name, classname=classname, time=f"{_elapsed(req):.3f}")
    outcome = _outcome(req, result)
    shown: list[Finding] = []
    if outcome is not None:
        tag, message = outcome
        node = ET.SubElement(case, tag, message=xml_text(message))
        if tag == "failure":
            shown = common.failing(req, result)
            node.set("type", "finding")
            node.text = "\n".join(_finding_line(f) for f in shown)
    out = _system_out(req, shown)
    if out:
        ET.SubElement(case, "system-out").text = out
    return outcome[0] if outcome else None


def render(data: ReportInput) -> bytes:
    result = data.result
    classname = xml_text(Path(result.suite).stem or "suite")
    suite_name = f"api-regress {common.COMMAND_TEXT[result.command]}: {result.suite}"
    suite = ET.Element("testsuite", name=xml_text(suite_name))
    counts = {"failure": 0, "error": 0, "skipped": 0}
    for req in result.requests:
        tag = _testcase(suite, req, result, classname)
        if tag is not None:
            counts[tag] += 1
    seconds = (result.finished_at - result.started_at).total_seconds()
    attrs = {
        "tests": len(result.requests), "failures": counts["failure"], "errors": counts["error"],
        "skipped": counts["skipped"], "time": f"{seconds:.3f}", "timestamp": result.started_at.isoformat(),
    }  # fmt: skip
    for key, value in attrs.items():
        suite.set(key, str(value))
    root = ET.Element("testsuites", name="api-regress")
    root.append(suite)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
