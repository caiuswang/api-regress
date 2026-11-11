"""本地 demo 服务（只用标准库）：`/v1/...` 是基线，`/v2/...` 是带回归问题的候选。数据固定，不可配置。"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from api_regress.demo.data import DATASETS

log = logging.getLogger(__name__)

DEFAULT_PORT = 18765
MAX_DELAY_MS = 10_000
_DECIMAL_MARK = "\x00decimal:"
_DECIMAL_RE = re.compile(r'"\\u0000decimal:([-+0-9.eE]+)"')


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    """JSON 值；`str` 按 text/plain 返回。"""
    headers: dict[str, str] = field(default_factory=dict)


def dumps(value: Any) -> bytes:
    """序列化 JSON，Decimal 原样输出（保留 `12.50` 末尾的 0）。"""

    def default(o: Any) -> str:
        if isinstance(o, Decimal):
            return _DECIMAL_MARK + str(o)
        raise TypeError(f"无法序列化 {type(o).__name__}")

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=default)
    return _DECIMAL_RE.sub(r"\1", text).encode()


def _envelope(data: Any, code: int = 0, msg: str = "ok") -> dict[str, Any]:
    """业务接口的统一外壳；request_id、server_time 每次都变，用来演示"建议 ignore"。"""
    return {
        "code": code,
        "msg": msg,
        "request_id": uuid.uuid4().hex,
        "server_time": datetime.now(UTC).isoformat(timespec="microseconds"),
        "data": data,
    }


def _int_arg(query: dict[str, list[str]], name: str, default: int) -> int | None:
    raw = query.get(name, [str(default)])[0]
    return int(raw) if raw.isdigit() else None


Handler = Callable[[dict[str, Any], re.Match[str], dict[str, list[str]]], Reply]


def _health(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    return Reply(body={"status": "UP"})


def _product_list(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    limit, skip = _int_arg(q, "limit", 10), _int_arg(q, "skip", 0)
    if limit is None or skip is None:
        return Reply(400, _envelope(None, 400, "limit、skip 必须是非负整数"))
    products = ds["products"]
    page = {"total": len(products), "skip": skip, "limit": limit, "products": products[skip : skip + limit]}
    return Reply(body=_envelope(page))


def _product_detail(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    product = next((p for p in ds["products"] if p["id"] == int(m["id"])), None)
    if product is None:
        return Reply(404, _envelope(None, 404, "商品不存在"))
    return Reply(body=_envelope(product))


def _order_detail(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    if m["id"] != "10086":
        return Reply(404, _envelope(None, 404, "订单不存在"))
    order = dict(ds["order"], updated_at=datetime.now(UTC).isoformat(timespec="microseconds"))
    return Reply(body=_envelope(order))


def _users(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    return Reply(body=ds["users"])


def _inventory(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    if ds["inventory"] is None:
        return Reply(body=_envelope(None, 1001, "库存服务超时"))
    return Reply(body=_envelope(ds["inventory"]))


def _daily_report(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    if ds["daily_report"] is None:
        return Reply(500, _envelope(None, 500, "internal error"))
    return Reply(body=_envelope(ds["daily_report"]))


def _config(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    return Reply(body=_envelope(ds["config"]), headers={"X-Api-Version": ds["api_version"]})


def _legacy(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    return Reply(body="OK")


def _status(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    code = int(m["code"])
    if not 200 <= code <= 599:
        return Reply(400, _envelope(None, 400, "状态码必须在 200–599 之间"))
    return Reply(code, None if code == 204 else {"status": code})


def _delay(ds: dict[str, Any], m: re.Match[str], q: dict[str, list[str]]) -> Reply:
    ms = min(int(m["ms"]), MAX_DELAY_MS)
    time.sleep(ms / 1000)
    return Reply(body={"delayed_ms": ms})


ROUTES: list[tuple[re.Pattern[str], Handler]] = [
    (re.compile(p), h)
    for p, h in [
        (r"/health", _health),
        (r"/products", _product_list),
        (r"/products/(?P<id>\d+)", _product_detail),
        (r"/orders/(?P<id>\d+)", _order_detail),
        (r"/users", _users),
        (r"/inventory", _inventory),
        (r"/reports/daily", _daily_report),
        (r"/config", _config),
        (r"/legacy", _legacy),
        (r"/status/(?P<code>\d{3})", _status),
        (r"/delay/(?P<ms>\d{1,6})", _delay),
    ]
]
_VERSIONED = re.compile(r"/(?P<ver>v[12])(?P<rest>/.*)")


def dispatch(target: str) -> Reply:
    """按 `/v1|v2/<路由>` 分发；版本或路由不存在时返回 404。"""
    parts = urlsplit(target)
    versioned = _VERSIONED.fullmatch(parts.path)
    if versioned is None:
        return Reply(404, {"error": "路径要以 /v1 或 /v2 开头", "path": parts.path})
    rest = versioned["rest"].rstrip("/") or "/"
    for pattern, handler in ROUTES:
        m = pattern.fullmatch(rest)
        if m is not None:
            return handler(DATASETS[versioned["ver"]], m, parse_qs(parts.query))
    return Reply(404, {"error": "没有这个接口", "path": parts.path})


def encode(reply: Reply) -> tuple[bytes, str | None]:
    if reply.body is None:
        return b"", None
    if isinstance(reply.body, str):
        return reply.body.encode(), "text/plain; charset=utf-8"
    return dumps(reply.body), "application/json; charset=utf-8"


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "api-regress-demo"

    def do_GET(self) -> None:  # noqa: N802
        self._respond(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(send_body=False)

    def _respond(self, send_body: bool) -> None:
        reply = dispatch(self.path)
        body, content_type = encode(reply)
        self.send_response(reply.status)
        # HEAD 只回状态码，不带 Content-Type，和 body 为空保持一致
        if content_type is not None and send_body:
            self.send_header("Content-Type", content_type)
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body) if send_body else 0))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 只记请求行和状态码，不记请求头
        log.info("%s %s", self.address_string(), format % args)


class DemoServer(ThreadingHTTPServer):
    # 不开 SO_REUSEADDR：macOS 上开了以后，端口被别的进程以通配地址占用时仍能绑定成功，请求会被抢走
    allow_reuse_address = False
    daemon_threads = True


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> DemoServer:
    """创建（还没开始服务的）demo 服务；`port=0` 时由系统分配端口。端口被占用时抛 OSError。"""
    return DemoServer((host, port), DemoHandler)


def base_url(server: DemoServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"
