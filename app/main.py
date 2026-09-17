"""FastAPI application entry point.

Run locally:
    .venv\\Scripts\\uvicorn.exe app.main:app --reload
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.routers.admin import router as admin_router
from app.routers.agent_admin import router as agent_admin_router
from app.routers.orders import router as orders_router
from app.routers.webhook import router as webhook_router
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def _multi_tenant_hardening_check() -> list[str]:
    """60 dukaanon se pehle jo pakka hona chahiye — nahi hai to ERROR log.
    Kamiyan lautata hai; production mein lifespan inke saath shuru hi nahi hota."""
    from sqlalchemy import func, select
    from sqlalchemy import text as sqltext

    from app.database import async_session_factory
    from app.models.tenant import WRITABLE_STATUSES, Tenant

    async with async_session_factory() as db:
        n = (
            await db.execute(
                select(func.count()).select_from(Tenant).where(Tenant.status.in_(WRITABLE_STATUSES))
            )
        ).scalar_one()
    gaps: list[str] = []

    # RLS SACH MEIN chal rahi hai ya nahi — schema se nahi, ASAL BEHAVIOUR se.
    #
    # Tables par ENABLE + FORCE ROW LEVEL SECURITY laga hai aur schema dekh
    # kar sab theek lagta hai. Par superuser RLS ko poori tarah nazarandaz
    # karta hai, aur FORCE uspar laagu hi nahi hota — FORCE sirf TABLE OWNER
    # ke liye hai. docker-compose ka POSTGRES_USER Postgres ka bootstrap
    # superuser hota hai, isliye default setup mein defence-in-depth ki
    # teesri parat maujood hi nahi hoti, aur isolation akele ORM filter par
    # tik jaati hai. Ek bhi raw query us filter ke bahar ho to leak chup-
    # chaap hoga.
    #
    # Isliye jaanch schema ki nahi, role ki hai — wahi cheez jo jhooth nahi
    # bol sakti.
    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one_or_none()
    if row:
        gaps.append(
            "DB role bypasses RLS (superuser/BYPASSRLS) — tenant isolation "
            "rests on the ORM filter alone; use a NOSUPERUSER app role"
        )

    if not settings.TOKEN_ENCRYPTION_KEY:
        gaps.append("TOKEN_ENCRYPTION_KEY empty — WhatsApp/Instagram/Google tokens stored in plaintext")
    if not settings.VENDOR_API_KEY:
        gaps.append("VENDOR_API_KEY empty — shop ADMIN_API_KEY doubles as vendor master key")
    elif settings.VENDOR_API_KEY == settings.ADMIN_API_KEY:
        gaps.append("VENDOR_API_KEY equals ADMIN_API_KEY — one leak opens both")
    if settings.ADMIN_API_KEY.startswith("change-me"):
        gaps.append("ADMIN_API_KEY is the .env.example placeholder")
    if gaps:
        log.error("multi_tenant_hardening_incomplete", active_tenants=n, gaps=gaps,
                  fix="python -m scripts.secure_setup")
    else:
        log.info("multi_tenant_hardening_ok", active_tenants=n)
    return gaps


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup", shop=settings.SHOP_NAME, environment=settings.ENVIRONMENT)
    from app.services import scheduler, tenant_context

    # Prime the home-tenant cache before any traffic: the sync DB events
    # (insert stamping) can only read the cache, never resolve it themselves.
    try:
        await tenant_context.get_home_tenant_id()
    except Exception:
        log.exception("home_tenant_prime_failed")

    # Security check: token encryption key, alag vendor key, NOSUPERUSER DB
    # role. Local par sirf ERROR log. PRODUCTION mein kami ho to app shuru hi
    # nahi hoti — plaintext tokens ya RLS-bypass ke saath chalna chup-chaap
    # khatra hai. Theek karne ke liye: python -m scripts.secure_setup
    gaps: list[str] = []
    try:
        gaps = await _multi_tenant_hardening_check()
    except Exception:
        log.exception("hardening_check_failed")
    if gaps and settings.ENVIRONMENT == "production":
        raise RuntimeError(
            "Refusing to start in production: " + "; ".join(gaps)
            + " — run: python -m scripts.secure_setup"
        )

    scheduler.start()
    # owner's edited message formats survive restarts
    try:
        from app.database import async_session_factory
        from app.services import app_settings as _as
        from app.services.messages import load_overrides

        async with async_session_factory() as db:
            load_overrides(await _as.get(db, "message_overrides"))
    except Exception:
        log.exception("message_overrides_load_failed")
    yield
    scheduler.shutdown()
    await engine.dispose()
    log.info("shutdown")


app = FastAPI(
    title="Laundry WhatsApp Bot",
    version="0.1.0",
    lifespan=lifespan,
    # No public API docs in production — this service faces the internet
    # (Meta webhooks) and its API surface is nobody else's business.
    docs_url="/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
)

from fastapi import Request
from fastapi.middleware.gzip import GZipMiddleware

# Big JSON payloads (inbox threads, reports) shrink ~10x over the tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class _noop:
    """Tenant na mile (staff panel par bina cookie) to context waisa hi —
    None, yaani system; RLS kuch nahi dikhata, jo sahi hai."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@app.middleware("http")
async def tenant_scope(request: Request, call_next):
    """Data isolation, layer 1: har HTTP request par tenant context set karo.

    - Valid kk_session cookie -> us user ka tenant.
    - Warna (anonymous, API key, webhook) -> is instance ka HOME tenant.

    Aage kya hota hai: app/database.py isi context se har transaction par
    Postgres ko `SET LOCAL app.tenant_id` bhejta hai (RLS enforce karta hai),
    har ORM SELECT par tenant filter lagta hai, aur har naya row isi tenant
    par stamp hota hai. HTTP se aane wali koi query kabhi unscoped nahi
    chal sakti — chahe endpoint code filter karna bhool bhi jaye.
    """
    from app.services import tenant_context

    path = request.url.path
    if path.startswith("/admin/static/") or path == "/health":
        return await call_next(request)  # no tenant data behind these

    # Platform surfaces — kisi ek dukaan ke nahi, sabki: vendor control
    # panel (key-gated) aur Razorpay ka webhook (jis dukaan ka paisa aaya,
    # wo notes se nikalti hai). Ye SYSTEM context mein chalte hain, home
    # mein nahi — warna invoices/billing_events par RLS lagte hi control
    # panel ko sirf home dikhta aur doosri dukaan ka invoice insert hi na
    # hota (WITH CHECK). Rate limit ke liye home ki id hi key rehti hai.
    if path.startswith("/control") or path == "/webhooks/razorpay":
        home = await tenant_context.get_home_tenant_id()
        if home is not None and _rate_limited(home, path):
            return JSONResponse(
                status_code=429,
                content={"detail": "Bahut tezi se requests — thoda ruk kar try karein."},
                headers={"Retry-After": "30"},
            )
        async with _noop():
            return await call_next(request)

    tid = None
    # Staff panel ka apna rasta: tenant us aadmi ke TOKEN se aata hai.
    # Login se pehle (token nahi hai) context system rehta hai — us waqt
    # hum jaante hi nahi ki kis dukaan ka aadmi hai, aur home tenant maan
    # lena galat hoga: doosri dukaan ka staff kabhi login hi na kar paata.
    if path.startswith("/staff"):
        staff_token = request.cookies.get("kk_staff", "")
        tid = (
            await tenant_context.tenant_id_for_staff_token(staff_token)
            if staff_token
            else None
        )
        async with tenant_context.as_tenant(tid) if tid is not None else _noop():
            return await call_next(request)

    token = request.cookies.get("kk_session", "")
    if token:
        tid = await tenant_context.tenant_id_for_session(token)
    if tid is None:
        tid = await tenant_context.get_home_tenant_id()

    # Per-tenant rate limit: ek client ka runaway loop baaki sab tenants ko
    # slow nahi kar sakta. Sirf API paths par; webhook (Meta ka traffic,
    # burst aata hai) aur static exempt hain.
    if tid is not None and _rate_limited(tid, request.url.path):
        return JSONResponse(
            status_code=429,
            content={"detail": "Bahut tezi se requests — thoda ruk kar try karein."},
            headers={"Retry-After": "30"},
        )

    # Owner ka number bhi context mein — "malik ko batao" wali har jagah
    # isi tenant ke malik ko bole, .env wale ko nahi.
    async with tenant_context.as_tenant(tid) if tid is not None else _noop():
        return await call_next(request)


# Sliding window per tenant (in-memory, per-PROCESS — restart par reset,
# aur N workers matlab asli limit N x RATE_LIMIT_PER_MIN. Ye jaan-boojh kar
# simple hai: ye limiter ek runaway client ko rokne ke liye hai, DDoS ke
# liye nahi — uske liye upar CDN/proxy hai.)
from collections import deque as _deque
import time as _time

# BUG THA: bucket `deque(maxlen=4096)` tha aur default limit 6000. len(dq)
# 4096 se upar ja hi nahi sakta, isliye `len(dq) >= limit` default config par
# KABHI sach nahi hota tha — yaani rate limiting chupchaap band padi thi.
# Test suite ise nahi pakad payi kyunki wo limit 1/5 par set karke chalti hai.
# Ab cap limit se hi nikalta hai.
_RL_BUCKETS: dict = {}
_RL_PREFIXES = ("/admin/api", "/admin/media", "/orders", "/api/", "/control", "/staff/api")


def _rate_limited(tid, path: str) -> bool:
    if not path.startswith(_RL_PREFIXES):
        return False
    limit = settings.RATE_LIMIT_PER_MIN
    if limit <= 0:
        return False  # 0/negative = disabled
    now = _time.monotonic()
    dq = _RL_BUCKETS.get(tid)
    if dq is None or dq.maxlen != limit + 1:
        # Pehli baar, ya limit runtime par badal di gayi (tests karte hain).
        # maxlen = limit + 1: window bharne ke liye kaafi, aur memory bandhi
        # rehti hai chahe tenant kitna bhi maare.
        dq = _RL_BUCKETS[tid] = _deque(dq or (), maxlen=limit + 1)
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        log.warning("tenant_rate_limited", tenant=str(tid), path=path)
        return True
    dq.append(now)
    return False


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response.

    Referrer-Policy matters most here: media URLs carry ?key=..., and this
    stops that key from leaking via the Referer header.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


app.include_router(webhook_router)
app.include_router(orders_router)
app.include_router(admin_router)
app.include_router(agent_admin_router)

# Bechne ka rasta: pricing -> signup -> payment -> login (app/routers/account.py)
from app.routers.account import router as account_router

app.include_router(account_router)

# Suyash ka apna panel: sab clients, plans, paisa (app/routers/control.py)
from app.routers.control import router as control_router

app.include_router(control_router)

# Dukaan ke aadmi ka apna panel — owner ke dashboard se bilkul alag rasta,
# alag cookie, alag pehre (app/routers/staff_panel.py)
from app.routers.staff_panel import router as staff_panel_router

app.include_router(staff_panel_router)

# Google Business Profile: saare Google reviews website par (Settings se connect)
from app.routers.google_business import router as google_business_router

app.include_router(google_business_router)

# Static assets for the dashboard (CSS/JS — no secrets, safe to serve openly)
from pathlib import Path

from fastapi.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """StaticFiles + real cache headers.

    CSS/JS are referenced with ?v=<mtime> (the dashboard route rewrites the
    version on every deploy), so the browser may cache them forever — a
    repeat visit re-downloads nothing. HTML must always revalidate, or a
    stale shell would point at assets that no longer exist.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(args[0] if args else kwargs.get("full_path", ""))
        if path.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount(
    "/admin/static",
    CachedStaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)


# PWA service worker. Served from /admin/sw.js (not /admin/static/...) with a
# Service-Worker-Allowed header so its scope can cover the whole /admin app.
# no-cache: a deployed SW update must be picked up on the next visit.
@app.get("/admin/sw.js", include_in_schema=False)
async def service_worker():
    from fastapi.responses import Response as _Resp

    sw = Path(__file__).resolve().parent / "static" / "sw.js"
    return _Resp(
        content=sw.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/admin"},
    )


# Public page: pricing + signup + login. Yahi wo darwaza hai jahan se ek
# nayi laundry andar aati hai (app/static/join.html).
_JOIN_FILE = Path(__file__).resolve().parent / "static" / "join.html"


_WELCOME_FILE = Path(__file__).resolve().parent / "static" / "welcome.html"


@app.get("/welcome", include_in_schema=False)
async def welcome_page():
    """Naye client ki landing. Yahan is dukaan ka koi data nahi hai — sirf
    unke apne account ki halat aur aage ka rasta."""
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_WELCOME_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/r/{slug}", include_in_schema=False)
async def review_redirect(slug: str):
    """Choti review link: /r/kwik-klin -> dukaan ka Google "write a review".

    Message mein Google ki lambi link (placeid=ChIJ...) badsurat lagti hai
    aur WhatsApp par aadhi kat jaati hai. Ye dukaan ke apne domain par
    chhoti rehti hai, bahar ki kisi shortener service par nahi — na expire,
    na kisi aur ka branding, aur har click hamare log mein.
    Do listing hon to baari-baari dono par bhejte hain.
    """
    import random

    from fastapi.responses import RedirectResponse
    from fastapi.responses import Response as _Resp
    from sqlalchemy import select as _select

    from app.database import async_session_factory
    from app.models.tenant import Tenant
    from app.services import customer_messages, tenant_context

    async with async_session_factory() as db:
        tid = (
            await db.execute(_select(Tenant.id).where(Tenant.slug == slug[:80].lower()))
        ).scalar_one_or_none()
    links = []
    if tid is not None:
        # Naya session CONTEXT KE ANDAR: RLS ka app.tenant_id transaction shuru
        # hote hi lagta hai — purane session mein context badalne se DB ki
        # pehchaan nahi badalti aur dukaan ki settings chhupi reh jaati.
        async with tenant_context.as_tenant(tid):
            async with async_session_factory() as db:
                links = await customer_messages.review_links(db)
    if not links:
        return _Resp(
            content="<!doctype html><meta charset=utf-8><title>Link not found</title>"
                    "<p style='font:16px system-ui;padding:2rem'>This review link is not set up.</p>",
            media_type="text/html", status_code=404,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )
    target = random.choice(links)
    log.info("review_link_clicked", tenant=slug)
    # 302 (307 nahi): GET hi rahe; no-store taaki link badle to turant lage
    return RedirectResponse(target, status_code=302,
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/pay/{token}", include_in_schema=False)
async def pay_page(token: str):
    """Grahak ka ek-tap UPI page. Bina login ke — grahak hamara user nahi hai.

    Kyun ye page beech mein hai: WhatsApp sirf http/https ko tap-able banata
    hai. `upi://` seedha message mein daalo to wo plain text hi rehta hai —
    lamba, badsurat, aur phir bhi tap nahi hota. Isliye message mein https
    jaata hai aur upi:// yahan se fire hota hai.

    Page par teen cheezein, teenon zaroori:
      - auto-redirect (Android par yahi asli rasta hai)
      - ek bada button, agar auto na chale ya desktop par khule
      - VPA text mein, taaki UPI app na khule to bhi paisa bheja ja sake
        (DuckDNS free hai, aur iOS par deep link ka bharosa kam hai)

    Token mein sirf dukaan aur amount hai. Link forward ho sakta hai, isliye
    is page par grahak ka naam, phone ya order number kabhi nahi.
    """
    from fastapi.responses import Response as _Resp

    from app.database import async_session_factory
    from app.models.tenant import Tenant
    from app.services import app_settings, pay_link, tenant_context

    def _fail() -> _Resp:
        # Galat, chhera hua, expire — sab ek jaisa 404. Farq batana sirf
        # chhedne wale ke kaam aata hai.
        return _Resp(
            content="<!doctype html><meta charset=utf-8><title>Link not valid</title>"
                    "<p style='font:16px system-ui;padding:2rem'>This payment link is "
                    "no longer valid. Please ask the shop for a new one.</p>",
            media_type="text/html", status_code=404,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    parsed = pay_link.parse(token)
    if parsed is None:
        return _fail()
    tenant_id, amount = parsed

    async with async_session_factory() as db:
        async with tenant_context.as_tenant(tenant_id):
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None:
                return _fail()
            vpa = (await app_settings.get(db, "upi_vpa") or "").strip()
            payee = (await app_settings.get(db, "upi_payee") or "").strip()

    # VPA hataye jaane ke baad purane link zinda reh jaate hain. Bina VPA ke
    # page par bhejna matlab grahak ko khali screen — isse 404 behtar hai.
    if not vpa:
        return _fail()

    shop = tenant.shop_name or payee or "Shop"
    uri = pay_link.upi_uri(vpa, payee or shop, amount)
    amt = f"{amount:.2f}"

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Pay {_esc(shop)}</title>
<style>
 body{{font:16px system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
   min-height:100vh;display:grid;place-items:center;background:#faf7f4;color:#1c1917}}
 .card{{background:#fff;padding:2rem 1.5rem;border-radius:16px;text-align:center;
   max-width:22rem;width:calc(100% - 2rem);box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .amt{{font-size:2.5rem;font-weight:700;margin:.25rem 0 1.5rem}}
 .btn{{display:block;background:#ea580c;color:#fff;text-decoration:none;
   padding:.9rem;border-radius:10px;font-weight:600;font-size:1.05rem}}
 .vpa{{margin-top:1.5rem;font-size:.9rem;color:#57534e;word-break:break-all}}
 code{{background:#f5f5f4;padding:.2rem .4rem;border-radius:4px}}
</style></head><body>
<div class="card">
  <div>Pay {_esc(shop)}</div>
  <div class="amt">&#8377;{amt}</div>
  <a class="btn" href="{_esc(uri)}">Pay with UPI app</a>
  <div class="vpa">Or send to <code>{_esc(vpa)}</code></div>
</div>
<script>
 /* Android: tap se seedha app chooser. Kuch browsers pehle render ke bina
    navigate nahi karte, isliye chhoti der. Na chale to button hai hi. */
 setTimeout(function(){{ location.href = {_json(uri)}; }}, 350);
</script>
</body></html>"""

    return _Resp(
        content=html, media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


_STATUS_WORD = {
    "RECEIVED": ("Received", "wait"), "PICKUP_ASSIGNED": ("Pickup scheduled", "wait"),
    "PICKED_UP": ("Picked up", "wait"), "IN_WASH": ("Washing", "wait"), "IN_DRY": ("Drying", "wait"),
    "IN_IRON": ("Ironing", "wait"), "READY": ("Ready", "ok"), "OUT_FOR_DELIVERY": ("Out for delivery", "ok"),
    "DELIVERED": ("Delivered", "ok"), "CANCELLED": ("Cancelled", ""), "ON_HOLD": ("On hold", "wait"),
}


@app.get("/b/{token}", include_in_schema=False)
async def bill_page(token: str):
    """Grahak ka bill — web page + UPI (GPay/PhonePe/Paytm) se payment.

    Token signed hai (services/bill_link.py). Amount LIVE: payment ke baad
    wahi link "Paid" dikhata hai. Link forward ho sakta hai — isliye phone
    masked, noindex, aur koi galti ho to sab ek jaisa 404.
    """
    from decimal import Decimal

    from fastapi.responses import Response as _Resp

    from app.models import Customer, Order
    from app.models.tenant import Tenant
    from app.services import app_settings, bill_link, integrations, receipt

    def _fail() -> _Resp:
        return _Resp(
            content="<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>Bill not found</title><p style='font:16px system-ui;padding:2rem'>"
                    "This bill link is not valid. Please ask the shop to send it again.</p>",
            media_type="text/html", status_code=404,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    parsed = bill_link.parse(token)
    if parsed is None:
        return _fail()
    tid, oid = parsed
    # Context pehle, session baad — RLS us dukaan par transaction ke shuru mein lagti hai
    async with integrations.tenant_db(tid) as db:
        order = await db.get(Order, oid)
        if order is None or order.tenant_id != tid:
            return _fail()
        tenant = await db.get(Tenant, tid)
        cust = await db.get(Customer, order.customer_id)
        s = await app_settings.all_settings(db)

    e = _esc
    shop = (tenant.shop_name if tenant else "") or "Laundry"
    r = receipt.build(order=order, customer_name=cust.name if cust else None,
                      customer_phone=None, shop_name=shop, settings=s)
    money = lambda v: f"₹{v:,.0f}" if float(v).is_integer() else f"₹{v:,.2f}"  # noqa: E731
    phone = "".join(ch for ch in (cust.phone if cust else "") if ch.isdigit())
    customer = e((cust.name or "").strip() if cust and cust.name else "Customer")
    if phone:
        customer += f" <span class='label'>· ••••{e(phone[-4:])}</span>"

    meta = [x for x in (r["address"], f"Ph: {r['phone']}" if r["phone"] else "", f"GSTIN: {r['gstin']}" if r["gstin"] else "") if x]
    shop_meta = "".join(f"<p>{e(x)}</p>" for x in meta)

    items = []
    partial = bool(r.get("partial"))
    for it in r["items"]:
        unit = " kg" if it["kg"] else ""
        detail = f"{it['qty']}{unit}" + (f" × {money(it['rate'])}" if it["rate"] is not None else "")
        pieces = ", ".join(f"{n} {q}" for n, q in it["pieces"])
        dl = ""
        if partial and it.get("count"):
            if it["pending"] == 0:
                dl = "<span class='dl ok'>✓ delivered</span>"
            elif it["delivered"] == 0:
                dl = "<span class='dl wait'>with us</span>"
            else:
                dl = f"<span class='dl part'>{it['delivered']} of {it['count']} delivered · {it['pending']} pending</span>"
        items.append(
            f"<li><span><span class='name'>{e(it['title'])}</span><small>{e(detail)}"
            f"{' · ' + e(pieces) if pieces else ''}</small>{dl}</span>"
            f"<span class='amt'>{money(it['amount']) if it['amount'] is not None else ''}</span></li>"
        )
    clothes = r.get("clothes") or {}
    delivery_card = ""
    if partial:
        still = "".join(f"<li><span>{e(n)}</span><b>× {k}</b></li>" for n, k in r["still"])
        delivery_card = (
            "<section class='card'><h2>Delivery</h2>"
            f"<div class='row'><span class='label'>Delivered</span><b class='status ok'>{clothes['delivered']} of {clothes['total']} clothes</b></div>"
            f"<div class='row'><span class='label'>Still with us</span><b class='status wait'>{clothes['pending']}</b></div>"
            f"<ul class='still'>{still}</ul>"
            "<p class='note'>We'll deliver the rest as soon as it's ready.</p></section>"
        )
    if r["urgent_charge"]:
        items.append(f"<li><span class='name'>Urgent charge</span><span class='amt'>{money(r['urgent_charge'])}</span></li>")

    rows = []
    if r["has_total"]:
        if r["discount"] or r["gst"] or r["urgent_charge"]:
            rows.append(("Subtotal", money(r["subtotal"])))
            if r["discount"]:
                rows.append(("Discount", "−" + money(r["discount"])))
            if r["gst"]:
                rows.append(("GST", money(r["gst"])))
        rows.append(("<b class='grand'>Total</b>", f"<b class='grand'>{money(r['total'])}</b>"))
        rows.append(("Paid", money(r["paid"])))
        rows.append(("<b>Balance due</b>", f"<b>{money(r['due']) if r['due'] > 0 else 'Paid in full ✅'}</b>"))
    totals = "".join(
        f"<div class='row{' due-row' if a.startswith('<b>Balance') else ''}'><span>{a}</span><span>{b}</span></div>"
        for a, b in rows
    )

    status_word, status_cls = _STATUS_WORD.get(order.status.name, (order.status.name.title(), ""))
    due = Decimal(str(r["due"])).quantize(Decimal("0.01"))
    pay_block = ""
    if r["has_total"] and due <= 0 and r["total"] > 0:
        pay_block = ""   # hero khud "Paid in full" bolta hai
    elif r["has_total"] and due > 0:
        pay_block = _upi_pay_block(r["upi"], r["upi_payee"], shop, due, f"Bill {order.order_number}")

    terms = ""
    if r["terms"]:
        terms = ("<section class='card'><details class='terms'><summary>Terms &amp; conditions</summary><ol>"
                 + "".join(f"<li>{e(t)}</li>" for t in r["terms"]) + "</ol></details></section>")
    digits = "".join(ch for ch in r["phone"] if ch.isdigit())
    contact_btn = ""
    if digits:
        wa = digits if digits.startswith("91") or len(digits) != 10 else "91" + digits
        contact_btn = f"<a class='contact-btn' href='https://wa.me/{wa}'>💬 WhatsApp the shop</a>"

    # ---- mood: paid (sparkle) / due / happy (delivered on time) / info ----
    st = order.status.name
    first_name = ((cust.name or "").strip().split() or ["there"])[0] if cust else "there"
    paid_full = r["has_total"] and due <= 0 and r["total"] > 0
    on_time = (st == "DELIVERED" and order.actual_delivery is not None
               and (order.expected_delivery is None or order.actual_delivery.date() <= order.expected_delivery))
    if paid_full and st == "DELIVERED":
        mood, emoji, title = "paid", "🎉", "All done, thank you!"
        sub = f"{first_name}, your clothes are delivered and the bill is settled."
    elif paid_full:
        mood, emoji, title = "paid", "✨", "Payment received!"
        sub = f"Thank you, {first_name}. We'll update you as your order moves."
    elif st == "DELIVERED":
        mood, emoji, title = "happy", "😊", "Delivered!"
        sub = "Hope the clothes came back just the way you like them."
    elif partial:
        mood, emoji, title = "happy", "🧺", "Partly delivered"
        sub = (f"{clothes['delivered']} of {clothes['total']} clothes delivered · {clothes['pending']} still with us"
               + (f" · {money(float(due))} due" if due > 0 else ""))
    elif st in ("READY", "OUT_FOR_DELIVERY"):
        mood, emoji, title = ("due" if due > 0 else "happy"), "🧺", ("Your clothes are ready" if st == "READY" else "On the way to you")
        sub = f"Balance of {money(float(due))} — pay now or at delivery." if due > 0 else "Fresh, folded and coming home."
    elif st == "CANCELLED":
        mood, emoji, title, sub = "info", "🚫", "Order cancelled", "Message us if this looks wrong."
    elif due > 0:
        mood, emoji, title = "due", "🧾", f"Hi {first_name}, here's your bill"
        sub = f"{money(float(due))} due · pay online in one tap."
    else:
        mood, emoji, title = "info", "🧺", f"Hi {first_name}, here's your order"
        sub = "We'll share the bill once the clothes reach us."
    badge = ""
    if on_time:
        badge = "<span class='hero-badge'>⏱️ Delivered on time</span>"
    elif partial and st != "DELIVERED":
        badge = f"<span class='hero-badge'>🧺 {clothes['pending']} more coming soon</span>"
    elif order.priority == "urgent" and st not in ("DELIVERED", "CANCELLED"):
        badge = "<span class='hero-badge'>⚡ Urgent service</span>"
    art = ""
    if mood in ("paid", "happy"):
        import random as _rnd

        rnd = _rnd.Random(order.order_number)
        art = "".join(f"<i style='left:{rnd.randint(2, 98)}%;animation-delay:{rnd.uniform(0, 3):.1f}s'></i>" for _ in range(22))
        art += "".join(f"<i class='spark' style='left:{rnd.randint(4, 94)}%;top:{rnd.randint(8, 80)}%;animation-delay:{rnd.uniform(0, 1.8):.1f}s'>✦</i>" for _ in range(8))
    theme = {"paid": "#059669", "due": "#f97316", "happy": "#0ea5e9"}.get(mood, "#1f2937")

    # ---- progress: Booked -> Picked up -> Cleaning -> Ready -> Delivered ----
    stage = {"RECEIVED": 0, "PICKUP_ASSIGNED": 0, "PICKED_UP": 1, "IN_WASH": 2, "IN_DRY": 2, "IN_IRON": 2,
             "READY": 3, "OUT_FOR_DELIVERY": 3, "DELIVERED": 4}.get(st, -1)
    steps = ["Booked", "Picked up", "Cleaning", "Ready", "Delivered"]
    if st == "RECEIVED":
        steps[1] = "At shop"
    progress = "".join(
        f"<li class='{'done' if i < stage or (i == stage == 4) else 'now' if i == stage else ''}'>{e(s)}</li>"
        for i, s in enumerate(steps)
    )
    if stage >= 0:
        progress += f"<span class='bar' style='width:{stage * 20}%'></span>"

    fills = {
        "{{SHOP_META}}": shop_meta, "{{NUMBER}}": e(order.order_number), "{{DATE}}": e(r["date"]),
        "{{CUSTOMER}}": customer,
        "{{DELIVERY_ROW}}": (f"<div class='row'><span class='label'>Delivery</span><span>{e(r['delivery'])}</span></div>"
                             if r["delivery"] else ""),
        "{{STATUS_CLASS}}": status_cls, "{{STATUS}}": e(status_word),
        "{{PAY_BLOCK}}": pay_block + delivery_card, "{{ITEMS}}": "".join(items), "{{TOTALS}}": totals,
        "{{TERMS}}": terms, "{{CONTACT_BTN}}": contact_btn,
        "{{MOOD}}": mood, "{{THEME}}": theme, "{{HERO_ART}}": art, "{{HERO_EMOJI}}": emoji,
        "{{HERO_TITLE}}": e(title), "{{HERO_SUB}}": e(sub), "{{HERO_BADGE}}": badge, "{{PROGRESS}}": progress,
        "{{ASSET_V}}": _SITE_ASSET_V, "{{SHOP}}": e(shop),
    }
    fills["{{LIVE_URL}}"] = f"/b/{e(token)}/live"
    fills["{{LIVE_V}}"] = e(_bill_live_version(order))
    html = (_SITE_DIR / "templates" / "bill.html").read_text(encoding="utf-8")
    for k, v in fills.items():
        html = html.replace(k, v)
    return _Resp(content=html, media_type="text/html",
                 headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _bill_live_version(order) -> str:
    """Jo badle to grahak ka khula hua bill page khud refresh ho: status, paisa."""
    return f"{order.status.name}|{order.amount_paid or 0}|{order.total_amount or 0}"


@app.get("/b/{token}/live", include_in_schema=False)
async def bill_live(token: str):
    """Bill page har kuch second yahan poochta hai — status/payment badla?

    Sirf ek chhota sa version string, koi rakam/naam nahi: page reload karke
    poora bill le leta hai. Galat token par wahi 404 jo page par."""
    from fastapi.responses import JSONResponse

    from app.models import Order
    from app.services import bill_link, integrations

    parsed = bill_link.parse(token)
    if parsed is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    tid, oid = parsed
    async with integrations.tenant_db(tid) as db:
        order = await db.get(Order, oid)
        if order is None or order.tenant_id != tid:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse({"v": _bill_live_version(order)}, headers={"Cache-Control": "no-store"})


def _esc(s: str) -> str:
    import html as _html

    return _html.escape(str(s), quote=True)


def _money(v) -> str:
    return f"₹{float(v):,.0f}" if float(v).is_integer() else f"₹{float(v):,.2f}"


def _upi_pay_block(vpa: str, payee: str, shop: str, amount, note: str, label: str = "Amount due") -> str:
    """GPay/PhonePe/Paytm ke ek-tap button (phone), QR (desktop), VPA copy —
    bill page aur saare-bill wale statement page, dono par yahi."""
    from decimal import Decimal

    from app.services import bill_link

    e = _esc
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if not vpa:
        return (f"<section class='card pay'><div class='due'>{e(label)}</div>"
                f"<div class='amount'>{_money(amount)}</div><p class='note'>Please pay at the shop or at delivery.</p></section>")
    params = bill_link.upi_params(vpa, payee or shop, amount, note)
    links = bill_link.app_links(params)
    buttons = "".join(
        f"<a class='mobile-only{' primary' if key == 'gpay' else ''}' href='{e(links['upi'])}' "
        f"data-android='{e(links[key]['android'])}' data-ios='{e(links[key]['ios'])}'>Pay with {e(label_)}</a>"
        for key, label_, _, _ in bill_link.UPI_APPS
    )
    buttons += f"<a class='mobile-only' href='{e(links['upi'])}'>Other UPI app</a>"
    return (
        "<section class='card pay'>"
        f"<div class='due'>{e(label)}</div><div class='amount'>{_money(amount)}</div>"
        f"<div class='apps'>{buttons}</div>"
        f"<div class='qr desktop-only' role='img' aria-label='UPI QR code'>{bill_link.qr_svg(links['upi'])}</div>"
        "<p class='desktop-only note'>Scan with any UPI app on your phone.</p>"
        f"<p class='vpa'>UPI ID: <b>{e(vpa)}</b><button type='button' id='copy-vpa' data-vpa='{e(vpa)}'>Copy</button></p>"
        "<p class='note'>After paying, the shop confirms it on your bill. Keep the payment screenshot until then.</p>"
        "</section>"
    )


@app.get("/b/c/{token}", include_in_schema=False)
async def customer_statement_page(token: str):
    """Grahak ke SAARE baaki bill ek page par — pehle dekho, phir ek tap mein sab ka paisa.

    Payment reminder ka link yahin aata hai. Har bill kholkar kapde, rakam,
    status dikhta hai (apne bill page ka link bhi), neeche kul rakam ke
    GPay/PhonePe/Paytm button. Token grahak+dukaan ka, signed, 1 saal.
    Rakam hamesha LIVE: dukaan par cash de diya to yahan bhi kat jaata hai.
    """
    from decimal import Decimal

    from fastapi.responses import Response as _Resp
    from sqlalchemy import select

    from app.models import Customer, Order, OrderStatus
    from app.models.tenant import Tenant
    from app.services import app_settings, bill_link, integrations, receipt

    def _fail() -> _Resp:
        return _Resp(
            content="<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>Link not valid</title><p style='font:16px system-ui;padding:2rem'>"
                    "This link is not valid. Please ask the shop to send it again.</p>",
            media_type="text/html", status_code=404,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    parsed = bill_link.parse_customer(token)
    if parsed is None:
        return _fail()
    tid, cid = parsed
    e = _esc
    async with integrations.tenant_db(tid) as db:
        cust = await db.get(Customer, cid)
        if cust is None or cust.tenant_id != tid:
            return _fail()
        tenant = await db.get(Tenant, tid)
        s = await app_settings.all_settings(db)
        rows = (
            await db.execute(
                select(Order).where(
                    Order.customer_id == cid,
                    Order.status != OrderStatus.CANCELLED,
                    Order.total_amount.isnot(None),
                    Order.total_amount > Order.amount_paid,
                ).order_by(Order.created_at.desc())
            )
        ).scalars().all()
        shop = (tenant.shop_name if tenant else "") or "Laundry"
        bills = []
        total_due = Decimal("0")
        for o in rows:
            r = receipt.build(order=o, customer_name=cust.name, customer_phone=None, shop_name=shop, settings=s)
            due = Decimal(str(r["due"])).quantize(Decimal("0.01"))
            total_due += due
            items = "".join(
                f"<li><span><span class='name'>{e(it['title'])}</span><small>{e(str(it['qty']) + (' kg' if it['kg'] else ''))}"
                f"{' × ' + _money(it['rate']) if it['rate'] is not None else ''}</small></span>"
                f"<span class='amt'>{_money(it['amount']) if it['amount'] is not None else ''}</span></li>"
                for it in r["items"]
            )
            status_word, status_cls = _STATUS_WORD.get(o.status.name, (o.status.name.title(), ""))
            c = r["clothes"]
            dl = (f" · {c['delivered']} of {c['total']} delivered" if r["partial"] else "")
            bills.append(
                f"<details class='billacc'><summary><span class='b-meta'><b>{e(o.order_number)}</b>"
                f"<small>{e(r['date'])} · <span class='status {status_cls}'>{e(status_word)}</span>{e(dl)}</small></span>"
                f"<span class='b-due'><b>{_money(due)}</b><small>of {_money(r['total'])}</small></span></summary>"
                f"<div class='inner'><ul class='items'>{items}</ul>"
                f"<div class='totals'><div class='row'><span>Total</span><span>{_money(r['total'])}</span></div>"
                f"<div class='row'><span>Paid</span><span>{_money(r['paid'])}</span></div>"
                f"<div class='row due-row'><span><b>Balance due</b></span><span><b>{_money(due)}</b></span></div></div>"
                f"<a class='open' href='/b/{bill_link.make(tid, o.id)}'>Open this bill →</a></div></details>"
            )
        upi = str(s.get("upi_vpa") or "").strip()
        payee = str(s.get("upi_payee") or "").strip()
        terms = receipt.terms_list(s.get("invoice_terms"))
        shop_phone = str(s.get("shop_contact_phone") or "").strip()

    first_name = ((cust.name or "").strip().split() or ["there"])[0]
    n = len(bills)
    if n == 0:
        mood, emoji, title, sub = "paid", "✨", "All clear!", f"{first_name}, you have no pending bills. Thank you!"
        pay_block = ""
    else:
        mood, emoji = "due", "🧾"
        title = f"Hi {first_name}, {n} bill{'s' if n > 1 else ''} pending"
        sub = f"Total payable {_money(total_due)} · open each bill below to see the clothes and amounts."
        pay_block = _upi_pay_block(upi, payee, shop, total_due, f"{n} bills", label="Total payable")
    terms_html = ""
    if terms:
        terms_html = ("<section class='card'><details class='terms'><summary>Terms &amp; conditions</summary><ol>"
                      + "".join(f"<li>{e(t)}</li>" for t in terms) + "</ol></details></section>")
    digits = "".join(ch for ch in shop_phone if ch.isdigit())
    contact_btn = ""
    if digits:
        wa = digits if digits.startswith("91") or len(digits) != 10 else "91" + digits
        contact_btn = f"<a class='contact-btn' href='https://wa.me/{wa}'>💬 WhatsApp the shop</a>"
    phone = "".join(ch for ch in (cust.phone or "") if ch.isdigit())
    customer = e((cust.name or "").strip() or "Customer") + (f" <span class='label'>· ••••{e(phone[-4:])}</span>" if phone else "")
    theme = {"paid": "#059669", "due": "#f97316"}.get(mood, "#1f2937")

    fills = {
        "{{SHOP}}": e(shop), "{{MOOD}}": mood, "{{THEME}}": theme, "{{HERO_EMOJI}}": emoji,
        "{{HERO_TITLE}}": e(title), "{{HERO_SUB}}": e(sub), "{{CUSTOMER}}": customer,
        "{{PAY_BLOCK}}": pay_block,
        "{{BILLS}}": "".join(bills) if bills else "<p class='note'>No pending bills.</p>",
        "{{COUNT}}": str(n), "{{TOTAL}}": _money(total_due),
        "{{TERMS}}": terms_html, "{{CONTACT_BTN}}": contact_btn, "{{ASSET_V}}": _SITE_ASSET_V,
    }
    html = (_SITE_DIR / "templates" / "statement.html").read_text(encoding="utf-8")
    for k, v in fills.items():
        html = html.replace(k, v)
    return _Resp(content=html, media_type="text/html",
                 headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _json(s: str) -> str:
    import json as _json_mod

    return _json_mod.dumps(s)


# Public website — apna folder, dashboard ke static se alag:
#   app/site/templates/index.html   server placeholders ({{...}}) wala HTML
#   app/site/assets/{css,js,img}    /site/assets/... par, saal bhar cache
_SITE_DIR = Path(__file__).resolve().parent / "site"
_SITE_FILE = _SITE_DIR / "templates" / "index.html"
app.mount(
    "/site/assets",
    CachedStaticFiles(directory=_SITE_DIR / "assets"),
    name="site-assets",
)


def _site_asset_version() -> str:
    """CSS/JS badle to naya ?v= — browser purani file cache se na uthaye."""
    import hashlib

    h = hashlib.sha1()
    for p in sorted((_SITE_DIR / "assets").rglob("*.*")):
        if p.suffix in (".css", ".js"):
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


_SITE_ASSET_V = _site_asset_version()

from app.services import site_pages  # noqa: E402  (service pages ka registry)


def _public_base(request: Request) -> str:
    """Canonical/OG/sitemap ke liye poora origin.

    SITE_URL set ho (production: https://kwikklin.online) to HAMESHA wahi —
    warna IP/duckdns/tunnel se khula page apna alag canonical deta aur Google
    ek hi page ke kai URL index karta (duplicate content). Set na ho to
    request se (proxy ke X-Forwarded-* pehle); Host header bahar se aata hai
    — sirf saaf hostname hi maana jaata hai."""
    import re as _re

    if settings.SITE_URL:
        return settings.SITE_URL.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if proto not in ("http", "https"):
        proto = "https"
    if not _re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", host):
        host = request.url.netloc
    return f"{proto}://{host}"


@app.get("/laundry", include_in_schema=False)
async def site_old_url():
    """Purana pata — 301 taaki Google ranking aur purane links naye / par aayein."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/", status_code=301)


@app.get("/", include_in_schema=False)
async def site_page(request: Request):
    """Kwik Klin ki public website (main domain) — grahak (pickup, rate list,
    reviews), franchise aur CRM. Local SEO mein domain ka root sabse taqatwar
    page hai, isliye laundry site yahin; CRM signup /join par. Rate list HOME
    dukaan ke CRM rate card se aati hai (app/services/site_rates.py)."""
    from fastapi.responses import Response as _Resp

    from app.services import google_business, site_rates

    html = (
        _SITE_FILE.read_text(encoding="utf-8")
        .replace("__BASE__", _public_base(request))
        # dev mein --reload har badlaav par process naya karta hai; prod mein deploy par
        .replace("{{ASSET_V}}", _SITE_ASSET_V)
    )
    html = await site_rates.render(html)
    import html as _html

    html = html.replace("{{AREAS_HTML}}", "".join(f"<li>{_html.escape(a)}</li>" for a in site_pages.AREAS))
    summary, cards = google_business.render_block(await google_business.home_reviews())
    html = html.replace("{{GBP_SUMMARY}}", summary).replace("{{GBP_REVIEWS}}", cards)
    return _Resp(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})


# Legal pages: ek shell (templates/legal.html) + har page ka content
# (templates/legal/*.html). Google OAuth verification aur grahak dono ke liye.
# Razorpay website verification bhi yahi pages maangta hai: About, Contact,
# Privacy, Terms, Cancellation & Refund, Shipping & Delivery (+ daam /laundry par).
_LEGAL_PAGES = {
    "about": ("About Kwik Klin", "Our story", "Kwik Klin was founded in Varanasi by Suyash Srivastava to make laundry hassle-free — every order tracked from pickup to delivery, with free pickup across Varanasi."),
    "contact": ("Contact Us", "Company", "Contact Kwik Klin in Varanasi on WhatsApp or phone, or visit our shop at Sundarpur Chauraha."),
    "privacy": ("Privacy Policy", "Legal", "How Kwik Klin collects, uses and protects personal data for laundry customers, website visitors and CRM businesses."),
    "terms": ("Terms of Service", "Legal", "Terms for the Kwik Klin laundry service in Varanasi, our website, WhatsApp assistant and the Kwik Klin CRM."),
    "refund-policy": ("Cancellation & Refund Policy", "Legal", "How cancellations and refunds work for Kwik Klin laundry orders and CRM subscriptions."),
    "shipping-policy": ("Shipping & Delivery Policy", "Legal", "Laundry pickup and delivery areas and timelines in Varanasi, and online delivery of the Kwik Klin CRM."),
}
_LEGAL_UPDATED = "17 September 2026"


def _legal_route(name: str):
    import html as _html

    title, eyebrow, description = _LEGAL_PAGES[name]

    async def page(request: Request):
        from fastapi.responses import Response as _Resp

        body = (_SITE_DIR / "templates" / "legal" / f"{name}.html").read_text(encoding="utf-8")
        html = (
            (_SITE_DIR / "templates" / "legal.html").read_text(encoding="utf-8")
            .replace("{{BODY}}", body)
            .replace("{{TITLE}}", _html.escape(title))
            .replace("{{EYEBROW}}", eyebrow)
            .replace("{{DESCRIPTION}}", _html.escape(description))
            .replace("{{PATH}}", f"/{name}")
            .replace("{{UPDATED}}", _LEGAL_UPDATED)
            # header/footer mein is page ka link "abhi yahan ho" dikhe
            .replace(f'<a href="/{name}">', f'<a href="/{name}" aria-current="page">')
            .replace("{{ASSET_V}}", _SITE_ASSET_V)
            .replace("__BASE__", _public_base(request))
        )
        return _Resp(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return page


for _name in _LEGAL_PAGES:
    app.add_api_route(f"/{_name}", _legal_route(_name), methods=["GET"], include_in_schema=False)


def _service_route(slug: str):
    """Local SEO service page: templates/service.html shell + services/<slug>.html
    content + CRM daam + FAQ (HTML aur JSON-LD ek hi data se)."""
    import html as _html
    import json as _json
    from urllib.parse import quote

    meta = site_pages.SERVICES[slug]

    async def page(request: Request):
        from fastapi.responses import Response as _Resp

        from app.services import site_rates

        base = _public_base(request)
        esc = _html.escape
        rates_html, offers = await site_rates.category_block(meta["rates"])
        faq_html = "".join(
            f'<details class="q"{" open" if i == 0 else ""}><summary>{esc(q)}</summary><p>{esc(a)}</p></details>'
            for i, (q, a) in enumerate(meta["faq"])
        )
        related = "".join(
            f'<li><a href="/{s}">{esc(m["name"])} in Varanasi</a></li>'
            for s, m in site_pages.SERVICES.items() if s != slug
        )
        ld = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Service",
                    "@id": f"{base}/{slug}#service",
                    "name": f'{meta["name"]} in Varanasi',
                    "serviceType": meta["name"],
                    "description": meta["description"],
                    "url": f"{base}/{slug}",
                    "areaServed": {"@type": "City", "name": "Varanasi"},
                    "provider": {
                        "@type": "DryCleaningOrLaundry", "@id": f"{base}/#business", "name": "Kwik Klin",
                        "telephone": "+91-96968-56069", "url": f"{base}/",
                        "address": {"@type": "PostalAddress", "streetAddress": "Sundarpur Chauraha",
                                    "addressLocality": "Varanasi", "addressRegion": "Uttar Pradesh",
                                    "postalCode": "221005", "addressCountry": "IN"},
                    },
                    **({"offers": offers} if offers else {}),
                },
                {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{base}/"},
                        {"@type": "ListItem", "position": 2, "name": "Services", "item": f"{base}/#services"},
                        {"@type": "ListItem", "position": 3, "name": meta["name"], "item": f"{base}/{slug}"},
                    ],
                },
                {
                    "@type": "FAQPage",
                    "mainEntity": [
                        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                        for q, a in meta["faq"]
                    ],
                },
            ],
        }
        body = (_SITE_DIR / "templates" / "services" / f"{slug}.html").read_text(encoding="utf-8")
        fills = {
            "{{BODY}}": body,
            "{{JSON_LD}}": _json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"),
            "{{RATES}}": rates_html,
            "{{FAQ}}": faq_html,
            "{{RELATED}}": related,
            "{{AREAS}}": "".join(f"<li>{esc(a)}</li>" for a in site_pages.AREAS),
            "{{TITLE}}": esc(meta["title"]),
            "{{DESCRIPTION}}": esc(meta["description"]),
            "{{H1}}": esc(meta["h1"]),
            "{{LEAD}}": esc(meta["lead"]),
            "{{NAME_LOWER}}": esc(meta["name"].lower()),
            "{{NAME}}": esc(meta["name"]),
            "{{SLUG}}": slug,
            "{{WA_TEXT}}": quote(f'Hello Kwik Klin, I would like {meta["name"].lower()} in Varanasi.'),
            "{{ASSET_V}}": _SITE_ASSET_V,
            "__BASE__": base,
        }
        html = (_SITE_DIR / "templates" / "service.html").read_text(encoding="utf-8")
        for k, v in fills.items():
            html = html.replace(k, v)
        html = html.replace(f'<a href="/{slug}">', f'<a href="/{slug}" aria-current="page">')
        return _Resp(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return page


for _slug in site_pages.SERVICES:
    app.add_api_route(f"/{_slug}", _service_route(_slug), methods=["GET"], include_in_schema=False)


@app.get("/api/public/reviews", include_in_schema=False)
async def public_reviews() -> JSONResponse:
    """Website ke liye Google reviews (cached). Key na ho to configured=false."""
    from app.services import google_reviews

    return JSONResponse(
        await google_reviews.get_reviews(),
        headers={"Cache-Control": "public, max-age=600"},
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /$\n"
        + "".join(f"Allow: /{n}\n" for n in (*site_pages.SERVICES, *_LEGAL_PAGES)) +
        "Allow: /join\n"
        "Allow: /admin/static/\n"
        "Allow: /site/assets/\n"
        # Google page ko JS ke saath render karta hai — reviews/plans ki
        # API band ho to wo hissa use khali dikhta hai
        "Allow: /api/public/\n"
        "Allow: /api/plans\n"
        "Disallow: /admin\n"
        "Disallow: /control\n"
        "Disallow: /staff\n"
        "Disallow: /pay/\n"
        "Disallow: /b/\n"
        "Disallow: /r/\n"
        "Disallow: /api/\n"
        "Disallow: /webhook\n"
        f"\nSitemap: {_public_base(request)}/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    from datetime import date as _date

    from fastapi.responses import Response as _Resp

    base = _public_base(request)
    tpl = _SITE_DIR / "templates"

    def url(path: str, file: Path, priority: str) -> str:
        mod = _date.fromtimestamp(file.stat().st_mtime).isoformat()
        return f"  <url><loc>{base}{path}</loc><lastmod>{mod}</lastmod><priority>{priority}</priority></url>\n"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + url("/", _SITE_FILE, "1.0")
        + "".join(url(f"/{s}", tpl / "services" / f"{s}.html", "0.9") for s in site_pages.SERVICES)
        + url("/join", _JOIN_FILE, "0.6")
        + "".join(url(f"/{n}", tpl / "legal" / f"{n}.html", "0.3") for n in _LEGAL_PAGES)
        + "</urlset>\n"
    )
    return _Resp(content=xml, media_type="application/xml")


@app.get("/join", include_in_schema=False)
async def join_page():
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_JOIN_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


# Super-admin (vendor) panel ka HTML shell. Shell mein zero data hai — har
# API call X-API-Key maangti hai (require_vendor_key), jo page par daalni
# padti hai. Isi liye ye route control router (key-gated) ke BAHAR hai.
_CONTROL_FILE = Path(__file__).resolve().parent / "static" / "control.html"


@app.get("/control", include_in_schema=False)
async def control_page():
    """Vendor panel ka shell.

    STRICT CSP: script/style sirf apne origin se (no 'unsafe-inline') —
    isi liye JS/CSS externalized hain aur markup mein koi inline handler
    ya style attribute nahi. XSS ghus bhi jaye to script chal hi nahi
    sakti, aur vendor session cookie httpOnly hai to padhi bhi nahi jaa
    sakti. Asset links ?v=<mtime> se aate hain, isliye HTML no-store
    rehta hai par JS/CSS immutable cache hote hain.
    """
    from fastapi.responses import Response as _Resp

    html = _CONTROL_FILE.read_text(encoding="utf-8")
    static_dir = _CONTROL_FILE.parent
    v = int(max(
        (static_dir / "control.js").stat().st_mtime,
        (static_dir / "control.css").stat().st_mtime,
        (static_dir / "tokens.css").stat().st_mtime,
    ))
    html = html.replace("__V__", str(v))
    return _Resp(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
        },
    )


_BILLING_FILE = Path(__file__).resolve().parent / "static" / "billing.html"


@app.get("/billing", include_in_schema=False)
async def billing_page():
    """Client ka plan/usage/recharge page (data /api/billing/* se, session par)."""
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_BILLING_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


_STAFF_FILE = Path(__file__).resolve().parent / "static" / "staff.html"


@app.get("/staff", include_in_schema=False)
async def staff_page():
    """Dukaan ke aadmi ka panel. Page khud khula hai — data har call par
    kk_staff cookie se guzarta hai, aur wo cookie sirf /staff par jaati hai.

    CSP yahan bhi sakht: koi inline script nahi, koi bahar ka origin nahi.
    """
    from fastapi.responses import Response as _Resp

    html = _STAFF_FILE.read_text(encoding="utf-8")
    static_dir = _STAFF_FILE.parent
    v = int(max(
        (static_dir / "staff.js").stat().st_mtime,
        (static_dir / "staff.css").stat().st_mtime,
        (static_dir / "tokens.css").stat().st_mtime,
    ))
    import re as _re

    html = _re.sub(r"((?:staff|tokens)\.(?:js|css))\?v=[\w]+", rf"\1?v={v}", html)
    return _Resp(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


@app.get("/staff/manifest.webmanifest", include_in_schema=False)
async def staff_manifest():
    """Phone par 'Add to home screen' — isi file se app ki tarah lagta hai."""
    from fastapi.responses import Response as _Resp

    path = Path(__file__).resolve().parent / "static" / "staff-manifest.json"
    return _Resp(
        content=path.read_text(encoding="utf-8"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/staff/icon.svg", include_in_schema=False)
async def staff_icon():
    from fastapi.responses import Response as _Resp

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
        '<rect width="192" height="192" rx="42" fill="#111827"/>'
        '<rect x="26" y="26" width="140" height="140" rx="34" fill="#f97316"/>'
        '<text x="96" y="124" font-family="system-ui,sans-serif" font-size="72" '
        'font-weight="800" fill="#fff" text-anchor="middle">KK</text></svg>'
    )
    return _Resp(content=svg, media_type="image/svg+xml",
                 headers={"Cache-Control": "public, max-age=86400"})


@app.get("/staff/sw.js", include_in_schema=False)
async def staff_sw():
    """Service worker — sirf itna ki app install ho sake aur kholte hi
    khul jaye. Kaam ka data JAAN-BOOJH KAR cache nahi hota: purana order
    dikhana kisi ko galat kaam par bhej sakta hai."""
    from fastapi.responses import Response as _Resp

    js = (
        "const SHELL='kkstaff-v2';\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(SHELL)"
        ".then(c=>c.addAll(['/staff','/admin/static/staff.css','/admin/static/staff.js'])))});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>"
        "Promise.all(k.filter(x=>x!==SHELL).map(x=>caches.delete(x)))))});\n"
        "self.addEventListener('fetch',e=>{\n"
        "  const u=new URL(e.request.url);\n"
        "  if(e.request.method!=='GET'||u.pathname.startsWith('/staff/api')) return;\n"
        "  e.respondWith(fetch(e.request).then(r=>{\n"
        "    const copy=r.clone(); caches.open(SHELL).then(c=>c.put(e.request,copy)); return r;\n"
        "  }).catch(()=>caches.match(e.request)));\n"
        "});\n"
    )
    return _Resp(content=js, media_type="application/javascript",
                 headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/staff"})


@app.get("/social/{name}")
async def social_image(name: str):
    """Public marketing posters (Instagram fetches from here). Only files the
    daily social job created — nothing else in media/ is ever exposed."""
    from pathlib import Path as _P

    from fastapi.responses import FileResponse

    safe = _P(name).name
    if not (safe.startswith("social-") and safe.endswith(".png")):
        return JSONResponse(status_code=404, content={"detail": "not found"})
    path = _P(__file__).resolve().parent / "media" / safe
    if not path.exists():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return FileResponse(path, media_type="image/png")


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness + DB connectivity check. Never raises: reports instead."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("health_check_db_failed")
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": "unreachable"},
        )
    return JSONResponse(content={"status": "ok", "db": "connected"})
