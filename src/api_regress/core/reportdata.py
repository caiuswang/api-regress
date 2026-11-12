"""报告的输入：`result.json` 之外、渲染报告还要用到的信息（两侧的 sample、请求头的名字、报出 finding 的 suite 配置）。

core 不依赖 report：调用方（cli）把 `Reporter` 传进 `RunOptions.reporters`。service 先在内存里渲染全部报告，
确定最终退出码后再写文件，所以写出的报告和 `result.json` 的退出码一致。
这些信息不进 `result.json`，请求头只有名字没有值。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from api_regress.core.config import Assert, Request, Rule, Suite
from api_regress.core.models import RuleRef, Side
from api_regress.core.result import RunResult
from api_regress.core.rules import compile_rules
from api_regress.core.runner import merge_headers
from api_regress.core.samples import Sample


@dataclass(frozen=True)
class SuccessSpec:
    """生效的 success 表达式和它写在哪里：`defaults.success` 或请求级的 `success`。"""

    where: str
    expr: str


@dataclass(frozen=True)
class RequestExtras:
    """一条请求的补充信息。path 是 suite 里的路径加 query。samples 是参与对比的那一个（基线第 1 个、候选），没有时不出现。
    rules、asserts、success 是报出 finding 的 suite 配置，报告用它说明 finding 是怎么来的。"""

    path: str
    header_names: dict[Side, list[str]]
    samples: dict[Side, Sample] = field(default_factory=dict)
    rules: dict[RuleRef, Rule] = field(default_factory=dict)
    asserts: list[Assert] = field(default_factory=list)
    success: SuccessSpec | None = None


@dataclass(frozen=True)
class ReportInput:
    result: RunResult
    out_dir: Path
    extras: dict[str, RequestExtras]
    compare: Literal["structure", "values"] = "structure"


@dataclass(frozen=True)
class Reporter:
    """一种报告：写到 run 目录里的文件名，和只在内存里渲染、不写文件的函数。"""

    filename: str
    render: Callable[[ReportInput], bytes]


def _rules(suite: Suite, request: Request) -> dict[RuleRef, Rule]:
    return {compiled.ref: compiled.rule for compiled in compile_rules(suite, request)}


def _success(suite: Suite, request: Request) -> SuccessSpec | None:
    expr = suite.success_of(request)
    if expr is None:
        return None
    return SuccessSpec("success" if "success" in request.model_fields_set else "defaults.success", expr)


def extras_for(suite: Suite, request: Request, base: Sequence[Sample], cand: Sample | None) -> RequestExtras:
    names = {
        side: list(merge_headers(suite.defaults.headers, getattr(suite.targets, side).headers, request.headers))
        for side in ("baseline", "candidate")
    }
    samples: dict[Side, Sample] = {}
    if base:
        samples["baseline"] = base[0]
    if cand is not None:
        samples["candidate"] = cand
    return RequestExtras(
        path=request.path + (f"?{urlencode(request.query)}" if request.query else ""),
        header_names=names, samples=samples, rules=_rules(suite, request),
        asserts=list(request.asserts), success=_success(suite, request),
    )  # fmt: skip
