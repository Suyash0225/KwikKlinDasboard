"""Small WAHA adapter used by the application WhatsApp gateway.

WAHA is the transport only; business logic continues to use
app.services.whatsapp.send_message(). No Meta 24-hour window/template rules
apply when WHATSAPP_PROVIDER=waha.
"""

import base64
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import settings


class WahaError(Exception):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def enabled() -> bool:
    return bool(settings.WAHA_BASE_URL)


def _headers() -> dict[str, str]:
    out = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.WAHA_API_KEY:
        out["X-Api-Key"] = settings.WAHA_API_KEY
    return out


def chat_id(phone: str) -> str:
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return f"{digits}@c.us"


def _url(path: str) -> str:
    base = settings.WAHA_BASE_URL.rstrip("/")
    if not base:
        raise WahaError("WAHA_BASE_URL is not configured", transient=False)
    return f"{base}{path}"


async def _get(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(_url(path), headers=_headers())
    except httpx.TransportError as exc:
        raise WahaError(f"WAHA network error: {exc}", transient=True) from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise WahaError(f"WAHA temporary error {r.status_code}: {r.text[:300]}", transient=True)
    if r.status_code >= 400:
        raise WahaError(f"WAHA rejected request {r.status_code}: {r.text[:500]}", transient=False)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


async def _post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(_url(path), headers=_headers(), json=payload)
    except httpx.TransportError as exc:
        raise WahaError(f"WAHA network error: {exc}", transient=True) from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise WahaError(f"WAHA temporary error {r.status_code}: {r.text[:300]}", transient=True)
    if r.status_code >= 400:
        raise WahaError(f"WAHA rejected request {r.status_code}: {r.text[:500]}", transient=False)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _media_request_path(media_url: str) -> str:
    """Convert WAHA media URL to a path on our configured WAHA host.

    Never fetch the host embedded in an inbound webhook; that would allow
    a sender to make the app request arbitrary internal URLs.
    """
    parts = urlsplit(str(media_url or "").strip())
    path_parts = parts.path.split("/")
    if not parts.path.startswith("/api/files/") or ".." in path_parts:
        raise WahaError("WAHA media URL must point to a safe /api/files/ path", transient=False)
    return parts.path + (("?" + parts.query) if parts.query else "")


async def download_media_url(media_url: str) -> tuple[bytes, str | None]:
    """Download an inbound attachment through the configured WAHA host."""
    path = _media_request_path(media_url)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(_url(path), headers=_headers())
    except httpx.TransportError as exc:
        raise WahaError(f"WAHA media network error: {exc}", transient=True) from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise WahaError(
            f"WAHA media temporary error {response.status_code}: {response.text[:300]}",
            transient=True,
        )
    if response.status_code >= 400:
        raise WahaError(
            f"WAHA media rejected request {response.status_code}: {response.text[:300]}",
            transient=False,
        )
    return response.content, response.headers.get("content-type")

async def resolve_lid(lid: str) -> str | None:
    """Resolve a WAHA @lid chat id to its phone-number JID.

    NOWEB can emit @lid for inbound messages. The LIDs API maps that
    identifier to the corresponding @c.us JID when the mapping is known.
    """
    value = str(lid or "").strip()
    if not value:
        return None
    if not value.endswith("@lid"):
        return value if value.endswith("@c.us") else f"{value}@c.us"

    from urllib.parse import quote

    encoded = quote(value, safe="")
    try:
        data = await _get(f"/api/{settings.WAHA_SESSION}/lids/{encoded}")
    except WahaError as exc:
        if exc.transient:
            raise
        return None

    pn = data.get("pn") if isinstance(data, dict) else None
    if isinstance(pn, str) and pn:
        return pn.replace("@s.whatsapp.net", "@c.us")

    return None


def _message_id(data: dict) -> str:
    """Extract the actual WhatsApp message ID from WAHA response variants.

    NOWEB may return the message key itself (remoteJid/fromMe/id), rather
    than a plain string ID. Never stringify that whole key: conversation
    deduplication and delivery receipts require the stable inner ID.
    """
    if not isinstance(data, dict):
        return str(data or "")

    candidates = [
        data.get("id"),
        (data.get("data") or {}).get("id") if isinstance(data.get("data"), dict) else None,
        data.get("messageId"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("_serialized") or candidate.get("id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    # Some NOWEB versions return {key: {remoteJid, fromMe, id: "..."}}
    for parent in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
        key = parent.get("key") if isinstance(parent, dict) else None
        if isinstance(key, dict):
            value = key.get("_serialized") or key.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


async def send_text(
    phone: str,
    text: str,
    *,
    reply_to: str | None = None,
    link_preview: bool = True,
) -> str:
    payload = {
        "session": settings.WAHA_SESSION,
        "chatId": chat_id(phone),
        "text": text,
        "linkPreview": link_preview,
        "linkPreviewHighQuality": bool(link_preview),
    }
    if reply_to:
        payload["reply_to"] = reply_to
    data = await _post("/api/sendText", payload)
    return _message_id(data)


async def send_list(
    phone: str,
    text: str,
    rows: list[dict],
    *,
    button: str = "Choose",
    title: str = "Kwik Klin",
) -> str:
    # WAHA requires a non-empty message.title for sendList, including NOWEB.
    # Keep the customer-facing body in description and use a stable title.
    payload = {
        "session": settings.WAHA_SESSION,
        "chatId": chat_id(phone),
        "message": {
            "title": str(title or "Kwik Klin")[:60],
            "description": text,
            "button": button,
            "sections": [{"title": "Options", "rows": rows}],
        },
    }
    data = await _post("/api/sendList", payload)
    return _message_id(data)


async def send_image(
    phone: str,
    file_path: str,
    *,
    mime_type: str = "image/jpeg",
    caption: str | None = None,
) -> str:
    raw = Path(file_path).read_bytes()
    payload = {
        "session": settings.WAHA_SESSION,
        "chatId": chat_id(phone),
        "file": {
            "mimetype": mime_type,
            "filename": Path(file_path).name,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }
    if caption:
        payload["caption"] = caption
    data = await _post("/api/sendImage", payload)
    return _message_id(data)


async def send_link_preview(
    phone: str,
    text: str,
    *,
    preview_url: str,
    title: str,
    description: str,
    image_url: str | None = None,
) -> str:
    preview = {
        "url": preview_url,
        "title": title[:120],
        "description": description[:240],
    }
    if image_url:
        preview["image"] = {"url": image_url}
    data = await _post(
        "/api/send/link-custom-preview",
        {
            "session": settings.WAHA_SESSION,
            "chatId": chat_id(phone),
            "text": text,
            "linkPreviewHighQuality": True,
            "preview": preview,
        },
    )
    return _message_id(data)
