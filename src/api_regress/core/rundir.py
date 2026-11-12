"""run 目录：`result.json`，以及 `baseline/`、`candidate/` 下每条请求一个文件（落盘格式见 samples）。

accept 从这里读回候选响应（`read_result`、`read_side`）。"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Collection, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from api_regress.core.config import NAME_PATTERN
from api_regress.core.result import RunResult
from pydantic import ValidationError

from api_regress.core.samples import Sample, SampleFormatError, SideFile, parse_side_document, write_side_file

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path("api-regress-out")
RESULT_FILE = "result.json"
SIDE_DIRS = ("baseline", "candidate")
BODIES_DIR = "samples"
REPORT_FILES = {"html": "report.html", "md": "report.md", "junit": "junit.xml"}
"""各种报告的文件名。复用 run 目录时先删掉旧的，免得留下和本次结果对不上的报告。"""


class RunDirError(OSError):
    pass


def default_out_dir(now: datetime, root: Path = DEFAULT_ROOT) -> Path:
    """`api-regress-out/<YYYYmmdd-HHMMSS>/`；同一秒内重复运行时加 `-2`、`-3` 后缀。"""
    base = root / now.strftime("%Y%m%d-%H%M%S")
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{base.name}-{n}")
    return candidate


def _written_by_us(path: Path) -> bool:
    """result.json 是 api-regress 写的：能解析，并且有我们的顶层字段。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("schema_version"), int)
        and data.get("command") in ("run", "compare")
        and isinstance(data.get("tool_version"), str)
    )


_LEFTOVER_ENTRIES = frozenset({*SIDE_DIRS, BODIES_DIR, *REPORT_FILES.values()})


def _is_leftover(entries: list[Path]) -> bool:
    """上次写到一半中断（result.json 最后写，所以还没有）：只有两侧目录、body 目录和报告；
    两侧目录里只有 .json 文件，body 目录里不限。"""
    for entry in entries:
        if entry.name not in _LEFTOVER_ENTRIES:
            return False
        if entry.name in SIDE_DIRS and entry.is_dir() and any(not (f.is_file() and f.suffix == ".json") for f in entry.iterdir()):
            return False
    return True


def _is_reusable(out: Path, ignore: Collection[Path]) -> bool:
    entries = [p for p in out.iterdir() if p.resolve() not in ignore]
    return not entries or _written_by_us(out / RESULT_FILE) or _is_leftover(entries)


def check(out: Path, ignore: Collection[Path] = ()) -> None:
    """发请求之前检查 `--out` 能不能用，免得请求都发完了才发现写不进去。

    已存在且非空的目录只有是之前的 run 目录（result.json 是 api-regress 写的）才复用；
    这样 `--out .` 之类的误用不会把无关文件混进 run 目录，也不会删掉它们。ignore 里的文件（例如日志文件）不算。"""
    ignore = {p.resolve() for p in ignore}
    if out.exists() and not out.is_dir():
        raise RunDirError(f"--out {out} 已存在且不是目录")
    if out.is_dir() and not _is_reusable(out, ignore):
        raise RunDirError(f"--out {out} 不是空目录，也不是之前的 run 目录，拒绝写入")


def prepare(out: Path, ignore: Collection[Path] = ()) -> None:
    """再检查一次，然后清掉上次留下的 result.json、两侧响应和报告。

    result.json 最先删：本次写到一半失败或被打断时，目录里不会留下上次的 result.json 配本次的两侧响应。"""
    check(out, ignore)
    old_result = out / RESULT_FILE
    if old_result.is_file():
        log.info("清掉上次的 %s", old_result)
        old_result.unlink()
    for side in (*SIDE_DIRS, BODIES_DIR):
        old = out / side
        if old.is_dir():
            log.info("清掉上次的 %s", old)
            shutil.rmtree(old)
    ignored = {p.resolve() for p in ignore}
    for name in REPORT_FILES.values():
        old_report = out / name
        if old_report.is_file() and old_report.resolve() not in ignored:
            log.info("清掉上次的 %s", old_report)
            old_report.unlink()
    out.mkdir(parents=True, exist_ok=True)


def write_side(
    out: Path, side: str, name: str, fingerprint: str, samples: Iterable[Sample], compare_headers: list[str]
) -> Path:
    directory = out / side
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.json"
    write_side_file(path, body_dir(out, side, name), name, fingerprint, samples, compare_headers)
    return path


def body_dir(run_dir: Path, side: str, name: str) -> Path:
    return run_dir / BODIES_DIR / side / name


def write_result(out: Path, result: RunResult) -> Path:
    path = out / RESULT_FILE
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log.info("写入 %s", path)
    return path


def read_result(run_dir: Path) -> RunResult:
    """读之前的 run 目录（accept 用）。不是 api-regress 写的、或解析不了时抛 RunDirError。"""
    path = run_dir / RESULT_FILE
    if not _written_by_us(path):
        raise RunDirError(f"{run_dir} 不是 api-regress 的 run 目录（{RESULT_FILE} 不存在或不是 api-regress 写的）")
    try:
        result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RunDirError(f"{path} 解析不了（{type(exc).__name__}）") from None
    bad = [r.name for r in result.requests if not NAME_PATTERN.fullmatch(r.name)]
    if bad:
        raise RunDirError(f"{path} 里有不合法的请求名 {bad[0]!r}")
    return result


def read_side(run_dir: Path, side: str, name: str) -> SideFile | None:
    """`<side>/<name>.json` 和它的 body 文件；请求文件不存在时返回 None，格式不对、body 文件缺失或对不上抛 SampleFormatError。"""
    path = run_dir / side / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SampleFormatError(f"{path} 读不出来或不是合法的 JSON（{type(exc).__name__}）") from None
    return parse_side_document(data, body_dir(run_dir, side, name))
