"""Rate card ka daam vs grahak se liya daam — har line par bachat.

Dukaan kabhi poore bill par discount deti hai, kabhi ek kapde ka rate hi kam
likh deti hai (Saree ₹250 ki jagah ₹230). Doosre case mein grahak samajhta
hai ₹230 hi rate hai. Isliye bill banate waqt har line ke saath us din ka
rate card ka daam `list_rate` mein rakh lete hain; bill page, print aur
WhatsApp message usi se "Rate ₹250 → ₹230" aur kul bachat dikhate hain.

`list_rate` bill ke saath jama hota hai — baad mein rate card badle to
purane bill ki bachat nahi badalti.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rate
from app.services import urgent


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def rate_key(service: str | None, garment: str | None) -> tuple[str, str]:
    return ((service or "").strip().lower(), (garment or "").strip().lower())


def item_key(it: dict) -> tuple[str, str]:
    """Bill line -> rate card ki (service, garment) jodi.

    Dashboard line ko `type = garment || service` bhejta hai; KG line ka
    garment khali hota hai, to type wahi service ka naam hota hai.
    """
    service = str(it.get("service") or "")
    garment = str(it.get("garment") or it.get("type") or "")
    if garment.strip().lower() == service.strip().lower():
        garment = ""
    return rate_key(service, garment)


def _eligible(it: dict) -> bool:
    return isinstance(it, dict) and it.get("kind") != urgent.KIND and it.get("rate") is not None


async def active_rates(db: AsyncSession) -> dict[tuple[str, str], Decimal]:
    rows = (
        await db.execute(
            select(Rate.service, Rate.garment, Rate.rate).where(Rate.is_active.is_(True))
        )
    ).all()
    return {rate_key(s, g): r for s, g, r in rows}


async def stamp_list_rates(
    db: AsyncSession, items: list, *, previous: list | None = None
) -> None:
    """Har line par `list_rate` likho (in place). Kabhi raise nahi karta.

    Bill edit par `previous` (purani lines) ka list_rate pehle — jis din
    bill bana tha us din ka rate wahi tha, aaj ka nahi.
    """
    try:
        carried = {
            item_key(it): it["list_rate"]
            for it in (previous or [])
            if isinstance(it, dict) and it.get("list_rate") is not None
        }
        card = None
        for it in items or []:
            if not _eligible(it) or it.get("list_rate") is not None:
                continue
            key = item_key(it)
            if key in carried:
                it["list_rate"] = carried[key]
                continue
            if card is None:
                card = await active_rates(db)
            if key in card:
                it["list_rate"] = float(card[key])
    except Exception:
        import structlog

        structlog.get_logger().exception("list_rate_stamp_failed")


def without_list_rate(items: list | None) -> list:
    """Bill lines bina list_rate ke — "wahi order hai?" milane ke liye.
    Save hue order mein list_rate hota hai, grahak ki nayi request mein nahi."""
    return [
        {k: v for k, v in it.items() if k != "list_rate"} if isinstance(it, dict) else it
        for it in items or []
    ]


def line_saving(it: dict) -> float:
    """Rate card se kitna kam liya is line par (0 agar barabar ya zyada)."""
    if not _eligible(it) or it.get("list_rate") is None:
        return 0.0
    qty = _num(it.get("qty", 1)) or 1.0
    charged = it.get("amount")
    charged = _num(charged) if charged is not None else _num(it.get("rate")) * qty
    saved = round(_num(it["list_rate"]) * qty - charged, 2)
    return saved if saved >= 0.5 else 0.0


def total_saving(items: list | None) -> float:
    return round(sum(line_saving(it) for it in items or []), 2)


def _money(v: float) -> str:
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}"


def message_total(total, discount, items: list | None) -> str:
    """WhatsApp ka {total}: '880' / '880 (₹20 discount)' / '880 (₹40 saved)'.

    {total} mein hi jodte hain — Meta ka approved template (fixed params)
    aur owner ka apna message format bina badle bachat dikhate hain.
    """
    out = f"{total}"
    line = total_saving(items)
    disc = _num(discount)
    if line:
        out += f" (₹{_money(line + disc)} saved)"
    elif disc:
        out += f" (₹{discount} discount)"
    return out
