"""一次 body 对比的上下文：规则、对比模式、收集 finding 和 warning。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from api_regress.core import paths
from api_regress.core.diff.values import json_type
from api_regress.core.models import ABSENT, Finding, FindingType, Level, WarningCode, WarningItem, to_path_tokens
from api_regress.core.rules import Resolved, RuleSet

CompareMode = Literal["structure", "values"]
Path = tuple[paths.Token, ...]

# visit(ctx, path, base, cand, presence)：presence 是字段/元素缺失时用的 (removed 类型, added 类型)
Visit = Callable[["DiffContext", Path, Any, Any, "Presence"], None]


@dataclass(frozen=True)
class Presence:
    removed: FindingType
    removed_level: Level
    added: FindingType
    added_level: Level


FIELD = Presence("field_removed", "breaking", "field_added", "info")
ELEMENT = Presence("element_missing", "suspicious", "element_added", "info")


def _value_type(value: Any) -> str:
    return "absent" if value is ABSENT else json_type(value)


@dataclass
class DiffContext:
    rules: RuleSet
    compare: CompareMode
    visit: Visit
    request: str | None = None
    findings: list[Finding] = field(default_factory=list)
    warnings: list[WarningItem] = field(default_factory=list)
    _warned: set[tuple[str, str, str]] = field(default_factory=set)

    def resolve(self, path: Path) -> Resolved:
        return self.rules.resolve(path)

    def value_level(self, res: Resolved) -> Level | None:
        """值比较的级别：on_value_change 优先；`compare: values` 时默认 suspicious；否则不比较值。"""
        if res.on_value_change is not None:
            return res.on_value_change
        return "suspicious" if self.compare == "values" else None

    def report(
        self,
        kind: FindingType,
        level: Level,
        path: Path,
        base: Any,
        cand: Any,
        res: Resolved,
        example_index: int | None = None,
        *,
        summary: bool = False,
    ) -> None:
        """summary 为真时两侧的值是汇总信息（类型集合、小数位集合、长度），不填 base_type / cand_type。"""
        self.findings.append(
            Finding(
                type=kind,
                level=level,
                path=paths.format_path(path),
                generalized_path=paths.format_path(paths.generalize(path)),
                path_tokens=to_path_tokens(path),
                base_value=base,
                cand_value=cand,
                base_type=None if summary else _value_type(base),
                cand_type=None if summary else _value_type(cand),
                example_index=example_index,
                rule_refs=res.refs,
            )
        )

    @staticmethod
    def where(path: Path) -> str:
        """warning 里展示的路径：泛化路径。"""
        return paths.format_path(paths.generalize(path))

    def warn(self, code: WarningCode, path: Path, message: str) -> None:
        """按泛化路径去重：keyed / positional 数组里每个元素的同一个问题只提示一次。"""
        where = self.where(path)
        key = (code, where, message)
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(WarningItem(code=code, message=message, request=self.request, path=where))
