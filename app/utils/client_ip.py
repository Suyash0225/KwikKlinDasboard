"""Resolve client IPs without trusting spoofable forwarding headers."""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Callable

from starlette.datastructures import Headers
from starlette.requests import Request

from app.config import settings

_Network = IPv4Network | IPv6Network


@lru_cache(maxsize=16)
def _trusted_networks(raw: str) -> tuple[_Network, ...]:
    """Parse the comma-separated TRUSTED_PROXIES setting once per value."""
    networks: list[_Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"Invalid TRUSTED_PROXIES entry: {entry}") from exc
    return tuple(networks)


def _is_trusted(ip, networks: tuple[_Network, ...]) -> bool:
    for network in networks:
        if ip.version == network.version and ip in network:
            return True
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and mapped.version == network.version and mapped in network:
            return True
    return False


def resolve_client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxies: str,
) -> str | None:
    """Resolve X-Forwarded-For only when the immediate peer is trusted.

    Walk the forwarding chain from right to left, skipping trusted proxy hops.
    The first untrusted address is the client. Invalid headers fail closed to
    the direct peer rather than allowing a caller to choose an arbitrary IP.
    """
    if not peer:
        return None
    try:
        peer_ip = ip_address(peer)
    except ValueError:
        # Test clients and non-IP ASGI peers are returned as-is; their headers
        # are not trusted because they cannot match an IP/CIDR proxy entry.
        return peer

    networks = _trusted_networks(trusted_proxies)
    if not _is_trusted(peer_ip, networks) or not forwarded_for:
        return peer_ip.compressed

    parts = [part.strip() for part in forwarded_for.split(",")]
    if not parts or any(not part for part in parts):
        return peer_ip.compressed
    try:
        forwarded_ips = [ip_address(part) for part in parts]
    except ValueError:
        return peer_ip.compressed

    for candidate in reversed(forwarded_ips):
        if not _is_trusted(candidate, networks):
            return candidate.compressed
    return peer_ip.compressed


def get_client_ip(request: Request) -> str | None:
    """Use the trusted-proxy middleware result, or resolve a raw ASGI request."""
    if "client_ip" in request.scope:
        return request.scope["client_ip"]
    peer = request.client.host if request.client else None
    return resolve_client_ip(
        peer,
        request.headers.get("x-forwarded-for"),
        settings.TRUSTED_PROXIES,
    )


class TrustedProxyMiddleware:
    """Honor forwarding headers only when the direct ASGI peer is trusted.

    Uvicorn proxy-header rewriting must be disabled so the original peer stays
    visible here. This middleware also restores the public scheme for HTTPS
    requests forwarded by the trusted reverse proxy.
    """

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            peer = scope.get("client")
            peer_host = peer[0] if peer else None
            try:
                peer_ip = ip_address(peer_host) if peer_host else None
            except ValueError:
                peer_ip = None
            networks = _trusted_networks(settings.TRUSTED_PROXIES)
            trusted_peer = peer_ip is not None and _is_trusted(peer_ip, networks)
            headers = Headers(scope=scope)
            scope["client_ip"] = resolve_client_ip(
                peer_host,
                headers.get("x-forwarded-for"),
                settings.TRUSTED_PROXIES,
            )
            if trusted_peer:
                forwarded_proto = headers.get("x-forwarded-proto", "")
                proto = forwarded_proto.split(",", 1)[0].strip().lower()
                if proto in {"http", "https"}:
                    scope["scheme"] = proto
        await self.app(scope, receive, send)
