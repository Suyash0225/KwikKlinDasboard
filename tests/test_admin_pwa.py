"""Owner dashboard (/admin) ka PWA — install, icons, service worker ki hadein.

Staff app wale tests (test_staff_pwa.py) ka jodidaar. Yahan khaas:
1. Manifest /admin/static/ se aata tha — 1 saal ka immutable cache, to naam
   ya icon badalna phone tak kabhi nahi pahunchta.
2. SW har /admin/static/* aur har jawab (401/500 bhi) cache kar leta tha.
3. Mobile par input 16px se chhote the — iPhone tap par page zoom karta.
"""
import io
import re
from pathlib import Path

from PIL import Image

from app.config import settings

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def test_manifest_is_served_fresh_with_png_icons(client) -> None:
    r = await client.get("/admin/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert "immutable" not in r.headers.get("cache-control", "")
    m = r.json()
    assert m["start_url"] == "/admin" and m["scope"] == "/admin" and m["id"] == "/admin"
    assert m["display"] == "standalone"
    # staff app ka short_name "Kwik Klin" hai — ek hi phone par do ek-jaise naam na hon
    staff = (await client.get("/staff/manifest.webmanifest")).json()
    assert m["short_name"] != staff["short_name"]

    pngs = {(i["sizes"], i.get("purpose", "any")): i["src"] for i in m["icons"] if i["type"] == "image/png"}
    assert ("192x192", "any") in pngs and ("512x512", "any") in pngs and ("512x512", "maskable") in pngs
    staff_srcs = {i["src"] for i in staff["icons"]}
    for (sizes, _), src in pngs.items():
        assert src not in staff_srcs            # owner ka icon alag dikhe
        ir = await client.get(src)              # bina login — browser cookie nahi bhejta
        assert ir.status_code == 200, src
        assert ir.headers["content-type"] == "image/png"
        w, h = Image.open(io.BytesIO(ir.content)).size
        assert f"{w}x{h}" == sizes, src


async def test_icon_route_serves_only_known_names(client) -> None:
    assert (await client.get("/admin/apple-touch-icon.png")).status_code == 200
    assert (await client.get("/admin/nope.png")).status_code == 404
    assert (await client.get("/admin/..%2fmain.png")).status_code == 404


async def test_dashboard_links_manifest_icon_and_install_ui(client) -> None:
    html = (await client.get("/admin", headers=AUTH)).text
    assert '<link rel="manifest" href="/admin/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon" href="/admin/apple-touch-icon.png"' in html
    assert 'id="install-bar"' in html and 'id="side-install"' in html
    # gbp-poster.js bhi har deploy par naya version maange
    assert re.search(r"gbp-poster\.js\?v=\d{6,}\"", html)
    assert re.search(r"app\.js\?v=\d{6,}\"", html), "old '-18' suffix must not survive the rewrite"


async def test_service_worker_caches_only_the_shell(client) -> None:
    sw = await client.get("/admin/sw.js")
    assert sw.headers["service-worker-allowed"] == "/admin"
    assert sw.headers["cache-control"] == "no-cache"
    js = sw.text
    for guard in ('if (url.pathname.startsWith("/admin/api")) return;',
                  'if (url.pathname.startsWith("/admin/media")) return;'):
        assert guard in js
        assert js.index(guard) < js.index("e.respondWith")
    assert "if (!SHELL_SET.has(url.pathname)) return;" in js
    assert 'res.ok && res.type === "basic" && !res.redirected' in js
    assert "offlinePage()" in js


async def test_every_precached_url_exists(client) -> None:
    """addAll all-or-nothing hai: ek 404 = SW install hi nahi hota."""
    js = (await client.get("/admin/sw.js")).text
    block = re.search(r"const SHELL = \[(.*?)\];", js, re.S).group(1)
    urls = re.findall(r'"(/[^"]*)"', block)
    assert "/admin/static/app.js" in urls
    assert not [u for u in urls if u.startswith(("/admin/api", "/admin/media"))]
    for u in urls:
        assert (await client.get(u)).status_code == 200, u
    assert re.search(r'const PAGE = "/admin";', js) and re.search(r"\bPAGE,", block)


def test_touch_inputs_are_16px() -> None:
    """iPhone Safari 16px se chhote input par tap karte hi zoom karta hai."""
    css = (STATIC / "ui-v2.css").read_text(encoding="utf-8")
    rule = re.search(r"@media \(pointer: coarse\) \{\s*input:not\(.*?\{ font-size: 16px; \}", css, re.S)
    assert rule, "coarse-pointer 16px input rule missing"
