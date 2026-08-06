"""The agent must DO what it says it did.

This file exists because of a real incident: the owner said "100 ka petrol
expense me add kr do" and "Rahul Shah ko customer me save kr lo". The agent
replied "add kar diya hai" both times and wrote nothing — there was no tool
that could. These tests cover the tools that now exist and the check that
catches the claim if one ever slips through again.
"""

from decimal import Decimal

from sqlalchemy import delete, select

import app.services.agent_tools as at
import app.services.bill_agent as ba
from app.database import async_session_factory
from app.models import Customer, Expense

WRITE_PHONE = "+919999900077"


async def _clean() -> None:
    async with async_session_factory() as db:
        await db.execute(delete(Expense).where(Expense.description.ilike("%pytestkharcha%")))
        await db.execute(delete(Customer).where(Customer.phone == WRITE_PHONE))
        await db.commit()


# --------------------------- add_expense ---------------------------


async def test_add_expense_writes_a_real_row() -> None:
    await _clean()
    try:
        async with async_session_factory() as db:
            out = await at.run_tool(db, "add_expense", "100 | petrol pytestkharcha")
        assert "₹100" in out
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(Expense).where(Expense.description.ilike("%pytestkharcha%"))
                )
            ).scalars().all()
        assert len(rows) == 1, "the expense must actually be in the table"
        assert rows[0].amount == Decimal("100")
        assert rows[0].category == "Transport", "petrol belongs to Transport"
    finally:
        await _clean()


async def test_add_expense_categorises_from_the_words_used() -> None:
    await _clean()
    try:
        for note, cat in (
            ("surf powder pytestkharcha", "Detergent"),
            ("bijli ka bill pytestkharcha", "Electricity"),
            ("ravi ki salary pytestkharcha", "Salary"),
            ("machine repair pytestkharcha", "Maintenance"),
            ("chai pani pytestkharcha", "Other"),
        ):
            async with async_session_factory() as db:
                await at.run_tool(db, "add_expense", f"200 | {note}")
                got = (
                    await db.execute(
                        select(Expense.category).where(Expense.description == note)
                    )
                ).scalar_one()
            assert got == cat, f"{note!r} should file under {cat}, got {got}"
    finally:
        await _clean()


async def test_add_expense_refuses_when_there_is_no_amount() -> None:
    """'time 11 se 6 add kar do' once became an ₹11 expense. Never again."""
    await _clean()
    try:
        async with async_session_factory() as db:
            out = await at.run_tool(db, "add_expense", "11 se 6 | shop khulne ka time")
            n = (
                await db.execute(select(Expense).where(Expense.description.ilike("%khulne%")))
            ).scalars().all()
        assert "Format" in out
        assert not n, "a non-expense must never create a row"
    finally:
        await _clean()


async def test_add_expense_survives_rupee_symbols() -> None:
    await _clean()
    try:
        async with async_session_factory() as db:
            out = await at.run_tool(db, "add_expense", "Rs 250 | detergent pytestkharcha")
            row = (
                await db.execute(
                    select(Expense).where(Expense.description.ilike("%pytestkharcha%"))
                )
            ).scalar_one()
        assert row.amount == Decimal("250")
        assert "₹250" in out
    finally:
        await _clean()


# --------------------------- add_customer ---------------------------


async def test_add_customer_creates_the_row() -> None:
    await _clean()
    try:
        async with async_session_factory() as db:
            out = await at.run_tool(db, "add_customer", "9999900077 | Rahul Shah")
        assert "Rahul Shah" in out
        async with async_session_factory() as db:
            c = (
                await db.execute(select(Customer).where(Customer.phone == WRITE_PHONE))
            ).scalar_one()
        assert c.name == "Rahul Shah"
    finally:
        await _clean()


async def test_add_customer_names_someone_who_already_exists() -> None:
    """The number is usually already there — they messaged us first."""
    await _clean()
    try:
        async with async_session_factory() as db:
            db.add(Customer(phone=WRITE_PHONE))
            await db.commit()
        async with async_session_factory() as db:
            out = await at.run_tool(db, "add_customer", "Rahul Shah | 9999900077")
            c = (
                await db.execute(select(Customer).where(Customer.phone == WRITE_PHONE))
            ).scalar_one()
        assert c.name == "Rahul Shah", "an unnamed existing customer gets named"
        assert "save kar diya" in out
    finally:
        await _clean()


async def test_add_customer_rejects_junk_numbers() -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "add_customer", "Rahul Shah")
    assert "Number chahiye" in out


# --------------------------- set_shop_info ---------------------------


async def test_set_shop_timings_reaches_the_customer_bot() -> None:
    """'add kro office khulne ka time 11 se 6' — asked 03 Aug, ignored then."""
    from app.services import app_settings
    from app.services.ai_agent import _build_facts

    async with async_session_factory() as db:
        before = await app_settings.get(db, "shop_hours")
    try:
        async with async_session_factory() as db:
            out = await at.run_tool(
                db, "set_shop_info", "timing | subah 11 se shaam 6, Sunday band"
            )
        assert "save kar diya" in out
        async with async_session_factory() as db:
            db.add(Customer(phone=WRITE_PHONE, name="Timing Poochne Wala"))
            await db.commit()
            cust = (
                await db.execute(select(Customer).where(Customer.phone == WRITE_PHONE))
            ).scalar_one()
            facts = await _build_facts(db, cust)
        assert "Shop timings: subah 11 se shaam 6, Sunday band" in facts
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "shop_hours", before)
        await _clean()


async def test_set_shop_info_validates_turnaround() -> None:
    from app.services import app_settings

    async with async_session_factory() as db:
        before = await app_settings.get(db, "turnaround_days")
    try:
        async with async_session_factory() as db:
            bad = await at.run_tool(db, "set_shop_info", "turnaround | 400 din")
            good = await at.run_tool(db, "set_shop_info", "turnaround | 3 din")
            now = await app_settings.get(db, "turnaround_days")
        assert "1 se 30" in bad
        assert "3 din" in good and now == 3
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "turnaround_days", before)


async def test_set_shop_info_refuses_fields_it_must_not_touch() -> None:
    """UPI and GST stay on the Settings page — a misheard word costs money."""
    async with async_session_factory() as db:
        out = await at.run_tool(db, "set_shop_info", "upi | wrong@okaxis")
    assert "nahi kar sakta" in out
    assert "Settings page" in out


# --------------------- the "kar diya" safety net ---------------------


def test_claim_check_flags_action_words_with_no_write_tool() -> None:
    for lie in (
        "Theek hai, 100 rupaye petrol ka expense mein add kar diya hai.",
        "Rahul Shah ko customer database me save kar diya hai.",
        "Aapka note register me likh diya hai.",
        "Customer ki entry bana di hai.",
    ):
        assert ba._unbacked_claim(lie, []), lie


def test_claim_check_leaves_honest_answers_alone() -> None:
    for fine, used in (
        ("Kharcha likh diya: ₹100 — Transport.", ["add_expense"]),
        ("Naya customer save kar diya: Rahul Shah.", ["add_customer"]),
        ("Haan, Ajit ne 22:21 baje bataya ki Rahul ka pickup ho gaya hai.", []),
        ("Abhi 3 pending order hain, kul ₹410 baaki hai.", []),
        ("Aaj koi nayi inquiry nahi aayi hai.", []),
        ("Pankaj ka order deliver ho gaya hai.", []),
    ):
        assert not ba._unbacked_claim(fine, used), fine


async def test_loop_challenges_a_fake_completion(monkeypatch) -> None:
    """Claim without a write tool -> one more round, and the truth wins."""
    steps = [
        {"tool": "", "args": "", "answer": "Theek hai, 100 ka petrol expense me add kar diya hai."},
        {"tool": "", "args": "", "answer": "Maaf kijiye, ye main abhi nahi kar sakta."},
    ]
    seen: list[str] = []

    async def fake_ask_json(*, system, user_text, schema, model=None, max_tokens=0):
        seen.append(user_text)
        return steps.pop(0)

    monkeypatch.setattr(ba.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        ans = await ba._answer_manager_query(db, "100 ka petrol add kar do")

    assert ans == "Maaf kijiye, ye main abhi nahi kar sakta."
    assert "KUCH NAHI badla" in seen[-1], "the model must be told nothing was written"


async def test_loop_does_not_challenge_when_the_tool_really_ran(monkeypatch) -> None:
    steps = [
        {"tool": "add_expense", "args": "100 | petrol", "answer": ""},
        {"tool": "", "args": "", "answer": "Kharcha add kar diya hai, ₹100 Transport."},
    ]

    async def fake_ask_json(*, system, user_text, schema, model=None, max_tokens=0):
        return steps.pop(0)

    async def fake_tool(db, name, args):
        return "Kharcha likh diya: ₹100 — Transport"

    monkeypatch.setattr(ba.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(at, "run_tool", fake_tool)
    async with async_session_factory() as db:
        ans = await ba._answer_manager_query(db, "100 ka petrol add kar do")

    assert ans == "Kharcha add kar diya hai, ₹100 Transport."
    assert not steps, "no extra round when a write tool did the work"
