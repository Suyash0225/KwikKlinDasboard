"""Which shop is speaking — name and city of the current tenant.

AI prompts and fallbacks used to hardcode "Kwik Klin, Varanasi", so every
SaaS tenant's customers and campaigns were signed as Kwik Klin. Everything
customer-facing that names the shop should come from here.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class ShopIdentity:
    name: str
    city: str

    @property
    def signoff(self) -> str:
        return f"— {self.name} AI"

    @property
    def where(self) -> str:
        """' in Varanasi' or '' — reads naturally inside a sentence."""
        return f" in {self.city}" if self.city else ""


# tenant_id -> dukaan ka naam, jo current() ne pichhli baar DB se padha.
# get_message (sync) isi se {shop} bharta hai — warna har dukaan ke grahak
# ko settings.SHOP_NAME (ek hi platform naam) jaata tha.
_NAMES: dict = {}


def cached_name(tenant_id) -> str | None:
    return _NAMES.get(tenant_id) if tenant_id is not None else None


def _fallback() -> ShopIdentity:
    return ShopIdentity(name=(settings.SHOP_NAME or "").strip() or "Laundry", city="")


async def current(db: AsyncSession, tenant_id=None) -> ShopIdentity:
    """Identity of `tenant_id`, else the request's tenant, else the home shop."""
    from app.models.tenant import Tenant
    from app.services import tenant_context

    tid = tenant_id or tenant_context.effective_tenant_id()
    if tid is None:
        return _fallback()
    try:
        tenant = await db.get(Tenant, tid)
    except Exception:
        log.exception("shop_identity_lookup_failed")
        return _fallback()
    if tenant is None or not (tenant.shop_name or "").strip():
        return _fallback()
    _NAMES[tid] = tenant.shop_name.strip()
    return ShopIdentity(
        name=tenant.shop_name.strip(), city=(tenant.city or "").strip()
    )


def render(template: str, shop: ShopIdentity) -> str:
    """Fill <<SHOP>>, <<WHERE>> and <<SIGNOFF>> in a prompt or fixed reply.

    Plain replace, not str.format: prompts carry literal braces (JSON, {name}).
    """
    return (
        template.replace("<<SIGNOFF>>", shop.signoff)
        .replace("<<SHOP>>", shop.name)
        .replace("<<WHERE>>", shop.where)
    )
