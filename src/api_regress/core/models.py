"""core 各模块共用的基础模型与枚举。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from api_regress.core import paths

Level = Literal["breaking", "suspicious", "info"]
Side = Literal["baseline", "candidate"]
SIDES: tuple[Side, Side] = ("baseline", "candidate")

WarningCode = Literal[
    "env_var_missing",
    "array_degraded_to_schema",
    "array_empty",
    "rule_ineffective_in_schema",
    "rule_under_ignored",
    "index_rule_ineffective",
    "both_non_2xx",
    "json_parse_failed",
    "nan_or_infinity",
    "duplicate_key",
    "success_eval_error",
    "second_sample_unavailable",
    "fingerprint_mismatch",
]

Loc = tuple[str | int, ...]


class WarningItem(BaseModel):
    """提示信息。不是 finding，不参与退出码。"""

    code: WarningCode
    message: str
    request: str | None = None
    path: str | None = None
    location: str | None = None
    line: int | None = None


def format_loc(loc: Sequence[str | int]) -> str:
    """('requests', 0, 'rules', 1) -> 'requests[0].rules[1]'"""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


# ---------- finding ----------

FindingType = Literal[
    "status_changed",
    "body_kind_changed",
    "business_error",
    "header_missing",
    "header_changed",
    "field_removed",
    "type_changed",
    "nullability_changed",
    "scale_changed",
    "field_added",
    "array_length_changed",
    "element_missing",
    "element_added",
    "value_changed",
    "assert_failed",
    "assert_error",
]


class RuleRef(BaseModel):
    """规则的位置：defaults.rules[index] 或本请求的 rules[index]。"""

    model_config = ConfigDict(frozen=True)

    scope: Literal["defaults", "request"]
    index: int


class Absent(BaseModel):
    """字段不存在，和 null 区分开。输出为 {"absent": true}。"""

    model_config = ConfigDict(frozen=True)

    absent: Literal[True] = True


ABSENT = Absent()

ValueType = Literal["object", "array", "string", "number", "boolean", "null", "absent"]

TokenKind = Literal["key", "index", "any", "keyed"]


class PathToken(BaseModel):
    """具体路径的一级：key / index（positional）/ any（schema、unordered）/ keyed（展示用标签）。"""

    model_config = ConfigDict(frozen=True)

    kind: TokenKind
    value: str | int | None = None


_TOKEN_KIND: dict[type, tuple[TokenKind, str | None]] = {
    paths.Key: ("key", "name"),
    paths.Index: ("index", "value"),
    paths.AnyIndex: ("any", None),
    paths.KeyedElem: ("keyed", "label"),
}


def to_path_tokens(tokens: Sequence[paths.Token]) -> list[PathToken]:
    out = []
    for tok in tokens:
        kind, attr = _TOKEN_KIND[type(tok)]
        out.append(PathToken(kind=kind, value=getattr(tok, attr) if attr else None))
    return out


class Finding(BaseModel):
    """一条差异，参与退出码计算。

    body 的 finding：`path` 是具体路径，`generalized_path` 可以直接写进规则。
    非 body 的 finding 用伪路径（`$status`、`$header.Content-Type` 等），`path_tokens` 为空。
    schema 数组里的 finding：两侧的值是类型集合或小数位集合，并附 `example_index`。
    `base_type` / `cand_type` 是两侧值的 JSON 类型（缺失时为 `absent`），用来区分 `12.50` 和 `"12.50"`、
    以及真实的 `{"absent": true}` 和缺失；值是集合、长度等汇总信息时为 None。
    `finding_id` 在一条请求的流程结束时填上，见 `finding_id()`。
    """

    finding_id: str = ""
    type: FindingType
    level: Level
    path: str
    generalized_path: str
    path_tokens: list[PathToken] = []
    base_value: Any = None
    cand_value: Any = None
    base_type: ValueType | None = None
    cand_type: ValueType | None = None
    example_index: int | None = None
    rule_refs: list[RuleRef] = []
    message: str | None = None


def finding_id(request: str, kind: str, path: str) -> str:
    """请求名 + 类型 + 具体路径的 hash。keyed 数组的具体路径用 key 值，schema 数组用泛化路径，
    非 body 的 finding 用伪路径，所以同一个差异在两次运行中的 id 相同。"""
    digest = hashlib.sha256("\x00".join((request, kind, path)).encode("utf-8")).hexdigest()
    return digest[:16]


class Suggestion(BaseModel):
    """不稳定字段建议：`path` 是泛化路径，`yaml` 是可以直接粘贴进 rules 的片段。不会自动生效。"""

    path: str
    reason: str
    yaml: str
