"""内置 demo 服务：真实起一个本地服务，用自带的两个 suite 跑通，锁住每处故意改坏的地方会报出什么。"""

import threading
from pathlib import Path

import httpx
import pytest

from api_regress import demo
from api_regress.cli import main
from api_regress.core import service
from api_regress.core.service import RunOptions
from api_regress.demo import server as demo_server

# regress.yaml：每条请求预期的 (级别, finding 类型, 路径)
REGRESS_FINDINGS = {
    "health": set(),
    "product-list": {
        ("breaking", "scale_changed", "data.products[*].price"),
        ("breaking", "field_removed", "data.products[*].stock"),
        ("breaking", "type_changed", "data.products[*].dimensions"),
        ("info", "field_added", "data.products[*].brand"),
    },
    "order-detail": {
        ("breaking", "type_changed", "data.order_id"),
        ("breaking", "value_changed", "data.amount"),
        ("info", "field_added", "data.items[sku_id=B02].gift"),
        ("breaking", "assert_failed", "$assert[0]"),
    },
    "users": {
        ("breaking", "field_removed", "[id=1].email"),
        ("info", "field_added", "[id=1].mail"),
        ("breaking", "field_removed", "[id=2].email"),
        ("info", "field_added", "[id=2].mail"),
        ("suspicious", "element_missing", "[id=3]"),
    },
    "inventory": {("breaking", "business_error", "$success")},
    "daily-report": {("breaking", "status_changed", "$status")},
    "config": {("suspicious", "header_changed", "$header.X-Api-Version")},
}


@pytest.fixture(scope="module")
def demo_url():
    server = demo_server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield demo_server.base_url(server)
    server.shutdown()
    server.server_close()


def _run(tmp_path: Path, url: str, name: str) -> service.RunReport:
    suite = tmp_path / name
    suite.write_text(demo.suite_text(name), encoding="utf-8")
    return service.run_sync(suite, RunOptions(out=tmp_path / "out"), env={"DEMO_URL": url})


def test_pass_suite_has_no_findings(tmp_path, demo_url):
    report = _run(tmp_path, demo_url, "pass.yaml")
    assert report.exit_code == 0, report.exit_reason
    result = report.result
    statuses = {r.name: r.status for r in result.requests}
    assert statuses.pop("legacy") == "unsupported"
    assert set(statuses.values()) == {"ok"}
    assert all(not r.findings for r in result.requests)
    by_name = {r.name: r for r in result.requests}
    assert {w.code for w in by_name["order-not-found"].warnings} == {"both_non_2xx"}
    assert {s.path for s in by_name["product-list"].suggestions} == {"request_id", "server_time"}


def test_regress_suite_reports_each_planted_change(tmp_path, demo_url):
    report = _run(tmp_path, demo_url, "regress.yaml")
    assert report.exit_code == 1
    actual = {
        r.name: {(f.level, f.type, f.path) for f in r.findings} for r in report.result.requests
    }
    assert actual == REGRESS_FINDINGS
    assert all(r.status == "ok" for r in report.result.requests)


def test_decimal_keeps_trailing_zero(demo_url):
    text = httpx.get(f"{demo_url}/v1/orders/10086").text
    assert '"amount":12.50' in text and '"price":1.00' in text


@pytest.mark.parametrize(
    ("path", "status"),
    [("/v3/health", 404), ("/v1/nope", 404), ("/v1/products?limit=x", 400), ("/v1/status/204", 204),
     ("/v1/status/100", 400), ("/v1/delay/10", 200)],
)  # fmt: skip
def test_routes(demo_url, path, status):
    assert httpx.get(demo_url + path).status_code == status


def test_head_has_no_body_or_content_type(demo_url):
    response = httpx.head(f"{demo_url}/v1/health")
    assert response.status_code == 200
    assert response.content == b""
    assert "content-type" not in response.headers


def test_port_in_use_is_an_error(demo_url):
    port = int(demo_url.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        demo_server.make_server("127.0.0.1", port)


def test_write_suites_then_refuse_overwrite(tmp_path, capsys):
    assert main(["demo", "--write-suites", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pass.yaml", "regress.yaml"]
    for name in demo.SUITE_NAMES:
        assert service.validate_suite_sync(tmp_path / name).ok
    assert main(["demo", "--write-suites", str(tmp_path)]) == 2
    assert "不覆盖" in capsys.readouterr().err
