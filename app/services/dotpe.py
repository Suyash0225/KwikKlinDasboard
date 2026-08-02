"""DotPe BSP adapter — sends WhatsApp messages through DotPe's API.

Built from "DotPe WhatsApp APIs" PDF (owner-provided). Used only when
WHATSAPP_PROVIDER=dotpe; the single outbound door (whatsapp.send_message)
delegates here, so window checks / conversation logging stay in one place.

Known DotPe limitations vs Meta direct (from their docs):
- NO interactive reply buttons in free-form sends (text/media/location only).
  Phase 3.5 staff flows will use numbered text menus on this provider.
- No per-message wa_message_id: responses carry a jobId per call. We use our
  own clientRefId (uuid) as the stored message id: "dotpe:<uuid>".
- Templates are managed in the DotPe panel; names there must match our
  templates.py registry entries.
"""

import asyncio
import uuid

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

BASE_URL = "https://api.dotpe.in/api/comm/public/enterprise/v1"


class DotpeError(Exception):
    """DotPe API rejected or failed a request."""


def _headers() -> dict[str, str]:
    if not settings.DOTPE_API_KEY:
        raise DotpeError("DOTPE_API_KEY is not set — cannot call DotPe")
    return {"Dotpe-Api-Key": settings.DOTPE_API_KEY, "Content-Type": "application/json"}


def _require_waba() -> str:
    if not settings.DOTPE_WABA_NUMBER:
        raise DotpeError("DOTPE_WABA_NUMBER is not set — cannot call DotPe")
    return settings.DOTPE_WABA_NUMBER


async def send_text(to_phone: str, body: str) -> str:
    """Free-form text (inside the 24h window). Returns our message id."""
    ref = f"kk-{uuid.uuid4().hex}"
    payload = {
        "wabaNumber": _require_waba(),
        "recipient": to_phone.lstrip("+"),
        "source": "laundry-bot",
        "clientRefId": ref,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    data = await _post_with_retry("/wa/send/free-form", payload, to_phone)
    log.info("dotpe_text_sent", to=to_phone, job_id=data.get("jobId"), ref=ref)
    return f"dotpe:{ref}"


async def send_template(
    to_phone: str, template_name: str, language: str, params: list[str] | None = None
) -> str:
    """Template send (works outside the window). Returns our message id."""
    ref = f"kk-{uuid.uuid4().hex}"
    payload: dict = {
        "template": {"name": template_name, "language": language},
        "source": "laundry-bot",
        "wabaNumber": _require_waba(),
        "recipients": [to_phone.lstrip("+")],
        "clientRefId": ref,
    }
    if params:
        payload["params"] = {"body": params}
    data = await _post_with_retry("/wa/send", payload, to_phone)
    log.info(
        "dotpe_template_sent",
        to=to_phone,
        template=template_name,
        job_id=data.get("jobId"),
        ref=ref,
    )
    return f"dotpe:{ref}"


async def list_templates() -> list[dict]:
    """Fetch templates registered on the DotPe WABA (for setup/debugging)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{BASE_URL}/templates", headers=_headers())
    except httpx.TransportError as exc:
        raise DotpeError(f"network error listing templates: {exc}") from exc
    body = r.json()
    if r.status_code >= 400 or not body.get("status", False):
        raise DotpeError(f"list templates failed: {r.status_code} {body}")
    return body.get("data", [])


async def _post_with_retry(path: str, payload: dict, to_phone: str) -> dict:
    """POST to DotPe. One retry on network error / 5xx, then raise DotpeError."""
    last_error = "unknown"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{BASE_URL}{path}", headers=_headers(), json=payload
                )
        except httpx.TransportError as exc:
            last_error = f"network error: {exc}"
            log.warning("dotpe_network_error", to=to_phone, attempt=attempt, error=str(exc))
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break

        try:
            body = r.json()
        except ValueError:
            body = {}

        if r.status_code < 400 and body.get("status", False):
            return body

        last_error = f"{r.status_code} {body.get('message')} {body.get('error', '')}".strip()
        if r.status_code >= 500:
            log.warning("dotpe_5xx", to=to_phone, attempt=attempt, error=last_error)
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break
        log.error("dotpe_rejected", to=to_phone, error=last_error)
        break

    raise DotpeError(f"DotPe send to {to_phone} failed: {last_error}")
