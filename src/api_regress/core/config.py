"""suite 配置：数据模型、YAML 加载、环境变量替换、报错行号定位。"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from api_regress.core import expr, paths, rulesfile
from api_regress.core.models import SIDES, Level, Loc, Side, format_loc

log = logging.getLogger(__name__)

READ_METHODS = frozenset({"GET", "HEAD"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
"""请求 name 的合法写法。快照和 run 目录按 name 命名文件，读回时也用它检查，防止路径穿越。"""


# ---------- 数据模型 ----------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Model):
    base_url: str
    headers: dict[str, str] = {}


class Targets(_Model):
    baseline: Target
    candidate: Target


class DecimalRule(_Model):
    allow_string: bool = False


ArrayMode = Literal["schema", "keyed", "positional", "unordered"]


class ArrayRule(_Model):
    mode: ArrayMode | None = None
    keys: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _resolve_mode(self) -> ArrayRule:
        if self.keys and self.mode is None:
            self.mode = "keyed"
        if self.mode == "keyed" and not self.keys:
            raise ValueError("keyed 模式必须写 keys")
        if self.keys and self.mode != "keyed":
            raise ValueError(f"写了 keys 时 mode 只能是 keyed，当前是 {self.mode}")
        self.mode = self.mode or "schema"
        return self


class Rule(_Model):
    path: str
    ignore: bool | None = None
    decimal: DecimalRule | None = None
    scale_level: Level | None = None
    on_value_change: Level | None = None
    array: ArrayRule | None = None
    nullable: bool | None = None

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        paths.parse(value)
        return value

    @model_validator(mode="after")
    def _check_attrs(self) -> Rule:
        if all(getattr(self, attr) is None for attr in RULE_ATTRS):
            raise ValueError(f"规则至少要有一个属性：{', '.join(RULE_ATTRS)}")
        return self

    @property
    def tokens(self) -> paths.Path:
        return paths.parse(self.path)


RULE_ATTRS = tuple(name for name in Rule.model_fields if name != "path")


@dataclass(frozen=True)
class FileRule:
    """规则文件展开出的一条规则。file 是规则文件路径（相对路径已按 suite 所在目录拼好），line 是属性所在的行。"""

    rule: Rule
    file: str
    line: int | None


class Assert(_Model):
    expr: str
    level: Level = "suspicious"
    message: str | None = None


class Defaults(_Model):
    timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    concurrency: int = Field(default=4, ge=1)
    retries: int = Field(default=1, ge=0)
    compare: Literal["structure", "values"] = "structure"
    success: str | None = None
    headers: dict[str, str] = {}
    rules: list[Rule] = []


class Request(_Model):
    name: str
    method: str
    path: str
    query: dict[str, str] = {}
    headers: dict[str, str] = {}
    compare_headers: list[str] = []
    success: str | None = None
    rules_file: str | None = None
    rules: list[Rule] = []
    asserts: list[Assert] = []

    _file_rules: list[FileRule] = PrivateAttr(default_factory=list)

    @property
    def file_rules(self) -> list[FileRule]:
        """rules_file 展开出的规则，加载 suite 时填上；没写 rules_file 时为空。"""
        return self._file_rules

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_PATTERN.fullmatch(value):
            raise ValueError(f"name {value!r} 只能包含 A-Z a-z 0-9 _ . -")
        return value

    @field_validator("method")
    @classmethod
    def _check_method(cls, value: str) -> str:
        method = value.upper()
        if method in WRITE_METHODS:
            raise ValueError(f"v1 只发 GET/HEAD，不支持写方法 {method}")
        if method not in READ_METHODS:
            raise ValueError(f"不支持的 HTTP 方法 {value!r}，只能是 GET 或 HEAD")
        return method


class Suite(_Model):
    version: int
    targets: Targets
    defaults: Defaults = Defaults()
    requests: list[Request] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"不支持的 version {value}，当前只支持 1")
        return value

    def success_of(self, request: Request) -> str | None:
        """请求级写了 success（包括 null，表示不判定）就用请求级的，否则沿用 defaults。"""
        if "success" in request.model_fields_set:
            return request.success
        return self.defaults.success


# ---------- 加载结果与错误 ----------

VarSide = Literal["baseline", "candidate", "shared"]


class ConfigIssue(BaseModel):
    source: str
    location: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        return format_where(self.source, self.line, self.location) + f" {self.message}"


def format_where(source: str, line: int | None, location: str | None) -> str:
    """统一的位置前缀：`suite.yaml:12: requests[0].method:`"""
    where = f"{source}:{line}:" if line else f"{source}:"
    return f"{where} {location}:" if location else where


class ConfigError(Exception):
    def __init__(self, issues: list[ConfigIssue]):
        super().__init__("\n".join(str(i) for i in issues))
        self.issues = issues


class MissingVar(BaseModel):
    name: str
    side: VarSide
    location: str
    line: int | None = None


class UnverifiedSuite(Exception):
    """只在 sides 为空（validate）时出现：除了缺失变量所在的字段，其余都已通过校验，
    但这些字段的值要等变量设置后才能确定类型，因此拿不到完整的 Suite。"""

    def __init__(self, missing: list[MissingVar], fields: list[ConfigIssue], request_count: int):
        super().__init__(f"{len(fields)} 个字段依赖缺失的环境变量")
        self.missing = missing
        self.fields = fields
        self.request_count = request_count


class LoadedSuite(BaseModel):
    source: str
    suite: Suite
    raw: dict[str, Any]
    missing_vars: list[MissingVar] = []

    _lines: dict[tuple[str, ...], int] = PrivateAttr(default_factory=dict)

    def line_of(self, loc: Loc) -> int | None:
        return _lookup_line(self._lines, loc)


# ---------- 环境变量替换 ----------

# 默认值里不允许 $ 和 }，因此嵌套写法不会被当成合法变量，而是被识别为无法识别的写法
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}$]*))?\}")


@dataclass
class Substitution:
    env: Mapping[str, str]
    missing: list[tuple[str, Loc]] = field(default_factory=list)
    invalid: list[Loc] = field(default_factory=list)

    def walk(self, data: Any, loc: Loc = ()) -> Any:
        if isinstance(data, dict):
            return {key: self.walk(value, (*loc, key)) for key, value in data.items()}
        if isinstance(data, list):
            return [self.walk(value, (*loc, i)) for i, value in enumerate(data)]
        if isinstance(data, str):
            return self._string(data, loc)
        return data

    def _string(self, text: str, loc: Loc) -> str:
        if "${" in _VAR.sub("", text):
            self.invalid.append(loc)
            return text
        return _VAR.sub(lambda m: self._replace(m, loc), text)

    def _replace(self, m: re.Match[str], loc: Loc) -> str:
        name, default = m.group(1), m.group(2)
        value = self.env.get(name)
        if value or (value is not None and default is None):
            return value
        if default is not None:
            return default
        self.missing.append((name, loc))
        return m.group(0)


def substitute_env(data: Any, env: Mapping[str, str]) -> tuple[Any, Substitution]:
    """只替换字符串值里的 ${VAR} / ${VAR:-默认值}（变量未设置或为空时取默认值）。
    缺失的变量保留原文；返回的 Substitution 里记录缺失变量和无法识别的写法的位置。"""
    sub = Substitution(env)
    return sub.walk(data), sub


def _side_of(loc: Loc) -> VarSide:
    if len(loc) >= 2 and loc[0] == "targets" and loc[1] in SIDES:
        return loc[1]  # type: ignore[return-value]
    return "shared"


def _is_needed(side: VarSide, sides: Collection[Side]) -> bool:
    return side in sides or (side == "shared" and bool(sides))


# ---------- YAML：大小检查与行号映射 ----------

MAX_NODES = 100_000
MAX_DEPTH = 64


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.MappingNode):
        return [n for pair in node.value for n in pair]
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    return []


def _measure_new(node: yaml.Node, depth: int, memo: dict[int, tuple[int, int]], active: set[int]) -> tuple[int, int]:
    collection = isinstance(node, (yaml.MappingNode, yaml.SequenceNode))
    if collection and depth > MAX_DEPTH:
        raise ValueError(f"嵌套超过 {MAX_DEPTH} 层")
    active.add(id(node))
    measured = [_measure(child, depth + 1, memo, active) for child in _children(node)]
    active.discard(id(node))
    size = 1 + sum(m[0] for m in measured)
    if size > MAX_NODES:
        raise ValueError(f"展开锚点后超过 {MAX_NODES} 个节点")
    return size, int(collection) + max((m[1] for m in measured), default=0)


def _measure(node: yaml.Node, depth: int, memo: dict[int, tuple[int, int]], active: set[int]) -> tuple[int, int]:
    """返回 (展开锚点后的节点数, 子树里映射和序列的层数)。防止锚点指数展开、自引用和过深的嵌套。

    depth 是 node 所在的层（根为 1），只数映射和序列，标量不算一层。
    同一个锚点可能在不同深度被引用，所以记忆的是子树高度，每次命中都按当前深度重新判断。"""
    key = id(node)
    if key in active:
        raise ValueError("锚点引用了自身")
    if key not in memo:
        memo[key] = _measure_new(node, depth, memo, active)
    size, height = memo[key]
    if depth - 1 + height > MAX_DEPTH:
        raise ValueError(f"嵌套超过 {MAX_DEPTH} 层")
    return size, height


def _key(part: str | int) -> str:
    return str(part)


@dataclass
class _LineMap:
    lines: dict[tuple[str, ...], int] = field(default_factory=dict)
    dups: list[tuple[Loc, int]] = field(default_factory=list)

    def visit(self, node: yaml.Node, loc: Loc) -> None:
        if isinstance(node, yaml.MappingNode):
            self._mapping(node, loc)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                self._record((*loc, i), item.start_mark.line + 1)
                self.visit(item, (*loc, i))

    def _mapping(self, node: yaml.MappingNode, loc: Loc) -> None:
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = _key(key_node.value)
            line = key_node.start_mark.line + 1
            if key in seen:
                self.dups.append(((*loc, key), line))
            seen.add(key)
            self._record((*loc, key), line)
            self.visit(value_node, (*loc, key))

    def _record(self, loc: Loc, line: int) -> None:
        self.lines.setdefault(tuple(_key(p) for p in loc), line)


def _lookup_line(lines: dict[tuple[str, ...], int], loc: Loc) -> int | None:
    key = tuple(_key(p) for p in loc)
    while key:
        if key in lines:
            return lines[key]
        key = key[:-1]
    return lines.get((), None)


# ---------- pydantic 错误翻译 ----------

_MESSAGES = {
    "extra_forbidden": "未知字段，检查是否拼错",
    "missing": "缺少必填字段",
    "string_type": "应为字符串（YAML 里的数字、布尔值、日期要加引号）",
}


def _error_message(err: Mapping[str, Any]) -> str:
    if err["type"] == "value_error":
        return str(err["ctx"]["error"])
    return _MESSAGES.get(err["type"], err["msg"])


def _clean_loc(loc: Loc) -> Loc:
    # pydantic 对 dict 的 key 校验错误会在 loc 末尾加 '[key]'
    return tuple(p for p in loc if p != "[key]")


# ---------- 加载 ----------


class _Ctx:
    def __init__(self, source: str):
        self.source = source
        self.lines: dict[tuple[str, ...], int] = {}

    def issue(self, loc: Loc, message: str, line: int | None = None) -> ConfigIssue:
        return ConfigIssue(
            source=self.source,
            location=format_loc(loc),
            message=message,
            line=line or _lookup_line(self.lines, loc),
        )

    def error(self, message: str, line: int | None = None) -> ConfigError:
        return ConfigError([ConfigIssue(source=self.source, location="", message=message, line=line)])


def _yaml_error(exc: yaml.YAMLError, ctx: _Ctx, text: str) -> ConfigError:
    # 不用 str(exc)：它会带上出错行的原文，可能包含硬编码的 header 值
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = min(mark.line + 1, len(text.splitlines()) or 1) if mark else None
    parts = [getattr(exc, "context", None), getattr(exc, "problem", None)]
    detail = "，".join(p for p in parts if p) or type(exc).__name__
    return ctx.error(f"YAML 语法错误：{detail}", line)


def _compose(text: str, ctx: _Ctx) -> Any:
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if node is not None:
            _measure(node, 1, {}, set())
        return node, yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _yaml_error(exc, ctx, text) from None
    except RecursionError:
        raise ctx.error(f"YAML 嵌套超过 {MAX_DEPTH} 层") from None
    except ValueError as exc:
        # _measure 的限制，或 PyYAML 解析非法日期（如没加引号的 2024-02-30）
        raise ctx.error(f"YAML 无法加载：{exc}") from None


def _parse_yaml(text: str, ctx: _Ctx, what: str = "suite", empty_ok: bool = False) -> Any:
    node, data = _compose(text, ctx)
    line_map = _LineMap()
    if node is not None:
        line_map.visit(node, ())
    ctx.lines = line_map.lines
    if line_map.dups:
        raise ConfigError([ctx.issue(loc, f"重复的字段 {loc[-1]!r}", line) for loc, line in line_map.dups])
    if data is None and empty_ok:
        return None
    if not isinstance(data, dict):
        raise ctx.error(f"{what}顶层必须是一个映射（key: value）", 1)
    return data


class _Unverified(Exception):
    def __init__(self, fields: list[ConfigIssue]):
        self.fields = fields


def _issues(ctx: _Ctx, errors: list[Any]) -> ConfigError:
    return ConfigError([ctx.issue(_clean_loc(e["loc"]), _error_message(e)) for e in errors])


def _validate(data: dict, ctx: _Ctx, unresolved: set[Loc]) -> Suite:
    try:
        return Suite.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
    blocking = [e for e in errors if _clean_loc(e["loc"]) not in unresolved]
    if blocking:
        raise _issues(ctx, blocking)
    raise _Unverified([ctx.issue(_clean_loc(e["loc"]), "值来自缺失的环境变量，设置后才能校验") for e in errors])


BASE_URL_FORMAT = "必须是 http:// 或 https:// 开头的完整地址"
BASE_URL_USERINFO = "不能带用户名或密码（user:pass@），凭据请写在 headers 里"


def base_url_problem(url: str) -> str | None:
    """base_url 不合法时返回原因。带 userinfo 的地址会把密码写进日志和 run 目录，所以拒绝。"""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  端口越界时这里才抛 ValueError
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        return BASE_URL_FORMAT
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return BASE_URL_FORMAT
    if "@" in parts.netloc:
        return BASE_URL_USERINFO
    return None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list:
    return value if isinstance(value, list) else []


def _expr_fields(data: Any) -> list[tuple[Loc, Any, expr.ExprKind]]:
    """替换变量后的原始数据里所有表达式字段：(位置, 值, 种类)。

    直接读原始数据而不是 Suite 模型，这样 pydantic 校验没通过（UnverifiedSuite）时也能检查。"""
    root = _mapping(data)
    out: list[tuple[Loc, Any, expr.ExprKind]] = [(("defaults", "success"), _mapping(root.get("defaults")).get("success"), "success")]
    for i, req in enumerate(_sequence(root.get("requests"))):
        req = _mapping(req)
        out.append((("requests", i, "success"), req.get("success"), "success"))
        out += [
            (("requests", i, "asserts", j, "expr"), _mapping(a).get("expr"), "assert")
            for j, a in enumerate(_sequence(req.get("asserts")))
        ]
    return out


def _expr_issues(data: Any, ctx: _Ctx, unresolved: set[Loc]) -> list[ConfigIssue]:
    """表达式白名单检查。原文含缺失变量的跳过，设置变量后再查；类型不对的交给 pydantic 报。"""
    issues = []
    for loc, source, kind in _expr_fields(data):
        if not isinstance(source, str) or loc in unresolved:
            continue
        try:
            expr.compile_expr(source, kind)
        except expr.ExprError as exc:
            issues.append(ctx.issue(loc, f"表达式不合法：{exc}"))
    return issues


def _post_checks(suite: Suite, ctx: _Ctx, sides: Collection[Side], unresolved: set[Loc]) -> list[ConfigIssue]:
    issues = []
    seen: set[str] = set()
    for i, req in enumerate(suite.requests):
        if req.name in seen:
            issues.append(ctx.issue(("requests", i, "name"), f"重复的请求 name {req.name!r}"))
        seen.add(req.name)
    for side in SIDES:
        loc = ("targets", side, "base_url")
        # 本命令不需要、且值还没确定（缺变量）的一侧不检查
        problem = base_url_problem(getattr(suite.targets, side).base_url)
        if problem and (side in sides or loc not in unresolved):
            issues.append(ctx.issue(loc, f"base_url {problem}"))
    return issues


def _substitute(raw: dict, env: Mapping[str, str], ctx: _Ctx, sides: Collection[Side]) -> tuple[Any, Substitution]:
    data, sub = substitute_env(raw, env)
    bad = [ctx.issue(loc, "无法识别的 ${...} 写法：只支持 ${VAR} 和 ${VAR:-默认值}，不支持嵌套") for loc in sub.invalid]
    bad += [ctx.issue(loc, f"缺少环境变量 {name}") for name, loc in sub.missing if _is_needed(_side_of(loc), sides)]
    if bad:
        raise ConfigError(bad)
    return data, sub


# ---------- 规则文件 ----------


def _rule_issues(raw: rulesfile.RawRule, exc: ValidationError, ctx: _Ctx) -> list[ConfigIssue]:
    out = []
    for err in exc.errors():
        loc = _clean_loc(err["loc"])
        attr_loc = raw.attr_locs.get(str(loc[0])) if loc else None
        where = f"{raw.path} ${format_loc(loc)}" if loc else raw.path
        out.append(ConfigIssue(source=ctx.source, location=where, message=_error_message(err),
                               line=_lookup_line(ctx.lines, attr_loc or raw.loc)))  # fmt: skip
    return out


def parse_rules_file(text: str, source: str) -> list[FileRule]:
    """解析规则文件文本，展开成规则。有问题时抛 ConfigError，位置是规则文件里的行号和节点路径。"""
    ctx = _Ctx(source)
    expansion = rulesfile.expand(_parse_yaml(text, ctx, "规则文件", empty_ok=True), RULE_ATTRS)
    issues = [
        ConfigIssue(source=source, location=p.where, message=p.message, line=_lookup_line(ctx.lines, p.loc))
        for p in expansion.problems
    ]
    rules = []
    for raw in expansion.rules:
        try:
            rule = Rule.model_validate({"path": raw.path, **raw.attrs})
        except ValidationError as exc:
            issues += _rule_issues(raw, exc, ctx)
            continue
        rules.append(FileRule(rule, source, _lookup_line(ctx.lines, raw.loc)))
    if issues:
        raise ConfigError(sorted(issues, key=lambda i: i.line or 0))
    return rules


def _rules_file_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _load_rules_file(path: Path, loc: Loc, ctx: _Ctx) -> list[FileRule]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise ConfigError([ctx.issue(loc, f"无法读取规则文件 {path}：{detail}")]) from None
    rules = parse_rules_file(text, str(path))
    log.info("规则文件加载完成：%s，%d 条规则", path, len(rules))
    return rules


def _attach_rules_files(suite: Suite, ctx: _Ctx, base_dir: Path, unresolved: set[Loc]) -> list[ConfigIssue]:
    """按 suite 所在目录解析 rules_file，展开后挂到请求上。同一个文件只读一次；值来自缺失变量的跳过。"""
    cache: dict[Path, list[FileRule]] = {}
    issues: list[ConfigIssue] = []
    for i, req in enumerate(suite.requests):
        loc: Loc = ("requests", i, "rules_file")
        if req.rules_file is None or loc in unresolved:
            continue
        path = _rules_file_path(base_dir, req.rules_file)
        try:
            if path not in cache:
                cache[path] = _load_rules_file(path, loc, ctx)
        except ConfigError as exc:
            issues += exc.issues
            cache[path] = []
            continue
        req._file_rules = cache[path]
    return issues


def _request_count(raw: dict) -> int:
    requests = raw.get("requests")
    return len(requests) if isinstance(requests, list) else 0


def load_suite_text(
    text: str,
    source: str = "<suite>",
    sides: Collection[Side] = (),
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> LoadedSuite:
    """解析 suite 文本。sides 是本命令需要的 target：这些 target 和共用部分缺变量时报错，其余只记录。

    sides 为空（validate）且有字段因缺变量无法校验类型时，抛 UnverifiedSuite。
    rules_file 的相对路径按 base_dir 解析，默认是 source 所在的目录。"""
    ctx = _Ctx(source)
    raw = _parse_yaml(text, ctx)
    data, sub = _substitute(raw, os.environ if env is None else env, ctx, sides)
    missing = [
        MissingVar(name=name, side=_side_of(loc), location=format_loc(loc), line=_lookup_line(ctx.lines, loc))
        for name, loc in sub.missing
    ]
    unresolved = {loc for _, loc in sub.missing}
    try:
        suite = _validate(data, ctx, unresolved)
    except _Unverified as exc:
        bad = _expr_issues(data, ctx, unresolved)
        if bad:
            raise ConfigError(bad) from None
        raise UnverifiedSuite(missing, exc.fields, _request_count(raw)) from None
    issues = _post_checks(suite, ctx, sides, unresolved) + _expr_issues(data, ctx, unresolved)
    if issues:
        raise ConfigError(issues)
    issues = _attach_rules_files(suite, ctx, Path(source).parent if base_dir is None else base_dir, unresolved)
    if issues:
        raise ConfigError(issues)
    loaded = LoadedSuite(source=source, suite=suite, raw=raw, missing_vars=missing)
    loaded._lines = ctx.lines
    log.info("suite 加载完成：%s，%d 条请求", source, len(suite.requests))
    return loaded


def load_suite(
    path: str | Path, sides: Collection[Side] = (), env: Mapping[str, str] | None = None
) -> LoadedSuite:
    source = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError([ConfigIssue(source=source, location="", message=f"无法读取 suite 文件：{exc}")]) from None
    return load_suite_text(text, source, sides, env)
