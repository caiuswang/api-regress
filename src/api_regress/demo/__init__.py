"""内置 demo 服务：固定数据，用来在不联网时试用 api-regress。不依赖 core。"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

SUITE_NAMES = ("pass.yaml", "regress.yaml")


def suite_text(name: str) -> str:
    return files(__package__).joinpath("suites", name).read_text(encoding="utf-8")


def write_suites(directory: Path) -> list[Path]:
    """把示例 suite 写到 directory；有同名文件时一个都不写，抛 FileExistsError。"""
    targets = [directory / name for name in SUITE_NAMES]
    existing = [p for p in targets if p.exists()]
    if existing:
        raise FileExistsError(", ".join(str(p) for p in existing))
    directory.mkdir(parents=True, exist_ok=True)
    for name, path in zip(SUITE_NAMES, targets, strict=True):
        path.write_text(suite_text(name), encoding="utf-8")
    return targets
