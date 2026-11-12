"""快照：落盘的基线 sample，用来替代实时请求基线。

```
snapshot/
  manifest.json            # schema_version、tool_version、fingerprint_version、updated_at、每条请求的指纹/时间/来源
  requests/<name>.json     # 和 run 目录的 baseline/<name>.json 格式相同（samples.write_side_file）
  samples/<name>/<n>.json  # 第 n 个 sample 的 body（.json / .txt / .bin）
```

写入是原子的：在同级目录写好完整的新快照，再 `snap → snap.old`、新目录 → `snap`，最后删掉 `snap.old`。
不能直接 `os.replace` 到已存在的非空目录（ENOTEMPTY）。中断留下的残留由 `recover` 在下次启动时处理。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from api_regress import __version__
from api_regress.core.config import NAME_PATTERN
from api_regress.core.samples import (
    FINGERPRINT_VERSION,
    LegacyFormatError,
    Sample,
    SampleFormatError,
    dump_json,
    parse_side_document,
    write_side_file,
)
from api_regress.core.status import SnapshotProblem

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
REQUESTS_DIR = "requests"
BODIES_DIR = "samples"

Source = Literal["record", "accept"]


class SnapshotError(OSError):
    """快照不能用：损坏、不是快照目录、残留无法处理、写入失败。退出码 3。"""


class SnapshotVersionError(Exception):
    """manifest 的 schema_version 不兼容。退出码 2。"""


class ManifestEntry(BaseModel):
    fingerprint: str
    recorded_at: datetime
    source: Source
    samples: int


class Manifest(BaseModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    tool_version: str
    fingerprint_version: int
    updated_at: datetime
    requests: dict[str, ManifestEntry] = {}


@dataclass
class NewEntry:
    """要写进快照的一条请求。compare_headers 决定保留哪些响应头（Content-Type 总会保留）。"""

    name: str
    fingerprint: str
    samples: list[Sample]
    source: Source
    compare_headers: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    """读出来的快照。entries 只包含 load 时要求的请求。"""

    root: Path
    exists: bool
    fingerprint_version: int = FINGERPRINT_VERSION
    entries: dict[str, tuple[str, list[Sample]]] = field(default_factory=dict)

    def baseline(self, name: str, fingerprint: str) -> tuple[list[Sample], SnapshotProblem | None]:
        """这条请求的基线 sample；快照里没有时返回 ([], "missing")，指纹不一致时照样返回 sample。"""
        entry = self.entries.get(name)
        if entry is None:
            return [], "missing"
        stored, samples = entry
        if stored != fingerprint or self.fingerprint_version != FINGERPRINT_VERSION:
            return samples, "fingerprint_mismatch"
        return samples, None


# ---------- 路径与残留 ----------


def _old(root: Path) -> Path:
    return root.with_name(root.name + ".old")


def _tmp_prefix(root: Path) -> str:
    return f".{root.name}.tmp-"


def _normalize(root: Path) -> Path:
    root = root.resolve()
    if not root.name:
        raise SnapshotError(f"--dir 不能是根目录：{root}")
    return root


def recover(root: Path) -> Path:
    """record / accept 用：处理上次中断留下的残留，返回规范化后的快照路径。

    `snap` 不存在而 `snap.old` 存在：中断发生在两次 rename 之间，把 `.old` 恢复回来；
    `snap` 存在：`.old` 是已经替换掉的旧快照，删掉。临时目录一律删掉。"""
    root = _normalize(root)
    old = _old(root)
    if old.exists():
        if not (old / MANIFEST).is_file():
            raise SnapshotError(f"{old} 已存在，但不是 api-regress 留下的，请手动处理")
        if root.exists():
            log.info("删除上次留下的旧快照 %s", old)
            shutil.rmtree(old)
        else:
            log.warning("上次写快照时中断，恢复 %s → %s", old, root)
            os.rename(old, root)
    if root.parent.is_dir():
        for tmp in root.parent.glob(_tmp_prefix(root) + "*"):
            if tmp.is_dir():
                log.info("删除上次留下的临时目录 %s", tmp)
                shutil.rmtree(tmp, ignore_errors=True)
    return root


def readable(root: Path) -> Path:
    """compare 用：只读，不清理残留。`snap` 不存在而 `snap.old` 是我们留下的（上次写入中断在两次 rename 之间）时，
    直接读 `snap.old`，由下次 record / accept 的 `recover` 恢复。"""
    root = _normalize(root)
    old = _old(root)
    if not root.exists() and (old / MANIFEST).is_file():
        log.warning("快照 %s 不存在，上次写入中断留下了 %s，本次读取它；下次 snapshot record / accept 时会恢复", root, old)
        return old
    return root


# ---------- 读 ----------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SnapshotError(f"快照损坏：缺少 {path}") from None
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"快照损坏：{path} 读不出来或不是合法的 JSON（{type(exc).__name__}）") from None


def _read_manifest(root: Path) -> Manifest:
    path = root / MANIFEST
    data = _read_json(path)
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version != SCHEMA_VERSION:
        raise SnapshotVersionError(
            f"快照 {root} 的 schema_version 是 {version}，当前工具只支持 {SCHEMA_VERSION}；用 snapshot record --clean 重录"
        )
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        raise SnapshotError(f"快照损坏：{path} 格式不对（{exc.error_count()} 处错误）") from None
    bad = [name for name in manifest.requests if not NAME_PATTERN.fullmatch(name)]
    if bad:
        raise SnapshotError(f"快照损坏：{path} 里有不合法的请求名 {bad[0]!r}")
    return manifest


def _body_dir(root: Path, name: str) -> Path:
    return root / BODIES_DIR / name


def _read_entry(root: Path, name: str) -> tuple[str, list[Sample]]:
    path = root / REQUESTS_DIR / f"{name}.json"
    try:
        side = parse_side_document(_read_json(path), _body_dir(root, name))
    except LegacyFormatError as exc:
        raise SnapshotError(f"快照损坏：{path} {exc}，用 snapshot record --clean 重录") from None
    except SampleFormatError as exc:
        raise SnapshotError(f"快照损坏：{path}：{exc}") from None
    if side.name != name or not side.samples:
        raise SnapshotError(f"快照损坏：{path} 的 name 不对或没有 sample")
    return side.fingerprint, side.samples


def _is_snapshot_dir(root: Path) -> bool:
    return root.is_dir() and (not any(root.iterdir()) or (root / MANIFEST).is_file())


def load(root: Path, names: Iterable[str]) -> Snapshot:
    """读快照里的这些请求。目录不存在时当作空快照；损坏抛 SnapshotError，版本不兼容抛 SnapshotVersionError。"""
    if not root.exists():
        log.warning("快照 %s 不存在，所有请求都按快照缺失处理", root)
        return Snapshot(root=root, exists=False)
    if not root.is_dir():
        raise SnapshotError(f"--dir {root} 不是目录")
    if not (root / MANIFEST).is_file():
        if any(root.iterdir()):
            raise SnapshotError(f"--dir {root} 不是快照目录（没有 {MANIFEST}）")
        return Snapshot(root=root, exists=False)
    manifest = _read_manifest(root)
    entries = {name: _read_entry(root, name) for name in names if name in manifest.requests}
    log.info("读取快照 %s：%d 条请求", root, len(entries))
    return Snapshot(root=root, exists=True, fingerprint_version=manifest.fingerprint_version, entries=entries)


def check_writable(root: Path, clean: bool) -> Manifest | None:
    """发请求之前检查能不能写。返回现有的 manifest（不存在或 --clean 时为 None）。

    已存在的非空目录必须含 manifest.json，免得 `--dir .` 把无关文件换掉。--clean 不读 manifest 的内容，
    所以损坏或版本不兼容的快照可以用 --clean 重录。请求文件只检查是否存在：没变的文件按字节原样复制，
    不需要解析（大快照上 `record --only x` 不必把每个 body 都解一遍）；内容坏了由 compare 读取时报。"""
    if root.exists() and not _is_snapshot_dir(root):
        raise SnapshotError(f"--dir {root} 已存在，但不是快照目录（没有 {MANIFEST}），拒绝写入")
    if clean or not (root / MANIFEST).is_file():
        return None
    manifest = _read_manifest(root)
    missing = [name for name in manifest.requests if not (root / REQUESTS_DIR / f"{name}.json").is_file()]
    if missing:
        raise SnapshotError(f"快照损坏：缺少 {root / REQUESTS_DIR / (missing[0] + '.json')}")
    return manifest


# ---------- 写 ----------


def _merged_manifest(base: Manifest | None, entries: list[NewEntry], now: datetime) -> Manifest:
    requests = dict(base.requests) if base is not None else {}
    for entry in entries:
        requests[entry.name] = ManifestEntry(
            fingerprint=entry.fingerprint, recorded_at=now, source=entry.source, samples=len(entry.samples)
        )
    return Manifest(
        tool_version=__version__, fingerprint_version=FINGERPRINT_VERSION, updated_at=now,
        requests=dict(sorted(requests.items())),
    )  # fmt: skip


def _fill(tmp: Path, root: Path, base: Manifest | None, entries: list[NewEntry], manifest: Manifest) -> None:
    requests = tmp / REQUESTS_DIR
    requests.mkdir()
    replaced = {e.name for e in entries}
    for name in (base.requests if base is not None else {}):
        if name not in replaced:
            _copy_entry(root, tmp, name)
    for entry in entries:
        write_side_file(
            requests / f"{entry.name}.json", _body_dir(tmp, entry.name), entry.name, entry.fingerprint, entry.samples,
            entry.compare_headers,
        )  # fmt: skip
    dump_json(tmp / MANIFEST, json.loads(manifest.model_dump_json()))


def _copy_entry(root: Path, tmp: Path, name: str) -> None:
    """没变的请求按字节原样复制：请求文件和 body 目录都不解析（body 目录不存在时只复制请求文件，compare 时再报损坏）。"""
    shutil.copy2(root / REQUESTS_DIR / f"{name}.json", tmp / REQUESTS_DIR / f"{name}.json")
    bodies = _body_dir(root, name)
    if bodies.is_dir():
        shutil.copytree(bodies, _body_dir(tmp, name))


def _swap(tmp: Path, root: Path) -> None:
    if not root.exists():
        os.rename(tmp, root)
        return
    old = _old(root)
    os.rename(root, old)
    os.rename(tmp, root)
    try:
        shutil.rmtree(old)
    except OSError as exc:  # 新快照已经就位，旧的留给下次 recover 清理
        log.warning("新快照已写入，但删除旧快照 %s 失败（%s），下次运行时会再清理", old, exc.strerror or exc)


def write(root: Path, entries: list[NewEntry], base: Manifest | None, now: datetime) -> Manifest:
    """把 entries 按请求合并进 base（None 表示从空快照开始），原子地替换 root。失败抛 SnapshotError。"""
    manifest = _merged_manifest(base, entries, now)
    tmp: Path | None = None
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=_tmp_prefix(root), dir=root.parent))
        _fill(tmp, root, base, entries, manifest)
        _swap(tmp, root)
    except OSError as exc:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise SnapshotError(f"无法写入快照 {root}（{exc.strerror or exc}）") from exc
    log.info("写入快照 %s：本次 %d 条，共 %d 条", root, len(entries), len(manifest.requests))
    return manifest
