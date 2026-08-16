"""Fire-and-forget kaam: jawab pehle, khabar baad mein.

Kuch cheezein request ke andar honi hi nahi chahiye. Photo upload iska
sabse saaf udaharan tha: staff phone se photo bhejta tha, server photo
sambhal leta tha, aur phir **WhatsApp ka jawab aane tak** ruka rehta tha
taaki owner ko khabar bhej sake. Us poore waqt delivery wale ka phone
"atka" dikhta tha — jabki uska kaam kab ka ho chuka tha.

Ab aisa kaam yahan se chalta hai: apna DB session, apna tenant, aur apni
galti apne paas. Background ka fail hona kabhi us kaam ko nahi giraata
jiske baad wo chala tha.

Do baatein jaan-boojh kar aisi hain:

1. **Strong reference.** asyncio apne task ki sirf WEAK reference rakhta
   hai, isliye bare `create_task()` beech kaam mein garbage-collect ho
   sakta hai — bina kisi error ke. Isliye har task set mein rakha jata hai
   aur khatam hone par hi nikalta hai.
2. **Tenant saath jaata hai.** Background task apna context banate waqt
   copy karta hai, par hum saaf-saaf dobara set karte hain — warna kisi
   doosri dukaan ka context leak hone par khabar galat jagah chali jaye.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services import tenant_context

log = structlog.get_logger()

# Chal rahe kaam. Sirf isliye taaki GC inhe utha na le — dekho docstring.
_running: set[asyncio.Task] = set()


def spawn(coro: Awaitable[Any], *, label: str = "") -> asyncio.Task:
    """Coroutine ko background mein chalao aur uski reference pakde raho."""
    task = asyncio.ensure_future(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)
    if label:
        task.set_name(f"kk:{label}")
    return task


async def _with_session(
    fn: Callable[[AsyncSession], Awaitable[Any]], tenant_id: UUID | None, label: str
) -> None:
    token = tenant_context.current_tenant_id.set(tenant_id) if tenant_id else None
    try:
        async with async_session_factory() as db:
            await fn(db)
    except Exception:
        # Background ka girna kabhi upar wale kaam ko nahi girata — wo to
        # kab ka jawab de chuka. Par chup-chaap bhi nahi: log rahega.
        log.exception("background_job_failed", job=label or "unnamed")
    finally:
        if token is not None:
            tenant_context.current_tenant_id.reset(token)


def run_after(
    fn: Callable[[AsyncSession], Awaitable[Any]],
    *,
    tenant_id: UUID | None = None,
    label: str = "",
) -> asyncio.Task:
    """Jawab bhejne ke baad ye kaam karo — apne session aur apne tenant par.

    fn ko ek taaza AsyncSession milta hai. Request wala session MAT bhejo:
    response ke saath wo band ho jaata hai, aur uspar likhna crash hai.
    """
    return spawn(_with_session(fn, tenant_id, label), label=label)


async def drain(timeout: float = 5.0) -> int:
    """Shutdown par chal rahe kaam ko thoda waqt do. Kitne bache, wo lautao.

    Bina iske server band hote waqt aadhi bheji hui khabar beech mein kat
    jaati — owner ko kabhi pata hi na chalta ki photo lagi thi.
    """
    if not _running:
        return 0
    pending = list(_running)
    done, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        log.warning("background_jobs_unfinished", count=len(still))
    return len(still)
