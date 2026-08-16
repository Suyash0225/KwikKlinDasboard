"""Live updates — jo abhi hua wo abhi dikhe, bina refresh ke.

Ye us shikayat ka test hai jahan Ajit ne phone se bill banaya aur owner ke
khule hue dashboard par wo nazar hi nahi aaya. Data hamesha theek tha; page
ne dobara poocha hi nahi tha.

Sabse zaroori baat jo yahan pakdi jaati hai: ek dukaan ki halchal doosri
dukaan ke connection par KABHI nahi jaani chahiye.
"""

import asyncio
import json
import uuid

from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.models import Staff, StaffRole
from app.services import events


async def _collect(tenant, *, want: int, timeout: float = 2.0) -> list[dict]:
    """Ek subscriber banao aur `want` asli message tak sun-ne ki koshish karo."""
    out: list[dict] = []
    gen = events.stream(tenant, lambda: asyncio.sleep(0, result=False))

    async def _read():
        async for chunk in gen:
            if chunk.startswith("data: "):
                body = chunk[6:].strip()
                if body and body != "{}":
                    out.append(json.loads(body))
                    if len(out) >= want:
                        return

    try:
        await asyncio.wait_for(_read(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await gen.aclose()
    return out


async def test_one_shops_news_never_reaches_another(client) -> None:
    """Sabse zaroori pehra: ishara sirf apni dukaan ke connection par.

    Live update ka poora faayda tab hi hai jab wo ek deewar ke saath aaye —
    warna ye har dukaan ko dusri ki halchal dikhane wala pipe ban jaata.
    """
    a, b = uuid.uuid4(), uuid.uuid4()

    async def listen(t):
        return await _collect(t, want=1, timeout=1.0)

    task_a = asyncio.create_task(listen(a))
    task_b = asyncio.create_task(listen(b))
    await asyncio.sleep(0.15)          # dono subscribe ho jayen

    events.publish(a, "order", number="KK-1", action="created")
    got_a, got_b = await task_a, await task_b

    assert got_a and got_a[0]["number"] == "KK-1"
    assert got_b == [], "doosri dukaan ko iski khabar nahi milni chahiye"


async def test_nobody_listening_is_not_an_error() -> None:
    """Koi screen khuli hi na ho to publish chup-chaap 0 lautaye.

    Ye maayne rakhta hai: publish har order, har task ke raste mein baitha
    hai. Wahan se exception aana matlab live-update ki wajah se asli kaam
    girna — jo kabhi nahi hona chahiye.
    """
    assert events.publish(uuid.uuid4(), "task", code="T-9", action="done") == 0
    assert events.publish(None, "task", code="T-9", action="done") == 0


async def test_a_slow_phone_never_blocks_the_shop() -> None:
    """Queue bhar jaye to purana ishara girta hai — server rukta nahi.

    Kisi ka phone tunnel mein chala gaya, uski queue bhar gayi. Us ek phone
    ki wajah se baaki dukaan ka kaam ruk jaana sabse bura natija hota.
    """
    t = uuid.uuid4()
    q: asyncio.Queue = asyncio.Queue(maxsize=events.QUEUE_DEPTH)
    events._subscribers.setdefault(str(t), set()).add(q)
    try:
        for i in range(events.QUEUE_DEPTH + 20):
            events.publish(t, "task", code=f"T-{i}", action="done")
        assert q.qsize() <= events.QUEUE_DEPTH
        # sabse naya ishara zinda hai — wahi sabse kaam ka hai
        newest = json.loads(q._queue[-1])
        assert newest["code"] == f"T-{events.QUEUE_DEPTH + 19}"
    finally:
        events._subscribers.pop(str(t), None)


async def test_a_closed_screen_leaves_nothing_behind() -> None:
    """Stream khatam = subscriber register se saaf.

    Bina iske har reconnect ek mari hui queue chhod jaata aur memory
    chupchaap badhti rehti — wo dikkat mahinon baad, bhare hue din mein
    dikhti hai.
    """
    t = uuid.uuid4()
    before = events.open_connections(t)
    gen = events.stream(t, lambda: asyncio.sleep(0, result=False))
    await gen.__anext__()                       # "ready" — ab jud gaya
    assert events.open_connections(t) == before + 1
    await gen.aclose()
    assert events.open_connections(t) == before


async def test_the_heartbeat_is_something_the_browser_can_actually_hear(monkeypatch) -> None:
    """Dhadkan ka ek NAAM hai — `: ping` comment nahi.

    Comment line browser ke JS tak pahunchti hi nahi. Uske bina page ke paas
    ye jaanne ka koi tarika nahi tha ki stream sach mein guzar rahi hai ya
    beech mein kisi proxy ne use chup kara diya hai — aur chup padi stream
    khule hue dashboard ko chupchaap purana kar deti hai.
    """
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.05)
    gen = events.stream(uuid.uuid4(), lambda: asyncio.sleep(0, result=False))
    chunks: list[str] = []

    async def _read():
        async for chunk in gen:
            chunks.append(chunk)
            if len(chunks) >= 2:
                return

    try:
        await asyncio.wait_for(_read(), timeout=2.0)
    finally:
        await gen.aclose()

    assert chunks[0].startswith("event: ready")
    assert any(c.startswith("event: ping") for c in chunks), chunks


# --- khuli hui screen kabhi chupchaap purani na rahe -------------------------


def _asset(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "app" / "static" / name).read_text(
        encoding="utf-8"
    )


async def test_a_dead_stream_always_falls_back_to_asking() -> None:
    """Stream mar jaye to page KHUD poochhta rahe — hamesha.

    Ye us shikayat ka test hai jahan phone se bana bill owner ke khule hue
    dashboard par kabhi aaya hi nahi. Fallback ek ginti ke peeche tha
    ("teen baar error aaye tab polling"), par EventSource ka niyam ye hai ki
    401/403 par browser connection band karke DOBARA JUDTA HI NAHI — yani
    error sirf EK baar aata hai. Ginti teen tak pahunchti hi nahi thi:
    na stream chalti thi, na polling. Dashboard hamesha ke liye purana.

    Aur ye rozmarra ki baat thi — tunnel ka URL badalte hi session cookie
    chali jaati hai, page purani admin key par chalta rehta hai, aur
    EventSource header bhej hi nahi sakta.
    """
    for name in ("app.js", "staff.js"):
        js = _asset(name)
        assert "LIVE_FAILS" not in js, (
            f"{name}: fallback dobara ginti ke peeche chala gaya — 401 par "
            "wo ginti kabhi poori nahi hoti"
        )
        assert "EventSource.CLOSED" in js, (
            f"{name}: browser haar maan le (CLOSED) to turant poochhna shuru "
            "hona chahiye"
        )
        assert 'addEventListener("ping"' in js, (
            f"{name}: dhadkan sunna zaroori hai — wahi saboot hai ki stream "
            "sach mein zinda hai"
        )


async def test_a_signal_hiccup_never_shows_the_login_screen() -> None:
    """Network gir jaye to aadmi ko bahar nahi phenka jaata.

    Dono app par yahi shikayat thi: phone par refresh karte hi login page
    ek pal ko jhalak kar chala jaata tha. Wajah dono jagah ek hi soch thi —
    "jawab nahi aaya" ko "logged in nahi ho" maan lena.
    """
    app_js = _asset("app.js")
    assert "serverAnswered" in app_js, (
        "app.js: server ke jawab aur network ki hichki mein farak hona chahiye"
    )

    html = _asset("staff.html")
    assert '<section id="boot"' in html, "staff.html: boot screen chahiye"
    assert '<section id="login" class="login" hidden>' in html, (
        "staff.html: faisla aane se pehle login screen nahi dikhni chahiye"
    )


async def test_closing_a_task_tells_the_open_screens(client, sent) -> None:
    """Asli raasta: task band hua -> dashboard ko turant ishara.

    Yahi wo update hai jiske liye owner baar-baar page refresh karta tha.
    """
    from app.services import tasks as task_service
    from app.services import tenant_context

    tid = await tenant_context.get_home_tenant_id()
    sid = uuid.uuid4()
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active)"
                " VALUES (:i, :t, :p, 'Live Test', 'WASHER', true)"
            ),
            {"i": str(sid), "t": str(tid), "p": "+919999900215"},
        )
        await db.commit()

    listening = asyncio.create_task(_collect(tid, want=1, timeout=2.0))
    await asyncio.sleep(0.15)

    token = tenant_context.current_tenant_id.set(tid)
    try:
        async with async_session_factory() as db:
            staff = await db.get(Staff, sid)
            assert staff.role is StaffRole.WASHER
            t = await task_service.create_task(
                db, title="Live update ka test", staff=staff, notify=False,
            )
            await task_service.complete_task(db, t, by="test")
            code = t.code
    finally:
        tenant_context.current_tenant_id.reset(token)

    got = await listening
    assert got, "task ke badalne par ishara jaana chahiye"
    assert any(g.get("code") == code for g in got)

    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM tasks WHERE assigned_staff_id = :i"), {"i": str(sid)})
        await db.execute(sqltext("DELETE FROM staff WHERE id = :i"), {"i": str(sid)})
        await db.commit()


async def test_the_stream_is_shut_to_strangers(client) -> None:
    """Bina login ke live stream nahi khulta — na staff ka, na owner ka."""
    r = await client.get("/staff/api/events")
    assert r.status_code == 401


# --- badi list ka ek page (bill history) ------------------------------------


async def test_bills_come_one_page_at_a_time_with_a_real_total(client) -> None:
    """Bill history ab server se ek page aati hai, poori list nahi.

    Pehle page sabse naye 200 bill utaar kar browser mein chhaanta tha.
    Ek dukaan ke shuruati mahinon tak wo chalta hai; uske baad 201-wa bill
    kisi bhi tarah nahi milta — na search se, na page badalne se. Isliye
    ab `limit`, `offset` aur asli `total` sab DB se aate hain.
    """
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    r = await client.get("/admin/api/bills?limit=2", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "total" in body
    assert len(body["items"]) <= 2
    assert body["total"] >= len(body["items"])
    # total POORI ginti hai, is page ki nahi — pager isi par tika hai
    assert isinstance(body["total"], int)


async def test_bill_search_reaches_past_the_first_page(client) -> None:
    """Search DB tak jaata hai, isliye purana bill bhi utni aasani se milta hai."""
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    everything = (await client.get("/admin/api/bills?limit=100", headers=H)).json()
    if not everything["items"]:
        return                      # khaali dukaan — dhoondhne ko kuch nahi
    target = everything["items"][-1]      # sabse PURANA jo mila
    hit = (
        await client.get(
            f"/admin/api/bills?q={target['order_number']}", headers=H
        )
    ).json()
    assert any(i["order_number"] == target["order_number"] for i in hit["items"])


async def test_a_bad_filter_is_refused_not_ignored(client) -> None:
    """Galat status/tareekh chup-chaap poori list nahi lauta sakti.

    Chup-chaap ignore karna yahan sabse khatarnak jawab hai: owner "aaj ke
    bill" maangta hai aur use saare bill dikh jaate hain — bina kisi
    ishare ke ki filter laga hi nahi.
    """
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    assert (await client.get("/admin/api/bills?status=NOPE", headers=H)).status_code == 400
    assert (await client.get("/admin/api/bills?date_from=kal", headers=H)).status_code == 400
