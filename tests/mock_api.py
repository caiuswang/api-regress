"""run 测试共用的 mock 服务：httpx.MockTransport 按 (host, path) 返回固定内容。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from api_regress.core import service
from api_regress.core.service import RunOptions

BASE = "http://base.test"
CAND = "http://cand.test"
JSON = "application/json"


def suite_text(requests: str, defaults: str = "", targets: str = "") -> str:
    return (
        "version: 1\n"
        "targets:\n"
        f"  baseline:\n    base_url: {BASE}\n"
        f"  candidate:\n    base_url: {CAND}\n"
        f"{targets}"
        f"defaults:\n  retries: 1\n{defaults}"
        f"requests:\n{requests}"
    )


def req(name: str, path: str = "/x", method: str = "GET", extra: str = "") -> str:
    return f"  - name: {name}\n    method: {method}\n    path: {path}\n{extra}"


@dataclass
class Reply:
    status: int = 200
    body: str | bytes = ""
    content_type: str | None = JSON
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx.Response:
        headers = dict(self.headers)
        if self.content_type is not None:
            headers["Content-Type"] = self.content_type
        content = self.body.encode() if isinstance(self.body, str) else self.body
        return httpx.Response(self.status, content=content, headers=headers)


def j(body, status: int = 200, **kw) -> Reply:
    return Reply(status=status, body=body if isinstance(body, str) else json.dumps(body), **kw)


class Server:
    """按 (host, path) 返回固定内容；给多个 Reply 时按顺序依次返回，最后一个重复使用。"""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def on(self, side: str, path: str, *replies) -> Server:
        self.routes[(side, path)] = list(replies)
        return self

    def both(self, path: str, reply) -> Server:
        return self.on("base", path, reply).on("cand", path, reply)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        side = request.url.host.split(".")[0]
        replies = self.routes.get((side, request.url.path))
        if not replies:
            return httpx.Response(599, text="no route")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply.response()

    def count(self, side: str, path: str) -> int:
        return sum(1 for r in self.calls if r.url.host.startswith(side) and r.url.path == path)


def run(tmp_path: Path, text: str, server: Server, env=None, **opts) -> service.RunReport:
    tmp_path.mkdir(parents=True, exist_ok=True)
    suite = tmp_path / "suite.yaml"
    suite.write_text(text)
    opts.setdefault("out", tmp_path / "out")
    return service.run_sync(suite, RunOptions(**opts), env=env or {}, transport=server.transport)


def only(report: service.RunReport, name: str | None = None):
    assert report.result is not None
    requests = report.result.requests
    return requests[0] if name is None else next(r for r in requests if r.name == name)


def types(request_result) -> list[str]:
    return [f.type for f in request_result.findings]
