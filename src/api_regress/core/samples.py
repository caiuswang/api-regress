"""sample（一条请求的一次响应）：内存表示、落盘格式（单个 sample 和一条请求的文件）、请求指纹。

run 目录的 `baseline/`、`candidate/` 和快照共用同一种落盘格式。落盘时只保留 `Content-Type`
和 `compare_headers` 列出的响应头，请求头一律不保存。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def _body_fields(body: bytes) -> dict[str, str]:
    """优先存 UTF-8 原文；解码失败时存 base64。"""
    try:
        return {"body": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"body_b64": base64.b64encode(body).decode("ascii")}


def to_record(sample: Sample, compare_headers: Iterable[str]) -> dict[str, Any]:
    record: dict[str, Any] = {"url": sample.url, "status": sample.status}
    if sample.failed:
        return {**record, "error": sample.error, "retries": sample.retries}
    return {
        **record,
        "headers": kept_headers(sample.headers, compare_headers),
        **_body_fields(sample.body),
        "elapsed_ms": sample.elapsed_ms,
        "retries": sample.retries,
    }


class SampleFormatError(ValueError):
    pass


def _record_body(record: Mapping[str, Any]) -> bytes:
    if "body_b64" in record:
        try:
            return base64.b64decode(record["body_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise SampleFormatError(f"body_b64 不是合法的 base64：{exc}") from None
    body = record.get("body", "")
    if not isinstance(body, str):
        raise SampleFormatError("body 必须是字符串")
    try:
        return body.encode("utf-8")
    except UnicodeEncodeError:
        raise SampleFormatError("body 含有不能编码成 UTF-8 的字符") from None


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


def from_record(record: Any) -> Sample:
    """to_record 的逆操作；格式不对时一律抛 SampleFormatError。"""
    if not isinstance(record, Mapping) or not isinstance(record.get("url"), str):
        raise SampleFormatError("sample 必须是带 url 的对象")
    retries = record.get("retries", 0)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise SampleFormatError("retries 必须是非负整数")
    return Sample(
        url=record["url"],
        status=_optional(record, "status", (int,), "整数"),
        headers=_headers(record),
        body=_record_body(record),
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


def side_document(name: str, fingerprint: str, samples: Iterable[Sample], compare_headers: Iterable[str]) -> dict:
    keep = list(compare_headers)
    return {
        "name": name,
        "fingerprint": fingerprint,
        "fingerprint_version": FINGERPRINT_VERSION,
        "samples": [to_record(s, keep) for s in samples],
    }


def parse_side_document(data: Any) -> SideFile:
    """side_document 的逆操作；格式不对时抛 SampleFormatError。"""
    if not isinstance(data, Mapping):
        raise SampleFormatError("请求文件必须是 JSON 对象")
    name, fp, version, samples = (data.get(k) for k in ("name", "fingerprint", "fingerprint_version", "samples"))
    if not isinstance(name, str) or not isinstance(fp, str):
        raise SampleFormatError("name、fingerprint 必须是字符串")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SampleFormatError("fingerprint_version 必须是整数")
    if not isinstance(samples, list):
        raise SampleFormatError("samples 必须是列表")
    return SideFile(name, fp, version, [from_record(r) for r in samples])


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
