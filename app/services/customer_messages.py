"""Bill par haath se bheje jaane wale messages — thank you, payment mila, review.

Text server par banta hai (messages.py ka copy, owner ka override bhi),
browser mein nahi — warna har dukaan ke message mein kisi aur ka naam
chipak jaata hai. Bhejna browser karta hai: dukaan ka WhatsApp API ho to
wahan se, warna wa.me link se apne phone ka WhatsApp.

Review link choti aur dukaan ki apni hai: {SITE_URL ya public_base_url}/r/{slug}. Wo
Google ke review box par redirect karti hai (main.py). Bahar ki shortener
service nahi: link par dukaan ka domain dikhta hai, kabhi expire nahi hoti,
aur click ginti hamare log mein.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, Payment
from app.services import app_settings
from app.services.messages import get_message
from app.services.whatsapp import Button

KINDS = ("payment_thanks", "service_thanks", "review_request", "delivery_update")


class MessageError(ValueError):
    pass


def rupees(x) -> str:
    d = Decimal(str(x or 0)).quantize(Decimal("0.01"))
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"


def balance_line(order: Order) -> str:
    """Payment message ki aakhri line — auto (API) aur phone wala, dono yahi."""
    due = Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))
    if order.total_amount is not None and due > Decimal("0.009"):
        return f"Balance due: ₹{rupees(due)}"
    return "Your bill is fully paid ✅"


async def review_links(db: AsyncSession) -> list[str]:
    """Dukaan ki Google review links (ek ya do listing)."""
    out = []
    for key in ("google_review_link", "google_review_link_2"):
        v = (await app_settings.get(db, key) or "").strip()
        if v.startswith(("http://", "https://")):
            out.append(v)
    return out


async def short_review_link(db: AsyncSession, tenant) -> str:
    """Grahak ko jaane wali review link — choti wali agar public URL pata ho.

    Public URL na ho (laptop par dev) to seedhi Google link, taaki message
    kabhi bina link ke na jaye.
    """
    links = await review_links(db)
    if not links:
        return ""
    from app.services.google_auth import public_base

    base = await public_base(db)
    if base and tenant is not None and getattr(tenant, "slug", None):
        return f"{base}/r/{tenant.slug}"
    return links[0]


async def payment_buttons(db: AsyncSession, order: Order) -> list[Button]:
    """Customer payment actions — deliberately simple two-button UX."""
    from app.services import bill_link

    url = await bill_link.url_for(db, order)
    # The actual payment link stays in the message body. WAHA renders these
    # reply buttons as an interactive list; tapping Pay Now can resend/open
    # the same signed bill link through the inbound handler.
    return [
        Button(f"payment:{order.order_number}:pay", "💰 Pay Now"),
        Button(f"payment:{order.order_number}:paid", "✅ Already Paid"),
    ]


async def compose(db: AsyncSession, *, kind: str, order: Order, customer, tenant) -> str:
    if kind not in KINDS:
        raise MessageError("Unknown message")
    shop = ((tenant.shop_name if tenant else "") or "").strip() or "Laundry"
    name = (customer.name or "").strip() or "there"   # "Hello there" — messages ab English mein
    from app.services import bill_link

    fmt = {"name": name, "order_number": order.order_number, "shop": shop,
           "bill_line": bill_link.message_line(await bill_link.url_for(db, order))}

    if kind == "payment_thanks":
        last = (
            await db.execute(
                select(Payment).where(Payment.order_id == order.id)
                .order_by(Payment.received_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if last is None:
            raise MessageError("No payment recorded on this bill yet")
        fmt["amount"] = rupees(last.amount)
        fmt["balance_line"] = balance_line(order)
    elif kind == "delivery_update":
        # Poora de diya -> "sab X kapde deliver"; kuch -> "X diye, Y baaki".
        # Dono mein web bill ka link (wahi page jo bill banate waqt gaya tha),
        # jahan har kapde par gaya/baaki dikhta hai.
        from app.models import OrderStatus
        from app.services import delivery

        c = delivery.counts(order)
        # Status se seedha "Delivered" kiya ho (kapde chune bina) to har line
        # par delivered ka nishaan nahi hota — order ka status hi sach hai.
        if order.status is OrderStatus.DELIVERED:
            c = {"total": c["total"], "delivered": c["total"], "pending": 0}
        if c["delivered"] == 0:
            raise MessageError("Nothing delivered on this bill yet — mark the delivery first")
        if c["pending"] == 0:
            fmt["count"] = str(c["total"])
            kind = "delivery_update_full"
        else:
            fmt["given"], fmt["pending"] = str(c["delivered"]), str(c["pending"])
            kind = "partial_delivery"
    elif kind == "review_request":
        link = await short_review_link(db, tenant)
        if not link:
            raise MessageError("Add your Google review link in Settings → Business Profile first")
        fmt["review_link"] = link

    try:
        return get_message(kind, **fmt)
    except (KeyError, IndexError, ValueError):
        # Owner ke format mein koi anjaan {placeholder} — default copy bhejo,
        # message rukna nahi chahiye
        from app.services.messages import MESSAGES, lang_for

        return MESSAGES[kind][lang_for(kind)].format(**fmt)
