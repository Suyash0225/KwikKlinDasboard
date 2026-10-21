"""Grahak ki rating -> staff ke points, aur late kaam ki yaad.

Fixtures test_staff_panel se — dukaan A ka washerman (Awash), delivery
(Adel) aur manager (Amgr).
"""

from datetime import date, datetime, timedelta

from sqlalchemy import text as sqltext

from app.database import async_session_factory
from tests.test_staff_panel import (  # noqa: F401  (two_shops pytest fixture hai)
    A_DEL_PHONE, A_MGR_PHONE, A_PHONE, CUST_B, _login, _order_for, two_shops,
)

# CUST_A ka number hi dukaan A ke delivery wale ka hai (test_staff_panel) —
# grahak aur staff ke message alag pehchaanne ke liye CUST_B
IST_11 = datetime(2026, 10, 21, 11, 5)


async def _move(client, number: str) -> None:
    """Washerman dhoye, delivery wala pahunchaye — asli raste se."""
    await _login(client, A_PHONE)
    assert (await client.post(f"/staff/api/orders/{number}/washing")).status_code == 200
    assert (await client.post(f"/staff/api/orders/{number}/ready")).status_code == 200
    await _login(client, A_DEL_PHONE)
    r = await client.post(f"/staff/api/orders/{number}/deliver", json={"items": None})
    assert r.status_code == 200, r.text


async def _set(tenant_id, key, value) -> None:
    from app.services import app_settings, tenant_context

    token = tenant_context.current_tenant_id.set(tenant_id)
    try:
        async with async_session_factory() as db:
            await app_settings.set_value(db, key, value)
    finally:
        tenant_context.current_tenant_id.reset(token)


async def _rate(tenant_id, kind: str) -> None:
    """Grahak ne delivery ke message par button dabaya."""
    from app.models import Customer
    from app.routers.webhook import _handle_rating
    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(tenant_id)
    try:
        async with async_session_factory() as db:
            from sqlalchemy import select

            cust = (await db.execute(select(Customer).where(Customer.phone == CUST_B))).scalar_one()
            await _handle_rating(db, cust, CUST_B, kind)
    finally:
        tenant_context.current_tenant_id.reset(token)


async def _points(tenant_id) -> dict:
    async with async_session_factory() as db:
        rows = (await db.execute(sqltext(
            "SELECT s.name, p.points, p.rating, p.role FROM staff_points p JOIN staff s ON s.id = p.staff_id"
            " WHERE p.tenant_id = :t"), {"t": str(tenant_id)})).all()
    return {name: (pts, rating, role) for name, pts, rating, role in rows}


def _ask_about(monkeypatch, subject: str) -> None:
    """Sawaal kis cheez ka — asal mein order number se baari-baari, yahan tay."""
    from app.services import staff_scores

    real = staff_scores.subject_for
    monkeypatch.setattr(staff_scores, "subject_for", lambda order, w: subject if subject in ("wash", "overall") or "delivery" in w else real(order, w))


async def test_washing_rating_names_and_rewards_only_the_washer(client, two_shops, sent, monkeypatch) -> None:
    _ask_about(monkeypatch, "wash")
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    sent.clear()
    await _move(client, number)
    delivered = [m for m in sent if m["to"] == CUST_B and number in (m.get("text") or "")]
    assert delivered, "grahak ko delivery ka message"
    txt = delivered[-1]["text"]
    assert "Awash" in txt and "Adel" not in txt and "washing" in txt.lower()
    assert "reward" not in txt.lower() and "inaam" not in txt.lower(), "inaam band hai to vaada nahi"

    sent.clear()
    await _rate(two_shops["a"], "good")
    assert await _points(two_shops["a"]) == {"Awash": (5, "good", "washer")}
    reply = next(m["text"] for m in sent if m["to"] == CUST_B)
    assert "5 points" in reply and "Awash" in reply and "Adel" not in reply
    from app.services import background

    await background.drain()
    to_staff = {m["to"]: m["text"] for m in sent if m["to"] in (A_PHONE, A_DEL_PHONE)}
    assert ("Shabash" in to_staff[A_PHONE] or "Well done" in to_staff[A_PHONE]) and "washing" in to_staff[A_PHONE]
    assert A_DEL_PHONE not in to_staff

    # dobara dabaya — points dobara nahi
    await _rate(two_shops["a"], "good")
    assert await _points(two_shops["a"]) == {"Awash": (5, "good", "washer")}

    # staff app: mere points
    await _login(client, A_PHONE)
    me = (await client.get("/staff/api/points")).json()
    assert me["month_points"] == 5 and me["good"] == 1 and me["recent"][0]["order"] == number
    await _login(client, A_MGR_PHONE)
    team = {r["name"]: r for r in (await client.get("/staff/api/team")).json()}
    assert team["Awash"]["month_points"] == 5 and team["Adel"]["month_points"] == 0


async def test_delivery_rating_goes_to_the_delivery_person(client, two_shops, sent, monkeypatch) -> None:
    _ask_about(monkeypatch, "delivery")
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    sent.clear()
    await _move(client, number)
    txt = [m["text"] for m in sent if m["to"] == CUST_B and number in (m.get("text") or "")][-1]
    assert "Adel" in txt and "Awash" not in txt and "delivery" in txt.lower()
    await _rate(two_shops["a"], "good")
    assert await _points(two_shops["a"]) == {"Adel": (5, "good", "delivery")}


async def test_overall_rating_rewards_everyone_who_worked_on_it(client, two_shops, sent, monkeypatch) -> None:
    _ask_about(monkeypatch, "overall")
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    sent.clear()
    await _move(client, number)
    txt = [m["text"] for m in sent if m["to"] == CUST_B and number in (m.get("text") or "")][-1]
    assert "Awash" in txt and "Adel" in txt
    await _rate(two_shops["a"], "mid")
    assert await _points(two_shops["a"]) == {"Awash": (2, "mid", "washer"), "Adel": (2, "mid", "delivery")}


def test_subjects_take_turns_and_skip_work_nobody_did() -> None:
    from types import SimpleNamespace

    from app.services.staff_scores import subject_for

    both = {"washer": SimpleNamespace(id=1), "delivery": SimpleNamespace(id=2)}
    seen = [subject_for(SimpleNamespace(order_number=f"KK-20261021-{i:02d}"), both) for i in range(1, 91)]
    counts = {k: seen.count(k) for k in ("wash", "delivery", "overall")}
    assert all(n >= 15 for n in counts.values()), counts    # teeno baari-baari aate hain
    # dukaan se khud le gaya — delivery wala hai hi nahi: delivery ka sawaal nahi
    only_washer = {"washer": SimpleNamespace(id=1)}
    assert {subject_for(SimpleNamespace(order_number=f"KK-20261021-{i:02d}"), only_washer)
            for i in range(1, 91)} == {"wash"}


async def test_bad_rating_gives_no_points_but_a_respectful_note(client, two_shops, sent, monkeypatch) -> None:
    _ask_about(monkeypatch, "wash")
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    await _move(client, number)
    sent.clear()
    await _rate(two_shops["a"], "bad")
    assert await _points(two_shops["a"]) == {"Awash": (0, "bad", "washer")}
    note = next(m["text"] for m in sent if m["to"] == A_PHONE)
    assert "Sudhar chahiye" in note or "Needs work" in note
    assert "tankhwah" not in note.lower() and "salary" not in note.lower()


async def test_reward_promise_only_when_the_owner_turned_it_on(client, two_shops, sent, monkeypatch) -> None:
    _ask_about(monkeypatch, "wash")
    await _set(two_shops["a"], "staff_reward_enabled", True)
    await _set(two_shops["a"], "staff_reward_note", "Har 50 points par ₹200 bonus")
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    sent.clear()
    await _move(client, number)
    txt = [m["text"] for m in sent if m["to"] == CUST_B and number in (m.get("text") or "")][-1]
    assert "inaam" in txt.lower() or "reward" in txt.lower()
    sent.clear()
    await _rate(two_shops["a"], "good")
    assert any("₹200 bonus" in (m.get("text") or "") for m in sent if m["to"] == A_PHONE)


async def test_late_work_reminder_goes_once_to_the_person_responsible(client, two_shops, sent) -> None:
    from app.services import staff_scores, tenant_context

    late = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    very_late = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_B)
    async with async_session_factory() as db:
        for n, days in ((late, 1), (very_late, 3)):
            await db.execute(
                sqltext("UPDATE orders SET expected_delivery = :d WHERE order_number = :n AND tenant_id = :t"),
                {"d": IST_11.date() - timedelta(days=days), "n": n, "t": str(two_shops["a"])},
            )
        await db.commit()

    claimed = set()

    async def claim(key):
        if key in claimed:
            return False
        claimed.add(key)
        return True

    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        sent.clear()
        async with async_session_factory() as db:
            assert await staff_scores.run_work_nudges(db, IST_11, claim) == 1
        msg = next(m["text"] for m in sent if m["to"] == A_PHONE)
        assert late in msg and very_late in msg and "3 din late" in msg
        assert "aapke naam par" in msg or "in your name" in msg
        assert "tankhwah" not in msg.lower()
        # 2+ din late -> manager ko bhi
        assert any(very_late in (m.get("text") or "") and m["to"] != A_PHONE for m in sent)

        sent.clear()
        async with async_session_factory() as db:
            assert await staff_scores.run_work_nudges(db, IST_11, claim) == 0, "ek ghante mein ek hi baar"
            # nudge ghanta nahi
            assert await staff_scores.run_work_nudges(db, IST_11.replace(hour=13), claim) == 0
        assert not sent
    finally:
        tenant_context.current_tenant_id.reset(token)


def test_names_in_history_with_or_without_the_staff_prefix() -> None:
    from app.services.staff_scores import _clean_actor, first_name

    assert _clean_actor("staff:Ravi Kumar") == "Ravi Kumar"
    assert _clean_actor("Ravi") == "Ravi"
    assert first_name("Ravi Kumar") == "Ravi" and first_name(None) == ""


def test_every_rating_button_label_we_send_is_understood() -> None:
    """DELIVERED par jo button bhejte hain, unka text bhi pehchana jaye."""
    from app.routers.webhook import _match_rating

    for label, kind in (("⭐ Bahut achha", "good"), ("🙂 Theek tha", "mid"), ("😞 Sudhar chahiye", "bad"),
                        ("⭐ Excellent", "good"), ("🙂 It was okay", "mid"), ("😞 Needs work", "bad"),
                        ("rate_good", "good")):
        assert _match_rating(f"[button:{label}]") == kind, label
