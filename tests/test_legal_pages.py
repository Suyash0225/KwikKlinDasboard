"""Company/legal pages (Google OAuth + Razorpay verification): public, rendered, linked, listed."""

PAGES = {
    "/about": ["About Kwik Klin", "Suyash Srivastava", "founder-suyash.jpg", "Our promise"],
    "/contact": ["Contact Us", "+91 96968 56069", "Sundarpur Chauraha"],
    "/privacy": ["Privacy Policy", "Limited Use", "STOP", "Grievance Officer"],
    "/terms": ["Terms of Service", "24 hours", "10 times the service charge", "Varanasi"],
    "/refund-policy": ["Cancellation &amp; Refund Policy", "5–7 working days", "original payment method"],
    "/shipping-policy": ["Shipping &amp; Delivery Policy", "within Varanasi", "4 days"],
}


async def test_pages_render_without_placeholders(client) -> None:
    for path, must in PAGES.items():
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "{{" not in r.text and "__BASE__" not in r.text, path
        for text in must:
            assert text in r.text, (path, text)
        assert f'rel="canonical" href="http://test{path}"' in r.text
        assert f'<a href="{path}" aria-current="page">' in r.text


async def test_pages_are_linked_and_listed(client) -> None:
    for page in ("/", "/join"):
        html = (await client.get(page)).text
        for path in PAGES:
            assert f'href="{path}"' in html, (page, path)
    sitemap = (await client.get("/sitemap.xml")).text
    robots = (await client.get("/robots.txt")).text
    for path in PAGES:
        assert f"{path}</loc>" in sitemap and f"Allow: {path}" in robots, path
