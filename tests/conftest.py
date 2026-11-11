import pytest

_PROXY_VARS = ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "all_proxy", "http_proxy", "https_proxy", "no_proxy")


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """开发机上的代理环境变量会让发往本地 mock 服务的请求绕到代理上。"""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
