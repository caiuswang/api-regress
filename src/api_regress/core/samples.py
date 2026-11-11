"""sample（一条请求的一次响应）：内存表示、落盘格式、请求指纹。

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
        except (binascii.Error, TypeError) as exc:
            raise SampleFormatError(f"body_b64 不是合法的 base64：{exc}") from None
    body = record.get("body", "")
    if not isinstance(body, str):
        raise SampleFormatError("body 必须是字符串")
    return body.encode("utf-8")


def from_record(record: Mapping[str, Any]) -> Sample:
    """to_record 的逆操作；格式不对时抛 SampleFormatError。"""
    if not isinstance(record, Mapping) or not isinstance(record.get("url"), str):
        raise SampleFormatError("sample 必须是带 url 的对象")
    status = record.get("status")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise SampleFormatError("status 必须是整数或 null")
    headers = record.get("headers", {})
    if not isinstance(headers, Mapping) or not all(isinstance(v, str) for v in headers.values()):
        raise SampleFormatError("headers 必须是字符串到字符串的映射")
    return Sample(
        url=record["url"],
        status=status,
        headers={str(k).lower(): v for k, v in headers.items()},
        body=_record_body(record),
        elapsed_ms=record.get("elapsed_ms"),
        retries=int(record.get("retries", 0)),
        error=record.get("error"),
    )


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
