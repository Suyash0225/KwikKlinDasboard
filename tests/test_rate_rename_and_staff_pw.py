"""Rate card ka naam ✏️ se badalna, aur staff ka aasaan pehla password."""

from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services.auth import hash_password, simple_temp_password

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SVC, SVC2, G, G2 = "TEST Rn Press", "TEST Rn Steam", "TEST Rn Kurta", "TEST Rn Saree"


def test_simple_temp_password_is_name_plus_four_digits() -> None:
    pw = simple_temp_password("Ravi Kumar")
    assert pw.startswith("ravi") and len(pw) == 8 and pw[4:].isdigit()
    # Chhota/hindi naam — phir bhi 8 akshar, taaki hash_password maane
    for name in ("Om", "रवि", ""):
        pw = simple_temp_password(name)
        assert pw.startswith("kwik") and len(pw) == 8
        hash_password(pw)


async def _cleanup() -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("DELETE FROM rate_card WHERE service LIKE 'TEST Rn %' OR garment LIKE 'TEST Rn %'")
        )
        await db.commit()


async def test_rename_service_and_garment_keeps_rates(client) -> None:
    await _cleanup()
    try:
        for svc, g, rate in ((SVC, G, 20), (SVC, "", 50)):
            r = await client.post("/admin/api/rates", json={
                "service": svc, "garment": g, "unit": "kg" if not g else "pc", "rate": rate,
            }, headers=AUTH)
            assert r.status_code == 201, r.text
        await client.put("/admin/api/settings", json={"key": "service_stages", "value": {SVC: ["IN_IRON"]}}, headers=AUTH)

        r = await client.post("/admin/api/rates/rename", json={"kind": "service", "old": SVC, "new": SVC2}, headers=AUTH)
        assert r.status_code == 200 and r.json()["renamed"] == 2, r.text
        r = await client.post("/admin/api/rates/rename", json={"kind": "garment", "old": G, "new": G2}, headers=AUTH)
        assert r.status_code == 200 and r.json()["renamed"] == 1, r.text

        rows = [x for x in (await client.get("/admin/api/rates", headers=AUTH)).json() if x["service"].startswith("TEST Rn")]
        assert {(x["service"], x["garment"], float(x["rate"])) for x in rows} == {(SVC2, G2, 20.0), (SVC2, "", 50.0)}
        stages = (await client.get("/admin/api/settings", headers=AUTH)).json()["service_stages"]
        assert stages.get(SVC2) == ["IN_IRON"] and SVC not in stages

        # Naya naam pehle se hai to jodi dobara nahi banti
        await client.post("/admin/api/rates", json={"service": SVC, "garment": G2, "unit": "pc", "rate": 30}, headers=AUTH)
        r = await client.post("/admin/api/rates/rename", json={"kind": "service", "old": SVC, "new": SVC2}, headers=AUTH)
        assert r.status_code == 409
    finally:
        await client.put("/admin/api/settings", json={"key": "service_stages", "value": {}}, headers=AUTH)
        await _cleanup()
