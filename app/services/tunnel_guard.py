"""Tunnel self-healing: free trycloudflare tunnels die without warning,
silently cutting Meta's webhooks off. Every 10 minutes the scheduler asks
this guard to probe the public URL; if it's dead we spawn a fresh tunnel,
point Meta's webhook at it via the API and tell the owner — no human
intervention, downtime capped at ~10 minutes.
"""

import asyncio
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import structlog

from app.config import settings
from app.database import async_session_factory
from app.services import app_settings, audit

log = structlog.get_logger()

_URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
_LOG_FILE = Path(__file__).resolve().parent.parent / "media" / "tunnel.log"

# Windows process creation flags: survive our restarts, own process group.
_DETACHED = 0x00000008 | 0x00000200


async def check_and_heal() -> str:
    """'ok' | new public URL | 'failed' | 'no_cloudflared' | 'down_fixed'."""
    async with async_session_factory() as db:
        base = (await app_settings.get(db, "public_base_url") or "").rstrip("/")
        fixed = bool(await app_settings.get(db, "public_url_fixed"))
    if base and await _alive(base):
        return "ok"

    # Sthir URL (Tailscale Funnel / domain / VM): yahan cloudflare tunnel
    # banana sabse bada nuksan hoga — wo aapka URL badal dega aur Meta ka
    # webhook bhi apni taraf mod lega. Sirf batao, chhedo mat.
    if fixed:
        log.error("public_url_down_but_fixed", url=base or None)
        await _tell_owner_fixed_down(base)
        return "down_fixed"

    log.warning("tunnel_dead_healing", old=base or None)
    if not shutil.which("cloudflared"):
        log.error("cloudflared_not_installed")
        return "no_cloudflared"

    url = await asyncio.to_thread(_respawn)
    if not url:
        await audit.record(
            actor_role="system", actor="tunnel-guard", action="tunnel_heal",
            args={}, result="respawn failed", ok=False,
        )
        return "failed"

    async with async_session_factory() as db:
        await app_settings.set_value(db, "public_base_url", url)
    meta_ok = await _update_meta_webhook(url)
    await audit.record(
        actor_role="system", actor="tunnel-guard", action="tunnel_heal",
        args={"old": base or None}, result=f"{url} meta={'ok' if meta_ok else 'FAILED'}",
        ok=meta_ok,
    )
    await _tell_owner(url, meta_ok)
    log.info("tunnel_healed", url=url, meta_ok=meta_ok)
    return url


async def _alive(base: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(base + "/health")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _respawn() -> str | None:
    """Kill any old cloudflared, start a fresh one, read its URL. Blocking."""
    subprocess.run(
        ["taskkill", "/F", "/IM", "cloudflared.exe"],
        capture_output=True, check=False,
    )
    _LOG_FILE.parent.mkdir(exist_ok=True)
    _LOG_FILE.write_text("")
    with open(_LOG_FILE, "w") as out:
        subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"],
            stdout=subprocess.DEVNULL, stderr=out, creationflags=_DETACHED,
        )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        m = _URL_RE.search(_LOG_FILE.read_text(errors="replace"))
        if m:
            return m.group(0)
        time.sleep(1.5)
    return None


async def _update_meta_webhook(url: str) -> bool:
    """Point the Meta app's webhook at the new tunnel (needs app id+secret).

    A freshly-minted tunnel takes a few seconds to become reachable from
    Meta's side — wait for our own /health through it, then retry thrice.
    """
    if not settings.WHATSAPP_APP_ID:
        return False
    # don't ask Meta to verify until the tunnel actually routes
    for _ in range(10):
        if await _alive(url):
            break
        await asyncio.sleep(3)
    app_token = f"{settings.WHATSAPP_APP_ID}|{settings.WHATSAPP_APP_SECRET}"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_APP_ID}/subscriptions",
                    params={"access_token": app_token},
                    data={
                        "object": "whatsapp_business_account",
                        "callback_url": f"{url}/webhook",
                        "verify_token": settings.WHATSAPP_VERIFY_TOKEN,
                        "fields": "messages,message_template_status_update",
                    },
                )
            if r.status_code == 200 and r.json().get("success") is True:
                return True
            log.warning("meta_webhook_update_rejected", attempt=attempt, body=r.text[:150])
        except httpx.HTTPError:
            log.exception("meta_webhook_update_failed", )
        await asyncio.sleep(8)
    return False


async def _tell_owner_fixed_down(url: str) -> None:
    """Sthir URL neeche hai. Ghante mein ek baar batao — har 10 min nahi,
    warna ye khud ek spam ban jayega."""
    global _LAST_DOWN_ALERT

    now = time.monotonic()
    if now - _LAST_DOWN_ALERT < 3600:
        return
    _LAST_DOWN_ALERT = now
    try:
        from app.services.whatsapp import SendError, send_message

        async with async_session_factory() as db:
            await send_message(
                db, to_phone=settings.MANAGER_PHONE,
                text=(
                    f"⚠️ Public URL jawab nahi de raha:\n{url or '(set nahi)'}\n\n"
                    "Laptop/Tailscale chalu hai? Tab tak WhatsApp ke message "
                    "andar nahi aayenge. (Maine khud kuch nahi badla — URL "
                    "fixed mark kiya hua hai.)"
                ),
            )
    except SendError:
        log.info("fixed_down_notify_skipped")
    except Exception:
        log.exception("fixed_down_notify_failed")


_LAST_DOWN_ALERT = 0.0


async def _tell_owner(url: str, meta_ok: bool) -> None:
    try:
        from app.services.whatsapp import SendError, send_message

        note = (
            f"🔧 Tunnel gir gaya tha — maine khud theek kar diya.\nNaya URL: {url}\n"
            + ("Meta webhook bhi update ho gaya ✅" if meta_ok else "⚠️ Meta webhook update NAHI hua — 'check' bolein")
        )
        async with async_session_factory() as db:
            await send_message(db, to_phone=settings.MANAGER_PHONE, text=note)
    except SendError:
        log.info("tunnel_heal_notify_skipped")
    except Exception:
        log.exception("tunnel_heal_notify_failed")
