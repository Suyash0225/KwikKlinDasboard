"""Small WAHA adapter used by the application WhatsApp gateway.

WAHA is the transport only; business logic continues to use
app.services.whatsapp.send_message(). No Meta 24-hour window/template rules
apply when WHATSAPP_PROVIDER=waha.
"""

import base64
from pathlib import Path

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


def _message_id(data: dict) -> str:
    # WAHA has returned both id and nested ids across versions.
    mid = data.get("id") or (data.get("data") or {}).get("id")
    if isinstance(mid, dict):
        mid = mid.get("_serialized") or mid.get("id")
    return str(mid or data.get("messageId") or data.get("key") or "")


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


async def send_list(phone: str, text: str, rows: list[dict], *, button: str = "Choose") -> str:
    payload = {
        "session": settings.WAHA_SESSION,
        "chatId": chat_id(phone),
        "description": text,
        "button": button,
        "sections": [{"title": "Options", "rows": rows}],
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
