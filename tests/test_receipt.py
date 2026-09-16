"""Bill ka text — WhatsApp aur chhota thermal printer (services/receipt.py).

DB nahi chahiye: builder order jaisa koi bhi object leta hai.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services import receipt
from app.services.app_settings import DEFAULTS


def _order(**kw):
    base = dict(
        order_number="KK-1", created_at=datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc),
        expected_delivery=date(2026, 9, 26), total_amount=1036, discount_amount=60,
        gst_amount=0, amount_paid=200,
        items=[
            {"type": "Wash & Fold (kg)", "service": "Wash & Fold (kg)", "qty": 15.6, "rate": 60,
             "amount": 936, "unit": "kg",
             "pieces": [{"type": "Shirt", "qty": 75}, {"type": "Pant", "qty": 45}]},
            {"type": "Saree", "service": "Dry Clean", "qty": 2, "rate": 80, "amount": 160, "unit": "pc"},
        ],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _r(order=None, **settings):
    return receipt.build(
        order=order or _order(), customer_name="Pooja Verma", customer_phone="+919999900011",
        shop_name="Sparkle Dry Clean", settings={**DEFAULTS, **settings},
    )


def test_whatsapp_text_reads_like_a_bill() -> None:
    t = receipt.render(_r())
    assert "Wash & Fold (kg)\n  15.6 kg x ₹60 = ₹936" in t
    assert "  Shirt 75\n  Pant 45\n  Total clothes: 120" in t
    assert "Saree (Dry Clean)\n  2 x ₹80 = ₹160" in t
    assert "Subtotal: ₹1,096" in t and "Discount: -₹60" in t and "Total: ₹1,036" in t
    assert "Due: ₹836" in t and "Date: 16 Sep 2026" in t and "Delivery: 26 Sep 2026" in t


def test_printer_text_fits_the_paper_and_is_plain_ascii() -> None:
    r = _r(shop_address="Shop 12, Lanka Road, Varanasi — near the big temple gate")
    for width in (32, 48):
        pt = receipt.render(r, width=width)
        assert pt.isascii(), "thermal printer ₹/emoji nahi chhapta"
        assert max(len(ln) for ln in pt.splitlines()) <= width
        assert "Rs.1,036" in pt
    # daam right side mein seedhe
    assert " 15.6 kg x Rs.60          Rs.936" in receipt.render(r, width=32)


def test_terms_are_owner_editable_and_kg_bills_get_the_count_rule() -> None:
    t = receipt.render(_r())
    assert "Terms & conditions:" in t and "10x the service charge" in t
    assert receipt.KG_TERM in t
    no_kg = _order(items=[{"type": "Saree", "service": "Dry Clean", "qty": 1, "rate": 80, "amount": 80}])
    assert receipt.KG_TERM not in receipt.render(_r(no_kg))
    # owner ne apni sharten likhin
    own = receipt.render(_r(no_kg, invoice_terms="- Cash only\n\n• No guarantee on zari work"))
    assert "1. Cash only" in own and "2. No guarantee on zari work" in own
    # khali = koi shart nahi (kg wali bhi nahi — owner ne sab hata di)
    assert "Terms" not in receipt.render(_r(invoice_terms=""))
