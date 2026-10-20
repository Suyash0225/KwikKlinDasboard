"""AI marketing copy is checked in code before it can reach customers."""

import app.services.marketing_agent as marketing_agent
from app.routers.agent_admin import _ensure_gmb_local_search_context, _offer_line
from app.services.copy_guard import copy_problems
from app.services.shop_identity import ShopIdentity

OFFER = "15% off on orders of ₹700 or more"


def test_copy_matching_the_offer_passes() -> None:
    copy = "✨ 15% off — ₹700 ya usse zyada ke order par. Reply karein! Sharma Laundry"
    assert copy_problems(copy, allowed_text=OFFER, must_include="Sharma Laundry") == []


def test_invented_discount_is_caught() -> None:
    problems = copy_problems("Aaj 25% off!", allowed_text=OFFER)
    assert any("25" in p for p in problems)


def test_invented_phone_and_link_are_caught() -> None:
    problems = copy_problems(
        "Call 98765 43210 ya visit www.cheap-laundry.in", allowed_text=OFFER,
    )
    assert any("phone" in p for p in problems)
    assert any("link" in p for p in problems)


def test_shops_own_number_is_allowed() -> None:
    assert copy_problems("Call +91 98765 43210", allowed_text="+919876543210") == []


def test_cut_off_copy_is_caught() -> None:
    problems = copy_problems("🍂 Winter care\n\n✨ 15% off", must_include="Sharma Laundry", min_chars=80)
    assert any("cut off" in p for p in problems)


async def test_draft_copy_falls_back_when_model_invents_a_discount(monkeypatch) -> None:
    async def fake_ask(**kw):
        assert "Sharma Laundry" in kw["system"] and "<<" not in kw["system"]
        return "🍂 Winter\n\nSirf aaj 40% off sab par!!! " * 3 + "Sharma Laundry – Fresh Clothes."

    monkeypatch.setattr(marketing_agent.llm_client, "ask", fake_ask)
    shop = ShopIdentity(name="Sharma Laundry", city="Lucknow")
    copy = await marketing_agent._draft_copy("lapsed", OFFER, shop=shop)
    assert "40%" not in copy
    assert OFFER in copy and "Sharma Laundry" in copy  # the safe template


async def test_draft_copy_keeps_a_clean_model_draft(monkeypatch) -> None:
    good = (
        "🍂 Sardiyon ki taiyari\n\nKambal aur jacket saaf karwane ka sahi samay.\n\n"
        "✨ 15% off — ₹700 or more ke order par\n\n📞 Reply karke pickup book karein.\n\n"
        "Sharma Laundry – Fresh Clothes. Better Care."
    )

    async def fake_ask(**kw):
        return good

    monkeypatch.setattr(marketing_agent.llm_client, "ask", fake_ask)
    shop = ShopIdentity(name="Sharma Laundry", city="Lucknow")
    assert await marketing_agent._draft_copy("lapsed", OFFER, shop=shop) == good


def test_offer_line_uses_only_clamped_numbers() -> None:
    line = _offer_line({"offer_type": "percent", "discount_value": 15, "min_order": 500, "validity_days": 7})
    assert line == "Get 15% off your next laundry order of ₹500 or more, valid for 7 days."
    # an AI message promising more than the cap is rejected against this line
    assert copy_problems("Get 25% off!", allowed_text=line)
    assert not copy_problems("Get 15% off above ₹500!", allowed_text=line)


def test_local_search_context_uses_the_tenants_city() -> None:
    body = _ensure_gmb_local_search_context("Fresh clothes this week.", city="Lucknow")
    assert "Lucknow" in body and "Varanasi" not in body
    # no city on file: never invent one
    assert "Varanasi" not in _ensure_gmb_local_search_context("Fresh clothes.", city="")
