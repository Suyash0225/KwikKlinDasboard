"""Staff app ka PWA — install, icons, service worker ki hadein.

Teen cheezein jo pehle chupke se tooti hui thin aur in tests ka asli kaam:
1. SW `/staff/` scope mein register hota tha, jo `/staff` page ko gherta hi
   nahi — offline shell aur serviceWorker.ready dono kabhi chale hi nahi.
2. Staff aur admin ke SW ek doosre ka cache mita dete the.
3. Precache list mein ek bhi galat URL ho to `cache.addAll` poora install
   gira deta hai — aur ye browser ke andar chupchaap hota hai.
"""
import io
import re
from pathlib import Path

from PIL import Image

from app.config import settings

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def test_manifest_is_installable_with_png_icons(client) -> None:
    r = await client.get("/staff/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "Kwik Klin Staff"
    assert m["short_name"] == "Kwik Klin"
    assert m["start_url"] == "/staff" and m["scope"] == "/staff"
    assert m["display"] == "standalone"
    assert m["theme_color"] and m["background_color"]

    pngs = {(i["sizes"], i.get("purpose", "any")): i["src"] for i in m["icons"] if i["type"] == "image/png"}
    assert ("192x192", "any") in pngs and ("512x512", "any") in pngs
    assert ("512x512", "maskable") in pngs
    # Har icon sach mein wahi size ka PNG hai jo manifest kehta hai
    for (sizes, _), src in pngs.items():
        ir = await client.get(src)
        assert ir.status_code == 200, src
        assert ir.headers["content-type"] == "image/png"
        w, h = Image.open(io.BytesIO(ir.content)).size
        assert f"{w}x{h}" == sizes, src


async def test_icon_route_serves_only_known_names(client) -> None:
    assert (await client.get("/staff/apple-touch-icon.png")).status_code == 200
    assert (await client.get("/staff/badge-96.png")).status_code == 200
    assert (await client.get("/staff/nope.png")).status_code == 404
    assert (await client.get("/staff/..%2fmain.png")).status_code == 404


async def test_page_links_manifest_and_ios_icon(client) -> None:
    html = (await client.get("/staff")).text
    assert '<link rel="manifest" href="/staff/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon" href="/staff/apple-touch-icon.png"' in html
    assert 'id="iosinstall"' in html          # Safari ka install button nahi hota
    assert 'id="boot-off"' in html            # offline par login form nahi


async def test_service_worker_headers_and_scope(client) -> None:
    sw = await client.get("/staff/sw.js")
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("application/javascript")
    assert sw.headers["cache-control"] == "no-cache"
    # "/staff" (bina slash) scope ke liye ye header zaroori hai
    assert sw.headers["service-worker-allowed"] == "/staff"
    js = (STATIC / "staff.js").read_text(encoding="utf-8")
    assert 'register("/staff/sw.js", { scope: "/staff" })' in js


async def test_service_worker_never_touches_api_or_other_caches(client) -> None:
    js = (await client.get("/staff/sw.js")).text
    # API kabhi intercept nahi: respondWith se pehle hi lautna
    assert 'if (url.pathname.startsWith("/staff/api")) return;' in js
    assert js.index('startsWith("/staff/api")') < js.index("e.respondWith")
    # Sirf allowlist cache hoti hai, aur sirf poore 200 jawab
    assert "if (!SHELL_SET.has(url.pathname)) return;" in js
    assert 'res.ok && res.type === "basic"' in js
    # Purana cache sirf apne prefix ka mitta hai — admin ka "kk-shell-*" nahi
    assert "k.startsWith(PREFIX) && k !== CACHE" in js
    admin_sw = (await client.get("/admin/sw.js")).text
    assert "k.startsWith(PREFIX) && k !== CACHE" in admin_sw
    staff_prefix = re.search(r'const PREFIX = "([^"]+)"', js).group(1)
    admin_prefix = re.search(r'const PREFIX = "([^"]+)"', admin_sw).group(1)
    assert not staff_prefix.startswith(admin_prefix) and not admin_prefix.startswith(staff_prefix)
    # Shell na mile to bhi khali/toota page nahi
    assert "offlinePage()" in js


async def test_every_precached_url_exists(client) -> None:
    """addAll all-or-nothing hai: ek 404 = SW install hi nahi hota."""
    js = (await client.get("/staff/sw.js")).text
    block = re.search(r"const SHELL = \[(.*?)\];", js, re.S).group(1)
    urls = re.findall(r'"(/[^"]*)"', block)
    assert "/admin/static/staff.js" in urls
    assert not [u for u in urls if u.startswith("/staff/api")]
    for u in urls:
        assert (await client.get(u)).status_code == 200, u
    # PAGE const bhi list ka hissa hai
    assert re.search(r'const PAGE = "/staff";', js) and re.search(r"\bPAGE,", block)


async def test_dashboard_versions_ui_v2_css(client) -> None:
    """ui-v2.css ?v=3 par atka tha — immutable cache mein nayi CSS kabhi
    phone tak nahi pahunchti."""
    html = (await client.get("/admin", headers=AUTH)).text
    assert re.search(r"ui-v2\.css\?v=\d{6,}", html), "ui-v2.css must carry an mtime version"


def test_bill_progress_is_defined() -> None:
    """Bills tab har card par billProgress() bulata hai; function chhoot
    gaya tha aur poora tab ReferenceError se khali reh jaata tha."""
    js = (STATIC / "staff.js").read_text(encoding="utf-8")
    assert "${billProgress(" in js
    assert "function billProgress(" in js
