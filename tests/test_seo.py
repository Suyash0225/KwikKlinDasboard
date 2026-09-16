"""Local SEO: laundry site on the domain root, service pages, canonical, sitemap."""

import json
import re

from app.config import settings
from app.services import site_pages


async def test_root_is_the_laundry_site_and_old_url_redirects(client) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "<title>Laundry &amp; Dry Cleaning in Varanasi (Banaras)" in r.text
    assert 'rel="canonical" href="http://test/"' in r.text
    assert "{{" not in r.text
    for slug in site_pages.SERVICES:
        assert f'href="/{slug}"' in r.text, slug
    assert "<li>Lanka</li>" in r.text

    old = await client.get("/laundry", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == "/"
    assert (await client.get("/join")).status_code == 200


async def test_service_pages_have_unique_seo_and_valid_json_ld(client) -> None:
    titles = set()
    for slug, meta in site_pages.SERVICES.items():
        r = await client.get(f"/{slug}")
        assert r.status_code == 200, slug
        html = r.text
        assert "{{" not in html and "__BASE__" not in html, slug
        title = re.search(r"<title>(.*?)</title>", html).group(1)
        assert "Varanasi" in title and title not in titles
        titles.add(title)
        assert html.count("<h1>") == 1
        assert f'rel="canonical" href="http://test/{slug}"' in html
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
        types = {g["@type"] for g in ld["@graph"]}
        assert types == {"Service", "BreadcrumbList", "FAQPage"}
        faq = next(g for g in ld["@graph"] if g["@type"] == "FAQPage")
        assert len(faq["mainEntity"]) == len(meta["faq"])
        # same FAQ visible on the page (Google requires it)
        for q, _ in meta["faq"]:
            assert q.replace("&", "&amp;") in html


async def test_site_url_setting_pins_canonical_and_sitemap(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SITE_URL", "https://kwikklin.online/")
    home = (await client.get("/")).text
    assert 'rel="canonical" href="https://kwikklin.online/"' in home
    sitemap = (await client.get("/sitemap.xml")).text
    assert "<loc>https://kwikklin.online/</loc>" in sitemap
    for slug in site_pages.SERVICES:
        assert f"<loc>https://kwikklin.online/{slug}</loc>" in sitemap
    assert "/laundry<" not in sitemap
    robots = (await client.get("/robots.txt")).text
    assert "Sitemap: https://kwikklin.online/sitemap.xml" in robots
