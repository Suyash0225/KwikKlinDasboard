"""/admin ki CSP — abhi report-only, aur wo jaan-bujh kar.

/control par yahi policy asli mein lagi hai aur wahan markup saaf hai.
/admin par abhi ~264 inline handler bache hain, to enforce karte hi har
button mar jaata. Report-only isliye: page chalta rehta hai aur browser
khud gin kar batata hai ki kitna kaam baaki hai.

In tests ka kaam do hai:
1. header galti se enforcing na ho jaye (jab tak markup saaf na ho)
2. jis din markup saaf ho, ek hi jagah badalni pade — aur ye tests bata
   dein ki kahan
"""

import app.routers.admin as admin_mod
from app.config import settings

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
RO = "content-security-policy-report-only"


async def test_admin_page_reports_but_does_not_enforce(client) -> None:
    r = await client.get("/admin", headers=AUTH)

    assert r.status_code == 200
    policy = r.headers.get(RO)
    assert policy, "report-only CSP header hi nahi hai"
    # Enforcing header abhi NAHI — warna dashboard ka har onclick mar jaata.
    assert "content-security-policy" not in {
        k.lower() for k in r.headers if k.lower() != RO
    }
    assert "script-src 'self'" in policy
    assert "report-uri /admin/csp-report" in policy


async def test_scripts_may_not_be_inlined_even_in_report_mode(client) -> None:
    """script-src par 'unsafe-inline' kabhi nahi — asli XSS rasta wahi hai.

    style-src par abhi hai (242 style attributes), aur wo alag kaam hai jo
    is header ko rok nahi sakta.
    """
    policy = (await client.get("/admin", headers=AUTH)).headers[RO]

    script_part = next(p for p in policy.split(";") if "script-src" in p)
    assert "unsafe-inline" not in script_part
    assert "unsafe-eval" not in script_part


async def test_a_violation_is_counted_without_auth(client) -> None:
    """Browser report bina cookie/API-key ke bhejta hai — endpoint khula hai."""
    admin_mod._CSP_SEEN.clear()
    report = {"csp-report": {
        "effective-directive": "script-src-attr",
        "blocked-uri": "inline",
        "document-uri": "http://test/admin",
        "line-number": 412,
    }}

    r = await client.post("/admin/csp-report", json=report)

    assert r.status_code == 204
    assert admin_mod._CSP_SEEN[("script-src-attr", "inline")] == 1


async def test_repeats_are_counted_not_relogged(client) -> None:
    """Ek dashboard load 260+ violations bhejta hai. Ginti rakho, log nahi."""
    admin_mod._CSP_SEEN.clear()
    report = {"csp-report": {
        "effective-directive": "script-src-attr", "blocked-uri": "inline",
    }}
    for _ in range(5):
        await client.post("/admin/csp-report", json=report)

    summary = (await client.get("/admin/csp-report", headers=AUTH)).json()

    assert summary["distinct"] == 1, "ek hi tarah ki violation, ek hi row"
    assert summary["total"] == 5
    assert summary["violations"][0]["count"] == 5


async def test_junk_reports_do_not_raise(client) -> None:
    """Report endpoint khula hai — kachra bhejne par 500 nahi girna chahiye."""
    admin_mod._CSP_SEEN.clear()

    assert (await client.post("/admin/csp-report", content=b"not json")).status_code == 204
    assert (await client.post("/admin/csp-report", json=[1, 2, 3])).status_code == 204
    assert (await client.post("/admin/csp-report", json={})).status_code == 204


async def test_summary_needs_the_admin_key(client) -> None:
    assert (await client.get("/admin/csp-report")).status_code == 401
