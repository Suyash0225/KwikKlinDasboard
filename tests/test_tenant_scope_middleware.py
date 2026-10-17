"""HTTP tenant-context regression tests for anonymous and admin-key requests."""

from contextlib import asynccontextmanager
import uuid

from starlette.requests import Request
from starlette.responses import Response

from app import main
from app.config import settings
from app.services import tenant_context


def _request(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    raw_path = path.encode("ascii")
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", b"testserver")],
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    })


@asynccontextmanager
async def _fake_as_tenant(tid):
    tenant_token = tenant_context.current_tenant_id.set(tid)
    system_token = tenant_context.current_system_context.set(False)
    try:
        yield
    finally:
        tenant_context.current_system_context.reset(system_token)
        tenant_context.current_tenant_id.reset(tenant_token)


async def test_anonymous_request_has_no_tenant_and_does_not_lookup_home(monkeypatch) -> None:
    tenant_token = tenant_context.current_tenant_id.set(None)
    system_token = tenant_context.current_system_context.set(False)
    monkeypatch.setattr(main, "_rate_limited", lambda *_: False)

    async def unexpected_home_lookup():
        raise AssertionError("anonymous request must not resolve the home tenant")

    monkeypatch.setattr(tenant_context, "get_home_tenant_id", unexpected_home_lookup)

    async def call_next(_request):
        assert tenant_context.current_tenant_id.get() is None
        assert tenant_context.current_system_context.get() is False
        return Response("ok")

    try:
        response = await main.tenant_scope(_request("/api/me"), call_next)
        assert response.status_code == 200
    finally:
        tenant_context.current_system_context.reset(system_token)
        tenant_context.current_tenant_id.reset(tenant_token)


async def test_invalid_admin_key_does_not_get_home_tenant(monkeypatch) -> None:
    tenant_token = tenant_context.current_tenant_id.set(None)
    system_token = tenant_context.current_system_context.set(False)
    monkeypatch.setattr(main, "_rate_limited", lambda *_: False)

    async def unexpected_home_lookup():
        raise AssertionError("invalid API key must not resolve the home tenant")

    monkeypatch.setattr(tenant_context, "get_home_tenant_id", unexpected_home_lookup)

    async def call_next(_request):
        assert tenant_context.current_tenant_id.get() is None
        assert tenant_context.current_system_context.get() is False
        return Response("ok")

    try:
        response = await main.tenant_scope(
            _request("/orders", [(b"host", b"testserver"), (b"x-api-key", b"not-the-key")]),
            call_next,
        )
        assert response.status_code == 200
    finally:
        tenant_context.current_system_context.reset(system_token)
        tenant_context.current_tenant_id.reset(tenant_token)


async def test_valid_admin_api_key_gets_home_tenant_only_on_protected_api(monkeypatch) -> None:
    home_id = uuid.uuid4()
    tenant_token = tenant_context.current_tenant_id.set(None)
    system_token = tenant_context.current_system_context.set(False)
    monkeypatch.setattr(main, "_rate_limited", lambda *_: False)
    monkeypatch.setattr(tenant_context, "get_home_tenant_id", lambda: _async_value(home_id))
    monkeypatch.setattr(tenant_context, "as_tenant", _fake_as_tenant)

    async def call_next(_request):
        assert tenant_context.current_tenant_id.get() == home_id
        assert tenant_context.current_system_context.get() is False
        return Response("ok")

    try:
        response = await main.tenant_scope(
            _request("/orders", [
                (b"host", b"testserver"),
                (b"x-api-key", settings.ADMIN_API_KEY.encode("utf-8")),
            ]),
            call_next,
        )
        assert response.status_code == 200
    finally:
        tenant_context.current_system_context.reset(system_token)
        tenant_context.current_tenant_id.reset(tenant_token)


async def _async_value(value):
    return value
