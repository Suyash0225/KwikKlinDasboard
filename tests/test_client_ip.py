"""Client-IP resolution must ignore forwarding headers from untrusted peers."""

from fastapi import Request

from app.config import settings
from app.utils.client_ip import TrustedProxyMiddleware, get_client_ip, resolve_client_ip


def _request(peer: str, xff: str | None = None, xfp: str | None = None) -> Request:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if xfp is not None:
        headers.append((b"x-forwarded-proto", xfp.encode()))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 12345),
        "server": ("testserver", 80),
    })


def test_untrusted_peer_cannot_spoof_forwarded_ip(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1")
    request = _request("198.51.100.7", "203.0.113.99")
    assert get_client_ip(request) == "198.51.100.7"


def test_trusted_proxy_can_forward_client_ip(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1")
    request = _request("127.0.0.1", "198.51.100.7")
    assert get_client_ip(request) == "198.51.100.7"


def test_forwarded_chain_skips_trusted_proxy_hops(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1,10.0.0.0/8")
    assert resolve_client_ip(
        "127.0.0.1", "203.0.113.99, 10.0.0.8", settings.TRUSTED_PROXIES
    ) == "203.0.113.99"


def test_malformed_forwarded_header_falls_back_to_direct_peer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1")
    request = _request("127.0.0.1", "spoofed, not-an-ip")
    assert get_client_ip(request) == "127.0.0.1"


async def test_trusted_proxy_restores_https_scheme_and_client_ip(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1")
    captured = {}

    async def inner(scope, receive, send):
        captured["client_ip"] = scope.get("client_ip")
        captured["scheme"] = scope.get("scheme")

    middleware = TrustedProxyMiddleware(inner)
    request = _request("127.0.0.1", "198.51.100.7", "https")
    await middleware(request.scope, None, None)
    assert captured == {"client_ip": "198.51.100.7", "scheme": "https"}
