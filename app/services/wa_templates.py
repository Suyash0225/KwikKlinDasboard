"""Meta par WhatsApp templates — HAR DUKAAN apne WABA + token se.

Pehle Template Studio .env ka WABA/token use karta tha: har dukaan ka
template home dukaan ke account par ban jaata aur har dukaan home ki list
dekhti. Ab creds hamesha us dukaan ke (tenants.wa_*); .env wale sirf home
dukaan ke liye. Owner dashboard (Template Studio) aur vendor Control panel
dono isi file se chalte hain.
"""

import re
from typing import NamedTuple

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = structlog.get_logger()

GRAPH = "https://graph.facebook.com/v21.0"


class Creds(NamedTuple):
    token: str
    waba_id: str
    phone_number_id: str


class TemplateError(Exception):
    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail, self.status = detail, status


async def graph(method: str, path: str, token: str, **kw):
    """Ek Graph call — tests isi ko nakli banate hain."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(
            method, f"{GRAPH}/{path}", headers={"Authorization": f"Bearer {token}"}, **kw
        )
    return r.status_code, r.json()


def creds_for(tenant) -> Creds | None:
    """Dukaan ke apne creds; .env wale sirf home dukaan ko. Na hon to None."""
    from app.services import tenant_context

    if tenant is not None and tenant.wa_token and tenant.wa_waba_id and tenant.wa_phone_number_id:
        return Creds(tenant.wa_token, tenant.wa_waba_id, tenant.wa_phone_number_id)
    if tenant is not None and tenant.id != tenant_context.cached_home_tenant_id():
        return None
    if settings.WHATSAPP_TOKEN and settings.WHATSAPP_WABA_ID and settings.WHATSAPP_PHONE_NUMBER_ID:
        return Creds(settings.WHATSAPP_TOKEN, settings.WHATSAPP_WABA_ID, settings.WHATSAPP_PHONE_NUMBER_ID)
    return None


async def creds_for_current(db: AsyncSession) -> Creds | None:
    """Request ki dukaan (tenant context) ke creds."""
    from app.models.tenant import Tenant
    from app.services import tenant_context

    tid = tenant_context.current_tenant_id.get() or tenant_context.cached_home_tenant_id()
    return creds_for(await db.get(Tenant, tid) if tid is not None else None)


def _need(creds: Creds | None) -> Creds:
    if creds is None:
        raise TemplateError("WhatsApp API is not connected for this shop")
    return creds


async def list_remote(creds: Creds | None) -> list[dict]:
    creds = _need(creds)
    status, data = await graph(
        "GET", f"{creds.waba_id}/message_templates", creds.token,
        params={"fields": "name,status,category,language,components,rejected_reason", "limit": 100},
    )
    if status != 200:
        raise TemplateError(str(data)[:300], 502)
    from app.services.templates import register_dynamic

    out = []
    for t in data.get("data", []):
        comps = t.get("components", [])
        body = next((c.get("text", "") for c in comps if c.get("type") == "BODY"), "")
        out.append({
            "name": t["name"], "status": t.get("status"), "category": t.get("category"),
            "language": t.get("language"), "body": body,
            "buttons": next((c.get("buttons", []) for c in comps if c.get("type") == "BUTTONS"), []),
            "rejected_reason": t.get("rejected_reason"),
        })
        # approved templates become sendable through the single door
        if t.get("status") == "APPROVED":
            register_dynamic(
                t["name"], t.get("language", "en_US"), len(set(re.findall(r"\{\{(\d+)\}\}", body)))
            )
    return out


async def create(
    creds: Creds | None, *, name: str, category: str, body: str, language: str = "en_US",
    footer: str | None = None, buttons: list[dict] | None = None, samples: list[str] | None = None,
) -> dict:
    creds = _need(creds)
    name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    samples = samples or []
    var_ids = sorted({int(n) for n in re.findall(r"\{\{(\d+)\}\}", body)})
    if var_ids != list(range(1, len(var_ids) + 1)):
        raise TemplateError("Variables must be {{1}}, {{2}}… in order, no gaps")
    if var_ids and len(samples) < len(var_ids):
        raise TemplateError(
            f"Provide a sample value for each of the {len(var_ids)} variables (Meta needs them for review)"
        )
    body_comp: dict = {"type": "BODY", "text": body}
    if var_ids:
        body_comp["example"] = {"body_text": [samples[: len(var_ids)]]}
    components: list[dict] = [body_comp]
    if footer:
        components.append({"type": "FOOTER", "text": footer})
    btns = []
    for b in buttons or []:
        kind, text = b["type"], b["text"]
        if kind == "QUICK_REPLY":
            btns.append({"type": kind, "text": text})
        elif kind == "URL":
            if not b.get("url"):
                raise TemplateError(f"Button '{text}' needs a URL")
            btns.append({"type": kind, "text": text, "url": b["url"]})
        else:
            if not b.get("phone_number"):
                raise TemplateError(f"Button '{text}' needs a phone number")
            btns.append({"type": kind, "text": text, "phone_number": b["phone_number"]})
    if btns:
        components.append({"type": "BUTTONS", "buttons": btns})

    status, data = await graph(
        "POST", f"{creds.waba_id}/message_templates", creds.token,
        json={"name": name, "language": language, "category": category, "components": components},
    )
    if status != 200:
        err = data.get("error", {})
        raise TemplateError(err.get("error_user_msg") or err.get("message") or str(data)[:250])
    return {"name": name, "status": data.get("status", "PENDING")}


async def delete(creds: Creds | None, name: str) -> None:
    creds = _need(creds)
    status, data = await graph(
        "DELETE", f"{creds.waba_id}/message_templates", creds.token, params={"name": name}
    )
    if status != 200:
        raise TemplateError(str(data)[:250])


async def standard_status(creds: Creds | None) -> dict:
    """App ke apne templates (STANDARD_SPECS) + is dukaan ke WABA par unka haal."""
    from app.services.templates import STANDARD_SPECS

    remote: dict[str, dict] = {}
    state = "not_connected"
    if creds is not None:
        try:
            remote = {t["name"]: t for t in await list_remote(creds)}
            state = "ok"
        except (TemplateError, httpx.HTTPError):
            state = "error"
    return {
        "state": state,
        "templates": [
            {
                "name": name, "purpose": spec["purpose"], "body": spec["body"],
                "buttons": [b["text"] for b in spec.get("buttons", [])],
                "status": (remote.get(name) or {}).get("status")
                or ("NOT_SUBMITTED" if state == "ok" else "UNKNOWN"),
                "rejected_reason": (remote.get(name) or {}).get("rejected_reason"),
            }
            for name, spec in STANDARD_SPECS.items()
        ],
    }


async def submit_standard(creds: Creds | None) -> list[dict]:
    """Jo standard template is dukaan ke WABA par nahi hai, sab approval ko bhejo."""
    from app.services.templates import STANDARD_SPECS

    existing = {t["name"] for t in await list_remote(creds)}
    results = []
    for name, spec in STANDARD_SPECS.items():
        if name in existing:
            results.append({"name": name, "status": "ALREADY_ON_META"})
            continue
        try:
            out = await create(
                creds, name=name, category="UTILITY", body=spec["body"],
                samples=spec["samples"], buttons=spec.get("buttons"),
            )
            results.append(out)
        except TemplateError as exc:
            results.append({"name": name, "status": "FAILED", "error": exc.detail})
    return results
