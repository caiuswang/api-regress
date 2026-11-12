"""sample（一条请求的一次响应）：内存表示、落盘格式（单个 sample、body 文件和一条请求的文件）、请求指纹。

run 目录的 `baseline/`、`candidate/` 和快照共用同一种落盘格式：请求文件只放元数据，每个 sample 的 body
单独存一个文件（`<序号>.json|txt|bin`），放在调用方给的 body 目录里，请求文件用 `body_file` 只记文件名。
落盘时只保留 `Content-Type` 和 `compare_headers` 列出的响应头，请求头一律不保存。
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api_regress.core.jsonfmt import pretty_json
from api_regress.core.jsonload import charset_of, is_json_media, load_body

FINGERPRINT_VERSION = 1
CONTENT_TYPE = "content-type"


@dataclass
class Sample:
    """`status` 为 None 表示网络错误或超时（重试后仍失败），原因在 `error`。响应头名字一律小写。"""

    url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: float | None = None
    retries: int = 0
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status is None

    @property
    def content_type(self) -> str | None:
        return self.headers.get(CONTENT_TYPE)


def kept_headers(headers: Mapping[str, str], compare_headers: Iterable[str]) -> dict[str, str]:
    names = {CONTENT_TYPE, *(name.lower() for name in compare_headers)}
    return {name: value for name, value in headers.items() if name in names}




# ---------- body 文件 ----------

BODY_FILE = re.compile(r"([1-9][0-9]*)\.(json|txt|bin)")


@dataclass(frozen=True)
class StoredBody:
    """body 文件的内容。formatted 为真表示是缩进格式化过的 JSON，否则是响应的原始字节。"""

    data: bytes
    formatted: bool
    suffix: str


def _warning_codes(loaded: Any) -> list[str]:
    return [w.code for w in loaded.warnings]


def formatted_json(body: bytes, content_type: str | None) -> bytes | None:
    """缩进格式化 JSON body；格式化会改变对比结果时返回 None（存原始字节）。

    例外：charset 不是 UTF-8（输出是 UTF-8，和存下的 Content-Type 对不上）、解析失败、有重复 key
    （格式化只剩最后一个，duplicate_key 提示就没了）。最后把结果重新解析一遍自检：值（repr 能区分 12.50 和 12.5）
    和 warning 都要和原文相同。"""
    if codecs.lookup(charset_of(content_type)).name != "utf-8":
        return None
    loaded = load_body(body, content_type)
    if not loaded.is_json or "duplicate_key" in _warning_codes(loaded):
        return None
    data = (pretty_json(loaded.value) + "\n").encode("utf-8")
    again = load_body(data, content_type)
    same = again.is_json and repr(again.value) == repr(loaded.value) and _warning_codes(again) == _warning_codes(loaded)
    return data if same else None


def stored_body(body: bytes, content_type: str | None) -> StoredBody:
    """扩展名：Content-Type 是 JSON 时 .json（不论是否格式化）；否则能按 UTF-8 解码时 .txt，不能时 .bin。"""
    if is_json_media(content_type):
        data = formatted_json(body, content_type)
        return StoredBody(body, False, "json") if data is None else StoredBody(data, True, "json")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return StoredBody(body, False, "bin")
    return StoredBody(body, False, "txt")


def _body_fields(sample: Sample, index: int) -> tuple[dict[str, Any], StoredBody | None]:
    if not sample.body:
        return {"body_size": 0}, None
    stored = stored_body(sample.body, sample.content_type)
    fields = {
        "body_file": f"{index + 1}.{stored.suffix}",
        "body_formatted": stored.formatted,
        "body_size": len(stored.data),
        "body_sha256": hashlib.sha256(stored.data).hexdigest(),
    }
    return fields, stored


def to_record(sample: Sample, compare_headers: Iterable[str], index: int) -> tuple[dict[str, Any], StoredBody | None]:
    """第 index 个 sample（从 0 开始）的记录，以及要写进 body 文件的内容（body 为空或网络错误时为 None）。"""
    record: dict[str, Any] = {"url": sample.url, "status": sample.status}
    if sample.failed:
        return {**record, "error": sample.error, "retries": sample.retries}, None
    body, stored = _body_fields(sample, index)
    record = {
        **record,
        "headers": kept_headers(sample.headers, compare_headers),
        **body,
        "elapsed_ms": sample.elapsed_ms,
        "retries": sample.retries,
    }
    return record, stored


class SampleFormatError(ValueError):
    pass


class LegacyFormatError(SampleFormatError):
    """旧格式：body 内嵌在请求文件里（`body` / `body_b64`）。"""


def _check_body_file(name: Any, index: int) -> str:
    """body_file 只能是约定的文件名，序号等于 sample 的位置；在拼路径之前检查，请求文件里写不出路径穿越。"""
    match = BODY_FILE.fullmatch(name) if isinstance(name, str) else None
    if match is None or int(match.group(1)) != index + 1:
        raise SampleFormatError(f"第 {index + 1} 个 sample 的 body_file {name!r} 不合法，应为 {index + 1}.json、.txt 或 .bin")
    return name


def _read_body_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise SampleFormatError(f"缺少 body 文件 {path}") from None
    except OSError as exc:
        raise SampleFormatError(f"body 文件 {path} 读不出来（{exc.strerror or exc}）") from None


def _record_body(record: Mapping[str, Any], index: int, body_dir: Path) -> bytes:
    if "body" in record or "body_b64" in record:
        raise LegacyFormatError("是旧格式（body 内嵌在请求文件里）")
    size = record.get("body_size", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SampleFormatError("body_size 必须是非负整数")
    if "body_file" not in record:
        if size:
            raise SampleFormatError(f"第 {index + 1} 个 sample 的 body_size 是 {size}，但没有 body_file")
        return b""
    path = body_dir / _check_body_file(record["body_file"], index)
    data = _read_body_file(path)
    if len(data) != size or hashlib.sha256(data).hexdigest() != record.get("body_sha256"):
        raise SampleFormatError(f"body 文件 {path} 的大小或 SHA-256 和请求文件里记录的不一致")
    return data


def _optional(record: Mapping[str, Any], key: str, kinds: tuple[type, ...], what: str) -> Any:
    value = record.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, kinds)):
        raise SampleFormatError(f"{key} 必须是{what}或 null")
    return value


def _headers(record: Mapping[str, Any]) -> dict[str, str]:
    headers = record.get("headers", {})
    if not isinstance(headers, Mapping) or not all(isinstance(v, str) for v in headers.values()):
        raise SampleFormatError("headers 必须是字符串到字符串的映射")
    return {str(k).lower(): v for k, v in headers.items()}


def from_record(record: Any, index: int, body_dir: Path) -> Sample:
    """to_record 的逆操作：body 从 body_dir 里读回并校验。格式不对、body 文件缺失或对不上时一律抛 SampleFormatError。"""
    if not isinstance(record, Mapping) or not isinstance(record.get("url"), str):
        raise SampleFormatError("sample 必须是带 url 的对象")
    retries = record.get("retries", 0)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise SampleFormatError("retries 必须是非负整数")
    return Sample(
        url=record["url"],
        status=_optional(record, "status", (int,), "整数"),
        headers=_headers(record),
        body=_record_body(record, index, body_dir),
        elapsed_ms=_optional(record, "elapsed_ms", (int, float), "数字"),
        retries=retries,
        error=_optional(record, "error", (str,), "字符串"),
    )


# ---------- 一条请求的文件：run 目录的 baseline/、candidate/ 和快照的 requests/ 共用 ----------


@dataclass
class SideFile:
    name: str
    fingerprint: str
    fingerprint_version: int
    samples: list[Sample]


def dump_json(path: Path, data: Any) -> None:
    """落盘的 JSON 统一用这种格式（run 目录和快照共用）：UTF-8、不转义非 ASCII、缩进 2、末尾换行。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_side_file(path: Path, body_dir: Path, name: str, fingerprint: str, samples: Iterable[Sample],
                    compare_headers: Iterable[str]) -> None:  # fmt: skip
    """写一条请求的文件和它的 body 文件。body 文件先写，请求文件最后写（请求文件引用 body 文件）。"""
    keep = list(compare_headers)
    records = []
    for index, sample in enumerate(samples):
        record, stored = to_record(sample, keep, index)
        if stored is not None:
            body_dir.mkdir(parents=True, exist_ok=True)
            (body_dir / record["body_file"]).write_bytes(stored.data)
        records.append(record)
    document = {"name": name, "fingerprint": fingerprint, "fingerprint_version": FINGERPRINT_VERSION, "samples": records}
    dump_json(path, document)


def parse_side_document(data: Any, body_dir: Path) -> SideFile:
    """write_side_file 的逆操作：data 是请求文件解析后的内容，body 从 body_dir 读回。格式不对时抛 SampleFormatError。"""
    if not isinstance(data, Mapping):
        raise SampleFormatError("请求文件必须是 JSON 对象")
    name, fp, version, samples = (data.get(k) for k in ("name", "fingerprint", "fingerprint_version", "samples"))
    if not isinstance(name, str) or not isinstance(fp, str):
        raise SampleFormatError("name、fingerprint 必须是字符串")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SampleFormatError("fingerprint_version 必须是整数")
    if not isinstance(samples, list):
        raise SampleFormatError("samples 必须是列表")
    return SideFile(name, fp, version, [from_record(r, i, body_dir) for i, r in enumerate(samples)])


def _names(headers: Any) -> set[str]:
    return {str(name).lower() for name in headers} if isinstance(headers, Mapping) else set()


def fingerprint(raw: Mapping[str, Any], index: int) -> str:
    """按 `${VAR}` 替换前的模板计算：method、path、排序后的 query、defaults 和请求级的 header 名（小写、排序）。

    target 级的 headers 和 base_url 不参与，所以换环境、换 token 不会让指纹变化。"""
    defaults = raw.get("defaults") or {}
    request = raw["requests"][index]
    query = request.get("query") or {}
    data = {
        "method": str(request["method"]).upper(),
        "path": str(request["path"]),
        "query": sorted([str(k), str(v)] for k, v in query.items()),
        "headers": sorted(_names(defaults.get("headers")) | _names(request.get("headers"))),
    }
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
