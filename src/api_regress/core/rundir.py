"""run 目录：`result.json`，以及 `baseline/`、`candidate/` 下每条请求一个文件（落盘格式见 samples）。"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Collection, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from api_regress.core.result import RunResult
from api_regress.core.samples import FINGERPRINT_VERSION, Sample, to_record

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path("api-regress-out")
RESULT_FILE = "result.json"
SIDE_DIRS = ("baseline", "candidate")


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


def _is_reusable(out: Path, ignore: Collection[Path]) -> bool:
    entries = [p for p in out.iterdir() if p.resolve() not in ignore]
    return not entries or _written_by_us(out / RESULT_FILE)


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
    """再检查一次，然后清掉上次留下的两侧响应。"""
    check(out, ignore)
    for side in SIDE_DIRS:
        old = out / side
        if old.is_dir():
            log.info("清掉上次的 %s", old)
            shutil.rmtree(old)
    out.mkdir(parents=True, exist_ok=True)


def _dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_side(
    out: Path, side: str, name: str, fingerprint: str, samples: Iterable[Sample], compare_headers: list[str]
) -> Path:
    directory = out / side
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.json"
    _dump(
        path,
        {
            "name": name,
            "fingerprint": fingerprint,
            "fingerprint_version": FINGERPRINT_VERSION,
            "samples": [to_record(s, compare_headers) for s in samples],
        },
    )
    return path


def write_result(out: Path, result: RunResult) -> Path:
    path = out / RESULT_FILE
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log.info("写入 %s", path)
    return path
