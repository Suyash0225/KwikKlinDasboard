"""Bill ka text — WhatsApp ke liye aur chhote thermal printer ke liye.

Pehle do jagah banta tha: dashboard apne JS mein, staff panel server par.
Dono dheere-dheere alag ho gaye (ek "16 Sept", doosra "16 Sep"; ek mein
chhoot, doosre mein nahi). Ab ek hi builder, do render:

  render(r)            -> WhatsApp / copy. ₹, koi padding nahi (WhatsApp
                          ka font barabar chaudai ka nahi hota).
  render(r, width=32)  -> 58mm printer (32 akshar), 48 = 80mm. Sirf ASCII:
                          saste Bluetooth thermal printer ₹, emoji aur
                          Devanagari nahi chhapte — kachra nikalta hai.
                          Isliye "Rs." aur daam right side mein seedhe.
"""

import textwrap
import unicodedata
from datetime import timedelta, timezone

from app.services import urgent

IST = timezone(timedelta(hours=5, minutes=30))

# Printer ki chaudai (mm) -> ek line mein kitne akshar (Font A)
PAPER_CHARS = {58: 32, 80: 48}

# Wazan wali line par ye shart apne aap judti hai — owner ki list se alag,
# kyunki ye sirf tab sach hai jab bill mein kg ki line ho.
KG_TERM = "Kg services: the clothes count at drop-off is final."


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _qty(v) -> str:
    f = _num(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def terms_list(raw) -> list[str]:
    """Settings ka text (ek line = ek shart) -> list. Khali = koi shart nahi."""
    if isinstance(raw, list):
        lines = raw
    else:
        lines = str(raw or "").splitlines()
    out = []
    for ln in lines:
        t = str(ln).strip().lstrip("-•*").strip()
        if t:
            out.append(t[:200])
    return out[:12]


def build(
    *,
    order,
    customer_name: str | None,
    customer_phone: str | None,
    shop_name: str,
    settings: dict,
    prev_due: float = 0.0,
    prev_bills: int = 0,
) -> dict:
    """Order + settings -> bill ka dhaancha (render se alag, taaki test ho sake)."""
    from app.services import delivery as _dl

    # Kitne kapde de diye, kitne abhi dukaan par — usi hisaab se jo panel
    # aur dashboard dikhate hain (delivery.lines), alag ginti nahi.
    by_line = {row["line"]: row for row in _dl.lines(order)}
    items = []
    has_kg = False
    urgent_charge = 0.0
    for idx, it in enumerate(order.items or []):
        if it.get("kind") == urgent.KIND:
            # kapda nahi, jaldi ki fees — totals mein apni line
            urgent_charge += _num(it.get("amount"))
            continue
        unit = (it.get("unit") or "").lower()
        kg = unit == "kg" or "(kg)" in str(it.get("service") or it.get("type") or "").lower()
        has_kg = has_kg or kg
        name = it.get("type") or it.get("garment") or it.get("service") or "?"
        svc = it.get("service") or ""
        title = name if (kg or not svc or svc.strip().lower() == str(name).strip().lower()) else f"{name} ({svc})"
        rate = it.get("rate")
        amount = it.get("amount")
        if amount is None and rate is not None:
            amount = _num(rate) * _num(it.get("qty", 1))
        pieces = [
            (str(p.get("type")), int(_num(p.get("qty"))))
            for p in (it.get("pieces") or [])
            if isinstance(p, dict) and p.get("type")
        ]
        dl = by_line.get(idx) or {}
        items.append({
            "title": str(title),
            "qty": _qty(it.get("qty", 1)),
            "kg": kg,
            "rate": None if rate is None else _num(rate),
            "amount": None if amount is None else _num(amount),
            "pieces": pieces,
            # delivery ki ginti (partial delivery ke bill ke liye)
            "count": int(dl.get("qty") or 0),
            "delivered": int(dl.get("delivered") or 0),
            "pending": int(dl.get("pending") or 0),
            "pieces_pending": [(str(p["type"]), int(p["pending"])) for p in (dl.get("pieces") or []) if p.get("pending")],
        })
    clothes = _dl.counts(order)
    partial = 0 < clothes["delivered"] < clothes["total"]
    still = []          # jo abhi dukaan par hai: (naam, kitne)
    if partial:
        for it in items:
            if it["pieces_pending"]:
                still.extend(it["pieces_pending"])
            elif it["pending"]:
                still.append((it["title"], it["pending"]))

    total = _num(order.total_amount)
    disc = _num(order.discount_amount)
    gst = _num(order.gst_amount)
    paid = _num(order.amount_paid)
    terms = terms_list(settings.get("invoice_terms"))
    if has_kg and terms:
        terms.append(KG_TERM)
    created = order.created_at.astimezone(IST) if order.created_at else None
    return {
        "shop": (shop_name or "Kwik Klin").strip(),
        "address": str(settings.get("shop_address") or "").strip(),
        "phone": str(settings.get("shop_contact_phone") or "").strip(),
        "gstin": str(settings.get("shop_gstin") or "").strip(),
        "number": order.order_number,
        "date": created.strftime("%d %b %Y") if created else "",
        "customer": (customer_name or "").strip() or (customer_phone or ""),
        "delivery": order.expected_delivery.strftime("%d %b %Y") if order.expected_delivery else "",
        "items": items,
        "clothes": clothes,
        "partial": partial,
        "still": still,
        "urgent": getattr(order, "priority", "normal") == "urgent",
        "urgent_charge": urgent_charge,
        "has_total": order.total_amount is not None,
        "subtotal": total + disc - gst - urgent_charge,
        "discount": disc,
        "gst": gst,
        "total": total,
        "paid": paid,
        "due": max(total - paid, 0.0),
        "prev_due": prev_due,
        "prev_bills": prev_bills,
        "upi": str(settings.get("upi_vpa") or "").strip(),
        "upi_payee": str(settings.get("upi_payee") or "").strip(),
        "terms": terms,
        "footer": str(settings.get("invoice_footer") or "Thank you!").strip(),
    }


def _ascii(s: str) -> str:
    """Printer-safe: ₹ -> Rs., baaki non-ASCII (emoji, Devanagari) hata do."""
    s = str(s).replace("₹", "Rs.").replace("—", "-").replace("–", "-").replace("×", "x")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if 32 <= ord(ch) < 127).strip()


def render(r: dict, width: int | None = None, *, terms: bool = True) -> str:
    """terms=False: WhatsApp wala chhota text — sharten web bill par (details)
    aur kagaz par rehti hain; message mein wo 6-8 line bahut lambi ho jaati thi."""
    printing = width is not None
    cur = "Rs." if printing else "₹"

    def money(v: float) -> str:
        return f"{cur}{v:,.0f}" if float(v).is_integer() else f"{cur}{v:,.2f}"

    w = width or 30
    rule = "-" * w
    out: list[str] = []

    def text(s: str, indent: int = 0, hang: int = 0) -> None:
        """hang: lipti hui line kitna andar — "1. " ke neeche se shuru ho."""
        s = _ascii(s) if printing else str(s)
        if not s:
            return
        if not printing:
            out.append(" " * indent + s)
            return
        out.extend(textwrap.wrap(s, w, initial_indent=" " * indent,
                                 subsequent_indent=" " * (indent + hang)) or [""])

    def center(s: str) -> None:
        if not printing:
            text(s)
            return
        for ln in textwrap.wrap(_ascii(s), w):
            out.append(ln.center(w).rstrip())

    def row(left: str, right: str, indent: int = 0) -> None:
        """Baayein naam, daayein rakam — printer par seedhe khaane mein."""
        if not printing:
            out.append(" " * indent + f"{left}: {right}" if right else " " * indent + left)
            return
        left, right = " " * indent + _ascii(left), _ascii(right)
        if len(left) + 1 + len(right) <= w:
            out.append(left + " " * (w - len(left) - len(right)) + right)
        else:
            out.extend(textwrap.wrap(left, w, subsequent_indent=" " * (indent + 1)))
            out.append(right.rjust(w))

    center(r["shop"])
    if r["address"]:
        center(r["address"])
    if r["phone"]:
        center(f"Ph: {r['phone']}")
    if r["gstin"]:
        center(f"GSTIN: {r['gstin']}")
    out.append(rule)
    row("Bill", r["number"])
    if r["date"]:
        row("Date", r["date"])
    if r["customer"]:
        row("Customer", r["customer"])
    if r["delivery"]:
        row("Delivery", r["delivery"])
    if r.get("urgent"):
        row("Priority", "URGENT" if printing else "⚡ URGENT")
    if r.get("partial"):
        c = r["clothes"]
        row("Delivered", f"{c['delivered']} of {c['total']} clothes")
    out.append(rule)

    for it in r["items"]:
        text(it["title"])
        unit = " kg" if it["kg"] else ""
        detail = f"{it['qty']}{unit}"
        if it["rate"] is not None:
            detail += f" x {money(it['rate'])}"
        amount = money(it["amount"]) if it["amount"] is not None else ""
        if printing:
            row(detail, amount, indent=1)
        else:
            out.append(f"  {detail}" + (f" = {amount}" if amount else ""))
        if it["pieces"]:
            count = sum(n for _, n in it["pieces"])
            if printing:
                for name, n in it["pieces"]:
                    row(name, str(n), indent=2)
                row("Total clothes", str(count), indent=2)
            else:
                out.extend(f"  {name} {n}" for name, n in it["pieces"])
                out.append(f"  Total clothes: {count}")
        # Partial delivery: har line par saaf likha ho ki ye gaya ya abhi yahin hai
        if r.get("partial") and it.get("count"):
            if it["pending"] == 0:
                note = "delivered"
            elif it["delivered"] == 0:
                note = "still with us"
            else:
                note = f"{it['delivered']} of {it['count']} delivered, {it['pending']} pending"
            text(f"({note})", indent=2)
    out.append(rule)
    if r.get("partial") and r.get("still"):
        n = sum(k for _, k in r["still"])
        text(f"Still with us ({n}):")
        for name, k in r["still"]:
            row(name, str(k), indent=2)
        out.append(rule)

    if r["has_total"]:
        if r["discount"] or r["gst"] or r.get("urgent_charge"):
            row("Subtotal", money(r["subtotal"]))
            if r["discount"]:
                row("Discount", "-" + money(r["discount"]))
            if r.get("urgent_charge"):
                row("Urgent charge", "+" + money(r["urgent_charge"]))
            if r["gst"]:
                row("GST", "+" + money(r["gst"]))
        row("Total", money(r["total"]))
        row("Paid", money(r["paid"]))
        # Chukta bill par "Due: ₹0" aur UPI ID grahak ko phir se paisa maangne
        # jaisa lagta hai — isliye saaf "Paid in full", aur UPI line hi nahi.
        row("Due", money(r["due"]) if r["due"] > 0 else ("Nil - PAID IN FULL" if printing else "Nil ✅ Paid in full"))
    else:
        row("Total", "-")
    if r["prev_due"] > 0:
        out.append(rule)
        bills = "bill" if r["prev_bills"] == 1 else "bills"
        row(f"Previous due ({r['prev_bills']} {bills})", money(r["prev_due"]))
        row("TOTAL TO PAY", money(r["due"] + r["prev_due"]))
    if r["upi"] and (r["due"] > 0 or r["prev_due"] > 0 or not r["has_total"]):
        out.append(rule)
        text(f"Pay via UPI: {r['upi']}" + (f" ({r['upi_payee']})" if r["upi_payee"] else ""))
    if r["terms"] and terms:
        out.append(rule)
        text("Terms & conditions:")
        for n, t in enumerate(r["terms"], 1):
            text(f"{n}. {t}", hang=len(f"{n}. "))
    out.append(rule)
    center(r["footer"])
    return "\n".join(out)


def payload(order, cust, tenant, settings: dict, prev_due: float, prev_bills: int) -> dict:
    """WhatsApp text + printer text — staff panel aur dashboard dono ka ek jawab."""
    r = build(
        order=order, customer_name=cust.name, customer_phone=cust.phone,
        shop_name=(tenant.shop_name if tenant and tenant.shop_name else "Kwik Klin"),
        settings=settings, prev_due=prev_due, prev_bills=prev_bills,
    )
    try:
        mm = int(settings.get("receipt_paper_mm") or 58)
    except (TypeError, ValueError):
        mm = 58
    mm = mm if mm in PAPER_CHARS else 58
    from app.services import bill_link

    # WhatsApp wale text mein web bill ka link (dekho + GPay/PhonePe se pay);
    # printer wale kagaz par nahi. Public domain pata na ho to link hi nahi.
    url = bill_link.url(order, settings)
    settled = r["has_total"] and r["due"] <= 0 and r["prev_due"] <= 0
    link_line = "🧾 View your bill online:" if settled else "🧾 View bill & pay online:"
    text = render(r, terms=False) + (f"\n\n{link_line}\n{url}" if url else "")
    return {"text": text, "print_text": render(r, width=PAPER_CHARS[mm]), "paper_mm": mm, "bill_url": url}
