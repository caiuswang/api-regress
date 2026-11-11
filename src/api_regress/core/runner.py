"""发请求：httpx.AsyncClient + 信号量控制并发，不跟随重定向。

重试自己实现，只针对连接错误和超时：httpx transport 自带的 retries 不会重试超时。任何状态码都不重试。
日志只写请求头的名字，不写值。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from api_regress.core.config import Request, Suite
from api_regress.core.models import Side
from api_regress.core.samples import Sample

log = logging.getLogger(__name__)

RETRYABLE = (httpx.ConnectError, httpx.TimeoutException, TimeoutError)
_DEBUG_BODY_BYTES = 2048
_ERROR_LIMIT = 300


@dataclass(frozen=True)
class Call:
    """一次要发出的请求。headers 的值可能含 token，只能用来发送，不能写进日志或结果。"""

    side: Side
    name: str
    method: str
    url: str
    headers: Mapping[str, str]


def build_url(base_url: str, path: str, query: Mapping[str, str]) -> str:
    url = httpx.URL(base_url.rstrip("/") + "/" + path.lstrip("/"))
    return str(url.copy_merge_params(dict(query)) if query else url)


def merge_headers(*layers: Mapping[str, str]) -> dict[str, str]:
    """后面的覆盖前面的，名字不区分大小写，保留最后一次的写法。"""
    merged: dict[str, tuple[str, str]] = {}
    for layer in layers:
        for name, value in layer.items():
            merged[name.lower()] = (name, value)
    return dict(merged.values())


def make_call(suite: Suite, request: Request, side: Side, base_url: str | None = None) -> Call:
    """请求头按 defaults → target → 请求级的顺序合并，越具体越优先。"""
    target = getattr(suite.targets, side)
    try:
        url = build_url(base_url or target.base_url, request.path, request.query)
    except httpx.InvalidURL as exc:
        url = f"{base_url or target.base_url}{request.path}"
        log.debug("URL 不合法：%s（%s）", url, exc)
    headers = merge_headers(suite.defaults.headers, target.headers, request.headers)
    return Call(side=side, name=request.name, method=request.method, url=url, headers=headers)


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: ", ".join(headers.get_list(name)) for name in headers.keys()}


# 这些异常的消息来自对端或网络，可以带上；其他异常（LocalProtocolError、UnicodeEncodeError 等）
# 是发送前校验请求时抛的，消息里会带着出问题的请求头的值，只能用固定的说明。
_DETAILED = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError)
_FIXED: tuple[tuple[type[BaseException], str], ...] = (
    (TimeoutError, "总耗时超过 timeout"),
    (httpx.LocalProtocolError, "请求不合法，可能是请求头的值含空格、换行等非法字符"),
    (UnicodeError, "请求头或 URL 含有不能编码的字符"),
    (httpx.InvalidURL, "URL 不合法"),
)


def _redact(text: str, call: Call) -> str | None:
    """消息里出现任何一个请求头的值（含去掉首尾空白后的写法）就整条丢掉。"""
    values = {v for value in call.headers.values() for v in (value, value.strip()) if v}
    return None if any(v in text for v in values) else text


def describe_error(exc: BaseException, call: Call) -> str:
    """失败原因：异常类型加说明。会进日志、stdout 和 result.json，所以绝不能含请求头的值。"""
    name = type(exc).__name__
    fixed = next((text for kind, text in _FIXED if isinstance(exc, kind)), None)
    if fixed is not None:
        return f"{name}: {fixed}"
    detail = _redact(str(exc), call) if isinstance(exc, _DETAILED) else None
    return f"{name}: {detail}"[:_ERROR_LIMIT] if detail else name


class Runner:
    def __init__(self, client: httpx.AsyncClient, concurrency: int, retries: int, timeout: float):
        self.client = client
        self.retries = retries
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)

    async def fetch(self, call: Call) -> Sample:
        async with self._semaphore:
            return await self._fetch(call)

    async def _fetch(self, call: Call) -> Sample:
        log.info("→ %s %s %s %s（请求头：%s）", call.name, call.side, call.method, call.url, ", ".join(call.headers) or "无")
        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                # httpx 的 timeout 只限制单次读写，慢慢吐数据的响应能拖很久，所以再加一个总时限
                async with asyncio.timeout(self.timeout):
                    response = await self.client.request(call.method, call.url, headers=dict(call.headers))
            except RETRYABLE as exc:
                if attempt >= self.retries:
                    return self._failed(call, attempt, exc)
                attempt += 1
                log.warning("%s %s %s 失败（%s），第 %d 次重试", call.name, call.side, call.url, type(exc).__name__, attempt)
                continue
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError) as exc:
                return self._failed(call, attempt, exc)
            return self._done(call, response, started, attempt)

    @staticmethod
    def _failed(call: Call, retries: int, exc: BaseException) -> Sample:
        error = describe_error(exc, call)
        log.info("← %s %s %s %s 失败：%s，重试 %d 次", call.name, call.side, call.method, call.url, error, retries)
        return Sample(url=call.url, retries=retries, error=error)

    @staticmethod
    def _done(call: Call, response: httpx.Response, started: float, retries: int) -> Sample:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        sample = Sample(
            url=call.url,
            status=response.status_code,
            headers=_response_headers(response.headers),
            body=response.content,
            elapsed_ms=elapsed,
            retries=retries,
        )
        log.info(
            "← %s %s %s %s %d，%.1f ms，重试 %d 次",
            call.name, call.side, call.method, call.url, sample.status, elapsed, retries,
        )  # fmt: skip
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s %s 响应体前 %d 字节：%r", call.name, call.side, _DEBUG_BODY_BYTES, sample.body[:_DEBUG_BODY_BYTES])
        return sample


def new_client(timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(timeout), transport=transport)
