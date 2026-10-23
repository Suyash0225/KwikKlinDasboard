"""Kam rate par liya kapda — bill par rate card ka daam aur bachat
(services/item_savings.py). Builder aur hisaab ko DB nahi chahiye.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services import item_savings, receipt
from app.services.app_settings import DEFAULTS


def _order(items, **kw):
    base = dict(
        order_number="KK-7", created_at=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc),
        expected_delivery=date(2026, 10, 4), total_amount=330, discount_amount=None,
        gst_amount=0, amount_paid=0, items=items,
    )
    base.update(kw)
    return SimpleNamespace(**base)


SAREE = {"type": "Saree", "service": "Dry Clean", "qty": 1, "rate": 230,
         "amount": 230, "unit": "pc", "list_rate": 250}
SHIRT = {"type": "Shirt", "service": "Dry Clean", "qty": 2, "rate": 50,
         "amount": 100, "unit": "pc", "list_rate": 50}


def _r(order):
    return receipt.build(order=order, customer_name="Pooja", customer_phone=None,
                         shop_name="Kwik Klin", settings=dict(DEFAULTS))


def test_line_saving_only_when_charged_below_the_rate_card() -> None:
    assert item_savings.line_saving(SAREE) == 20
    assert item_savings.line_saving(SHIRT) == 0
    assert item_savings.line_saving({**SAREE, "rate": 300, "amount": 300}) == 0
    assert item_savings.line_saving({k: v for k, v in SAREE.items() if k != "list_rate"}) == 0
    # qty ke saath: 3 saree ₹230 par, rate ₹250 -> ₹60
    assert item_savings.line_saving({**SAREE, "qty": 3, "amount": 690}) == 60


def test_item_key_matches_rate_card_rows() -> None:
    assert item_savings.item_key(SAREE) == ("dry clean", "saree")
    # KG line: type == service, rate card garment khaali
    assert item_savings.item_key({"type": "Wash & Fold (kg)", "service": "Wash & Fold (kg)"}) == ("wash & fold (kg)", "")


def test_bill_shows_rate_card_price_and_total_saving() -> None:
    r = _r(_order([SAREE, SHIRT]))
    saree, shirt = r["items"]
    assert saree["list_rate"] == 250 and saree["saved"] == 20
    assert shirt["list_rate"] is None and shirt["saved"] == 0
    assert r["saved"] == 20
    text = receipt.render(r)
    assert "(Rate ₹250, you saved ₹20)" in text
    assert "You saved: ₹20" in text


def test_total_saving_adds_bill_discount_to_line_savings() -> None:
    r = _r(_order([SAREE, SHIRT], total_amount=310, discount_amount=20))
    assert r["saved"] == 40


def test_plain_discount_bill_is_unchanged() -> None:
    """Sirf poore bill ka discount: purana 'Discount' row hi kaafi hai."""
    r = _r(_order([SHIRT], total_amount=90, discount_amount=10))
    assert r["saved"] == 0
    assert "You saved" not in receipt.render(r)


def test_whatsapp_total_carries_the_saving() -> None:
    assert item_savings.message_total(330, None, [SAREE, SHIRT]) == "330 (₹20 saved)"
    assert item_savings.message_total(310, 20, [SAREE, SHIRT]) == "310 (₹40 saved)"
    assert item_savings.message_total(90, 10, [SHIRT]) == "90 (₹10 discount)"
    assert item_savings.message_total(100, None, [SHIRT]) == "100"


def test_urgent_charge_line_is_never_a_saving() -> None:
    urgent_line = {"type": "Urgent charge", "service": "Urgent", "qty": 1, "rate": 50,
                   "amount": 50, "kind": "urgent_charge", "list_rate": 100}
    assert item_savings.line_saving(urgent_line) == 0


def test_without_list_rate_lets_a_repeat_request_match_the_saved_order() -> None:
    """Self-order ka dohra tap: save hue order mein list_rate hai, request mein nahi."""
    request = [{k: v for k, v in SAREE.items() if k != "list_rate"}]
    assert item_savings.without_list_rate([SAREE]) == request
    assert "list_rate" in SAREE, "original list badalni nahi chahiye"
