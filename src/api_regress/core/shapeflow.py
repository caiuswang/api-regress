"""shape 命令的流程：加载 suite（不需要任何环境变量）、从快照或 run 目录读基线样本、构建字段树、打印或写出骨架。

只读，不发请求。service 里的 `shape` / `shape_sync` 只做转发。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from api_regress.core import rundir, shape, snapshot
from api_regress.core.command import CommandReport, Stop, finish_log, internal_error, only_issues, suite_error, usage_issue
from api_regress.core.config import ConfigError, ConfigIssue, LoadedSuite, Request, UnverifiedSuite, load_suite
from api_regress.core.jsonload import load_body
from api_regress.core.result import EXIT_OK, ExitState, exit_code
from api_regress.core.rules import RuleSet
from api_regress.core.samples import LegacyFormatError, Sample, SampleFormatError

log = logging.getLogger(__name__)

COMMAND = "shape"
DEFAULT_MAX_KEYS = 50
SourceKind = Literal["snapshot", "run"]
_SOURCE_TEXT: dict[SourceKind, str] = {"snapshot": "快照", "run": "run 目录"}
_RULE_LOC = re.compile(r"defaults\.rules\b|requests\[\d+\]\.rules")
"""规则所在的位置（含 rules_file）：这里缺环境变量时规则没法确定，按 suite 错误处理。"""


class ShapeOptions(BaseModel):
    source: Path
    """--from：快照目录或 run 目录。"""
    only: list[str] = []
    max_keys: int = DEFAULT_MAX_KEYS
    write: Path | None = None
    """--write：不打印字段树，改为写出规则文件骨架 `<write>/<name>.yaml`。"""


class ShapeRequest(BaseModel):
    name: str
    samples: int = 0
    nodes: int = 0
    missing: str | None = None
    """没有样本的原因；有样本时为 None。"""
    skipped: str | None = None
    """没有 body 而跳过的原因（HEAD 请求、body 全为空）；这种请求不算没有样本，不影响退出码。"""


class ShapeReport(CommandReport):
    """lines 是要打印的内容（不含最后的 `exit N: 原因`）。"""

    lines: list[str] = []
    requests: list[ShapeRequest] = []
    written: list[str] = []


def _suite_error(source: str, issues: list[ConfigIssue]) -> Stop:
    return suite_error(COMMAND, source, issues, ShapeReport)


def _internal_error(source: str, detail: str) -> Stop:
    return internal_error(COMMAND, source, detail, ShapeReport)


# ---------- suite ----------


def _load(path: str | Path, env: Mapping[str, str] | None) -> LoadedSuite:
    """不需要任何 target：targets 里缺的变量不影响；规则（rules、rules_file）依赖缺失的变量时按 suite 错误。"""
    source = str(path)
    try:
        loaded = load_suite(path, sides=(), env=env)
    except ConfigError as exc:
        raise _suite_error(source, exc.issues) from None
    except UnverifiedSuite as exc:
        raise _suite_error(source, exc.fields) from None
    bad = [
        ConfigIssue(source=source, location=m.location, message=f"规则依赖缺失的环境变量 {m.name}", line=m.line)
        for m in loaded.missing_vars
        if _RULE_LOC.match(m.location)
    ]
    if bad:
        raise _suite_error(source, bad)
    return loaded


def _check_usage(source: str, loaded: LoadedSuite, options: ShapeOptions) -> None:
    issues = only_issues(source, (r.name for r in loaded.suite.requests), options.only)
    if options.max_keys < 1:
        issues.append(usage_issue(source, "--max-keys", f"必须是正整数，当前是 {options.max_keys}"))
    if issues:
        raise _suite_error(source, issues)


# ---------- 样本 ----------


@dataclass
class _Source:
    kind: SourceKind
    root: Path
    snap: snapshot.Snapshot | None = None


SampleLookup = tuple[list[Sample], str | None]


def _source_kind(root: Path) -> SourceKind | None:
    if (root / snapshot.MANIFEST).is_file():
        return "snapshot"
    if (root / rundir.RESULT_FILE).is_file():
        return "run"
    return None


def _open_source(source: str, given: Path, names: list[str]) -> _Source:
    """有 manifest.json 按快照读，否则有 result.json 按 run 目录读（两个都有时按快照）。"""
    root = snapshot.readable(given) if not (given / rundir.RESULT_FILE).is_file() else given
    kind = _source_kind(root)
    if kind is None:
        what = "不存在" if not given.exists() else "既不是快照目录（没有 manifest.json），也不是 run 目录（没有 result.json）"
        raise _internal_error(source, f"--from {given} {what}")
    try:
        if kind == "snapshot":
            return _Source(kind, root, snapshot.load(root, names))
        rundir.read_result(root)
    except snapshot.SnapshotVersionError as exc:
        raise _internal_error(source, str(exc)) from None
    except OSError as exc:
        raise _internal_error(source, str(exc)) from None
    return _Source(kind, root)


def _run_samples(root: Path, name: str) -> SampleLookup:
    try:
        side = rundir.read_side(root, "baseline", name)
    except LegacyFormatError as exc:
        raise OSError(f"run 目录里 {name} 的基线响应{exc}，请重新运行 run 或 snapshot compare") from None
    except SampleFormatError as exc:
        raise OSError(f"run 目录里 {name} 的基线响应损坏：{exc}") from None
    if side is None or not side.samples:
        return [], "run 目录里没有这条请求的基线响应"
    return side.samples, None


def _snapshot_samples(snap: snapshot.Snapshot, name: str) -> SampleLookup:
    entry = snap.entries.get(name)
    return (entry[1], None) if entry is not None else ([], "快照里没有这条请求")


def _samples(src: _Source, name: str) -> SampleLookup:
    if src.snap is not None:
        return _snapshot_samples(src.snap, name)
    return _run_samples(src.root, name)


def _json_bodies(samples: list[Sample]) -> list[Any]:
    """网络错误和非 JSON 的 sample 跳过。"""
    out = []
    for sample in samples:
        if sample.failed:
            continue
        loaded = load_body(sample.body, sample.content_type)
        if loaded.is_json:
            out.append(loaded.value)
    return out


# ---------- 每条请求 ----------


@dataclass
class _Built:
    request: Request
    rules: RuleSet
    bodies: list[Any]
    root: shape.Node | None
    info: ShapeRequest


def _no_body(request: Request, samples: list[Sample]) -> str | None:
    """HEAD 请求，或者不是网络错误的 sample 至少有一个、且 body 全为空（如 204）：本来就没有字段树可列。
    全部 sample 都是网络错误时不算，仍然是没有样本。"""
    usable = [s for s in samples if not s.failed]
    if not usable:
        return None
    if request.method == "HEAD":
        return "HEAD 请求，跳过"
    return "响应 body 为空，跳过" if all(not s.body for s in usable) else None


def _build(loaded: LoadedSuite, src: _Source, index: int) -> _Built:
    request = loaded.suite.requests[index]
    rules = RuleSet.for_request(loaded.suite, request, index)
    samples, missing = _samples(src, request.name)
    skipped = None if missing is not None else _no_body(request, samples)
    if skipped is not None:
        log.info("%s：没有 body（%s）", request.name, skipped)
        return _Built(request, rules, [], None, ShapeRequest(name=request.name, skipped=skipped))
    bodies = _json_bodies(samples)
    if missing is None and not bodies:
        missing = "样本都是网络错误或不是 JSON"
    if missing is not None:
        log.info("%s：没有样本（%s）", request.name, missing)
        return _Built(request, rules, [], None, ShapeRequest(name=request.name, missing=missing))
    root = shape.build(bodies)
    info = ShapeRequest(name=request.name, samples=len(bodies), nodes=shape.count_nodes(root))
    log.info("%s：样本 %d 个，节点 %d 个", request.name, info.samples, info.nodes)
    return _Built(request, rules, bodies, root, info)


def _missing_line(item: _Built) -> str:
    """没有字段树的请求：没有样本，或者没有 body 而跳过。"""
    if item.info.skipped is not None:
        return f"# {item.request.name}: 没有 body（{item.info.skipped}）"
    return f"# {item.request.name}: 没有样本（{item.info.missing}）"


def _tree_lines(item: _Built, given: Path, max_keys: int) -> list[str]:
    if item.root is None:
        return [_missing_line(item)]
    req = item.request
    head = f"# {req.name}  {req.method} {req.path}  样本 {item.info.samples} 个（{given}）"
    missed = shape.unmatched(item.rules.rules, item.root, item.bodies)
    return [head, *shape.render_tree(item.root, item.rules, max_keys), *shape.footer(item.rules, missed)]


def skeleton_text(item_name: str, body: list[str]) -> str:
    head = [
        f"# api-regress 规则文件：{item_name}",
        "# 每个 key 是相对路径；以 $ 开头的 key 是规则属性（$ignore、$decimal、$scale_level、$on_value_change、$array、$nullable）。",
        "# 只列出字段、不写属性的节点不产生规则。写法见 doc/user/rules-file.md。",
    ]
    return "\n".join([*head, *body]) + "\n"


def _join_blocks(blocks: list[list[str]]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        out += ([""] if out else []) + block
    return out


# ---------- --write ----------


def _targets(directory: Path, built: list[_Built]) -> list[tuple[_Built, Path]]:
    return [(item, directory / f"{item.request.name}.yaml") for item in built if item.root is not None]


def _check_targets(source: str, targets: list[tuple[_Built, Path]]) -> None:
    existing = [path for _, path in targets if path.exists() or path.is_symlink()]
    if existing:
        issues = [usage_issue(source, "--write", f"文件已存在：{path}（一个都没有写）") for path in existing]
        raise _suite_error(source, issues)


def _write_all(source: str, directory: Path, targets: list[tuple[_Built, Path]], max_keys: int) -> list[str]:
    """先检查所有目标文件都不存在再逐个写；写到一半失败时删掉本次已写出的文件。"""
    written: list[Path] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for item, path in targets:
            assert item.root is not None
            body = shape.render_skeleton(item.root, item.rules, max_keys)
            with path.open("x", encoding="utf-8") as fh:
                written.append(path)
                fh.write(skeleton_text(item.request.name, body))
            log.info("写入 %s", path)
    except OSError as exc:
        for path in written:
            path.unlink(missing_ok=True)
        raise _internal_error(source, f"无法写入规则文件骨架（{exc.strerror or exc}），已删除本次写出的文件") from None
    return [str(path) for path in written]


def _hint(suite_path: Path, written: list[str]) -> str:
    """例子里的路径和加载 suite 时一样，按（不解析符号链接的）suite 所在目录算相对路径；
    算不出来（Windows 上不在同一个盘）时写绝对路径，rules_file 也支持绝对路径。"""
    target = Path(written[0]).absolute()
    try:
        example = Path(os.path.relpath(target, suite_path.absolute().parent)).as_posix()
    except ValueError:
        example = str(target)
    return f"在 suite 的请求里加上 rules_file（例如 rules_file: {example}，相对 suite 所在目录）才会生效"


# ---------- 入口 ----------


def _exit(built: list[_Built]) -> tuple[int, str]:
    missing = sum(1 for item in built if item.info.missing is not None)
    if missing:
        return exit_code(ExitState(incomplete=missing, incomplete_text="条请求没有样本"))
    return EXIT_OK, "全部输出"


def _selected(loaded: LoadedSuite, only: list[str]) -> list[int]:
    return [i for i, r in enumerate(loaded.suite.requests) if not only or r.name in only]


def _print_tree(built: list[_Built], options: ShapeOptions) -> tuple[list[str], list[str]]:
    return _join_blocks([_tree_lines(item, options.source, options.max_keys) for item in built]), []


def _write_skeletons(source: str, suite_path: Path, built: list[_Built], options: ShapeOptions
                     ) -> tuple[list[str], list[str]]:  # fmt: skip
    assert options.write is not None
    targets = _targets(options.write, built)
    _check_targets(source, targets)
    written = _write_all(source, options.write, targets, options.max_keys)
    lines = [_missing_line(item) for item in built if item.root is None] + [f"wrote {p}" for p in written]
    return (lines + [_hint(suite_path, written)] if written else lines), written


def _shape(path: str | Path, options: ShapeOptions, env: Mapping[str, str] | None) -> ShapeReport:
    source = str(path)
    loaded = _load(path, env)
    _check_usage(source, loaded, options)
    indices = _selected(loaded, options.only)
    src = _open_source(source, options.source, [loaded.suite.requests[i].name for i in indices])
    log.info("shape 开始：样本来源 %s %s，%d 条请求", _SOURCE_TEXT[src.kind], src.root, len(indices))
    try:
        built = [_build(loaded, src, i) for i in indices]
    except OSError as exc:
        raise _internal_error(source, str(exc)) from None
    if options.write is None:
        lines, written = _print_tree(built, options)
    else:
        lines, written = _write_skeletons(source, Path(path), built, options)
    code, reason = _exit(built)
    finish_log(COMMAND, code, reason)
    infos = [item.info for item in built]
    return ShapeReport(source=source, exit_code=code, exit_reason=reason, lines=lines, requests=infos, written=written)


def shape_sync(path: str | Path, options: ShapeOptions, env: Mapping[str, str] | None = None) -> ShapeReport:
    """输出每条请求的字段树（或写出规则文件骨架）。只读，不发请求，不需要任何环境变量。"""
    try:
        return _shape(path, options, env)
    except Stop as stop:
        assert isinstance(stop.report, ShapeReport)
        return stop.report


async def shape_async(path: str | Path, options: ShapeOptions, env: Mapping[str, str] | None = None) -> ShapeReport:
    return await asyncio.to_thread(shape_sync, path, options, env)


