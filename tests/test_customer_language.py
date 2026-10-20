"""Customer-facing copy follows the shop's chosen language, live."""

import pytest

from app.config import settings
from app.services import messages
from app.services.messages import get_message, set_customer_lang, status_label, customer_lang
from app.models import OrderStatus

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
def _restore_lang():
    before = customer_lang()
    yield
    set_customer_lang(before)


def test_customer_keys_follow_the_switch_staff_keys_stay_hinglish() -> None:
    set_customer_lang("en")
    assert get_message("ack_received").startswith("Hello!")
    set_customer_lang("hi")
    assert get_message("ack_received").startswith("Namaste!")
    # staff/owner copy is Hinglish whatever the customer setting is
    set_customer_lang("en")
    assert get_message("bill_cancelled") == messages.MESSAGES["bill_cancelled"]["hi"]


def test_unknown_language_falls_back_to_default() -> None:
    set_customer_lang("fr")
    assert customer_lang() == messages.CUSTOMER_LANG


def test_lead_messages_carry_the_shops_own_name() -> None:
    for lang in ("hi", "en"):
        set_customer_lang(lang)
        text = get_message("lead_day1", name="Neha", shop="Sharma Laundry")
        assert "Sharma Laundry" in text and "Kwik Klin" not in text


async def test_saving_the_setting_applies_immediately(client) -> None:
    r = await client.put("/admin/api/settings", headers=AUTH, json={"key": "customer_language", "value": "hi"})
    assert r.status_code == 200
    assert customer_lang() == "hi"
    formats = (await client.get("/admin/api/message-formats", headers=AUTH)).json()
    ack = next(f for f in formats if f["key"] == "ack_received")
    assert ack["default"].startswith("Namaste!"), "the editor shows the new language"
    bad = await client.put("/admin/api/settings", headers=AUTH, json={"key": "customer_language", "value": "xx"})
    assert bad.status_code == 400
    await client.put("/admin/api/settings", headers=AUTH, json={"key": "customer_language", "value": "en"})
