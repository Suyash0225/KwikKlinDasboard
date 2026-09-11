"""Public URL set karo (Tailscale Funnel / apna domain / VM ka URL).

Ye teen kaam ek saath karta hai:
1. `public_base_url` set karta hai — Google callback, Razorpay, media links
   sab isi se bante hain
2. `public_url_fixed = True` — taaki tunnel-guard cloudflare tunnel bana kar
   aapka sthir URL overwrite na kar de (yahi sabse badi galti hoti)
3. Meta ka webhook usi URL par mod deta hai

Run:
    .venv\\Scripts\\python.exe -m scripts.set_public_url https://laptop.tail1234.ts.net

Wapas cloudflare wale auto-tunnel par jaana ho to:
    .venv\\Scripts\\python.exe -m scripts.set_public_url --auto
"""

import asyncio
import sys

import httpx
import structlog

# Windows ka console cp1252 hai — emoji print karte hi script apne hi
# output par crash ho jaati hai. UTF-8 par majboor karo.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.database import async_session_factory, engine
from app.services import app_settings
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def _alive(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url.rstrip("/") + "/health")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def main(url: str, auto: bool) -> None:
    # App ke bahar chalti hai: home-tenant cache khud bharo, warna settings
    # NULL tenant par save hoti hain aur chalte app ko dikhti hi nahi.
    from app.services import tenant_context

    await tenant_context.get_home_tenant_id()

    async with async_session_factory() as db:
        if auto:
            await app_settings.set_value(db, "public_url_fixed", False)
            print("\n  Auto-tunnel mode ON — tunnel-guard phir se cloudflare "
                  "tunnel banayega.\n")
            await engine.dispose()
            return

        url = url.rstrip("/")
        if not url.startswith("https://"):
            raise SystemExit("URL https:// se shuru hona chahiye")

        print(f"  Check kar raha hoon: {url}/health ...")
        ok = await _alive(url)
        if not ok:
            print("  ⚠️  Ye URL abhi jawab nahi de raha.")
            print("     (Funnel chalu hai? App 8000 par chal rahi hai?)")
            if input("     Phir bhi set karun? [y/N]: ").strip().lower() != "y":
                await engine.dispose()
                return
        else:
            print("  ✅ URL zinda hai.")

        await app_settings.set_value(db, "public_base_url", url)
        await app_settings.set_value(db, "public_url_fixed", True)
        log.info("public_url_pinned", url=url)

    from app.services.tunnel_guard import _update_meta_webhook

    print("  Meta ka webhook update kar raha hoon ...")
    meta_ok = await _update_meta_webhook(url)
    print("  Meta webhook:", "✅ ho gaya" if meta_ok else "⚠️ nahi hua (App ID/secret check karein)")

    print(f"""
  Public URL   : {url}
  Fixed        : haan (tunnel-guard ab ise nahi chhedega)

  Ab ye teen jagah bhi wahi URL daal dijiye:
    Google  -> Authorized redirect URI : {url}/api/auth/google/callback
    Razorpay-> Webhook URL             : {url}/webhooks/razorpay
    Meta    -> {'apne aap ho gaya' if meta_ok else 'Callback URL: ' + url + '/webhook'}
""")
    await engine.dispose()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "--auto":
        asyncio.run(main("", auto=True))
    else:
        asyncio.run(main(args[0], auto=False))
