"""core 各模块共用的基础模型与枚举。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

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
