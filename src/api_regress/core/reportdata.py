"""报告的输入：`result.json` 之外、渲染报告还要用到的信息（两侧的 sample、请求头的名字、规则路径）。

core 不依赖 report：调用方（cli）把 `Reporter` 传进 `RunOptions.reporters`。service 先在内存里渲染全部报告，
确定最终退出码后再写文件，所以写出的报告和 `result.json` 的退出码一致。
这些信息不进 `result.json`，请求头只有名字没有值。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from api_regress.core.config import Request, Suite
from api_regress.core.models import RuleRef, Side
from api_regress.core.result import RunResult
from api_regress.core.runner import merge_headers
from api_regress.core.samples import Sample


@dataclass(frozen=True)
class RequestExtras:
    """一条请求的补充信息。path 是 suite 里的路径加 query。samples 是参与对比的那一个（基线第 1 个、候选），没有时不出现。"""

    path: str
    header_names: dict[Side, list[str]]
    samples: dict[Side, Sample] = field(default_factory=dict)
    rule_paths: dict[RuleRef, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportInput:
    result: RunResult
    out_dir: Path
    extras: dict[str, RequestExtras]


@dataclass(frozen=True)
class Reporter:
    """一种报告：写到 run 目录里的文件名，和只在内存里渲染、不写文件的函数。"""

    filename: str
    render: Callable[[ReportInput], bytes]


def _rule_paths(suite: Suite, request: Request) -> dict[RuleRef, str]:
    paths = {RuleRef(scope="defaults", index=i): rule.path for i, rule in enumerate(suite.defaults.rules)}
    paths.update({RuleRef(scope="request", index=i): rule.path for i, rule in enumerate(request.rules)})
    return paths


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
        header_names=names, samples=samples, rule_paths=_rule_paths(suite, request)
    )
