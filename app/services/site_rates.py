"""Website (/laundry) ka rate list — CRM ke rate card se, sirf HOME dukaan ka.

Kyun explicitly home tenant: /laundry par middleware tenant cookie se chunta
hai. Kisi DOOSRI dukaan ka logged-in owner ye page khole to bina is pehre ke
use APNE rates dikhte — Kwik Klin ki website par kisi aur ka daam. Isliye
query hamesha `as_tenant(home)` ke andar, chahe dekhne wala koi bhi ho.

Dashboard mein rate badlo -> ~1 minute mein website par (chhota cache, taaki
har page load DB na maare). DB na mile to purana cache; wo bhi na ho to khali
card — page tootta nahi, "Ask on WhatsApp" dikhta hai.

Prices HTML mein server se hi jaate hain (JS se nahi) taaki Google padh sake.
"""

import html
import json
import re
import time
from decimal import Decimal

import structlog

log = structlog.get_logger()

_TTL = 60
_cache: dict = {"at": 0.0, "rows": None}

# Website par tab ka kram. Dashboard ke service naam dukaan ki bhasha mein hain
# ("Sirf Iron", "Wash & Fold (kg)") — yahan unhe grahak ke naam milte hain.
_TABS = [
    ("dry", "Dry Cleaning", "Dry cleaning prices in Varanasi"),
    ("wash", "Wash & Iron", "Wash & iron prices"),
    ("kg", "By Weight", "Laundry by weight"),
    ("iron", "Steam Ironing", "Steam ironing prices"),
    ("home", "Home Linen", "Home linen cleaning"),
]
_HOME_WORDS = re.compile(r"blanket|quilt|razai|rajai|curtain|bedsheet|bed sheet|sofa|carpet|duvet|comforter", re.I)
_GARMENT_EN = {"pant": "Trouser", "razai/blanket": "Blanket / Quilt", "suit 2pc": "Suit (2-piece)", "suit 3pc": "Suit (3-piece)"}


def _category(service: str, garment: str, unit: str) -> str:
    s = service.lower()
    if unit == "kg":
        return "kg"
    if _HOME_WORDS.search(garment):
        return "home"
    if "dry" in s:
        return "dry"
    if "wash" in s:
        return "wash"
    if "iron" in s or "press" in s:
        return "iron"
    return "other:" + service.strip()


def _label(service: str, garment: str, unit: str) -> str:
    if unit == "kg" or not garment:
        return re.sub(r"\s*\(kg\)\s*", "", service).strip()
    return _GARMENT_EN.get(garment.strip().lower(), garment.strip())


def _inr(v: Decimal) -> str:
    v = Decimal(v)
    return f"₹{int(v):,}" if v == v.to_integral() else f"₹{v:,.2f}"


async def _load() -> list[tuple[str, str, str, Decimal]]:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.rate import Rate
    from app.services import tenant_context

    home = await tenant_context.get_home_tenant_id()
    if home is None:
        return []
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.rate, Rate.garment)
                )
            ).scalars().all()
            return [(r.service, r.garment or "", r.unit, r.rate) for r in rows]


async def rate_card() -> dict[str, dict]:
    """{key: {title, heading, items:[(label, price_text, amount, unit)]}} — website ke kram mein."""
    now = time.monotonic()
    if _cache["rows"] is None or now - _cache["at"] > _TTL:
        try:
            _cache["rows"] = await _load()
        except Exception:
            log.exception("site_rates_load_failed")
            if _cache["rows"] is None:
                _cache["rows"] = []
        _cache["at"] = now

    out: dict[str, dict] = {k: {"title": t, "heading": h, "items": []} for k, t, h in _TABS}
    for service, garment, unit, rate in _cache["rows"]:
        key = _category(service, garment, unit)
        if key not in out:
            name = service.strip()
            out[key] = {"title": name, "heading": f"{name} prices", "items": []}
        price = _inr(rate) + (" / kg" if unit == "kg" else "")
        out[key]["items"].append((_label(service, garment, unit), price, rate, unit))
    return {k: v for k, v in out.items() if v["items"]}


def _from_price(card: dict, key: str) -> str:
    c = card.get(key)
    if not c:
        return ""
    low = min(c["items"], key=lambda i: i[2])
    unit = "kg" if low[3] == "kg" else "piece"
    return f'<div class="price-tag">From <b>{_inr(low[2])}</b> / {unit}</div>'


def _find(card: dict, key: str, *names: str):
    for label, _, amount, _ in card.get(key, {}).get("items", []):
        if label.lower() in names:
            return amount
    return None


async def category_block(keys: list[str]) -> tuple[str, list[dict]]:
    """Service page ke liye: in categories ke daam ki list (HTML) + JSON-LD offers."""
    card = await rate_card()
    esc = html.escape
    parts, offers = [], []
    for key in keys:
        c = card.get(key)
        if not c:
            continue
        lis = "".join(f'<li data-price="{esc(p)}">{esc(label)}</li>' for label, p, _, _ in c["items"])
        parts.append(f'<h3>{esc(c["heading"])}</h3><ul class="items">{lis}</ul>')
        for label, _, amount, unit in c["items"]:
            offers.append({
                "@type": "Offer",
                "itemOffered": {"@type": "Service", "name": f'{c["title"]} – {label}'},
                "priceSpecification": {
                    "@type": "UnitPriceSpecification", "price": float(amount), "priceCurrency": "INR",
                    "unitText": "per kg" if unit == "kg" else "per piece",
                },
            })
    if not parts:
        parts.append('<p class="muted empty">Our latest rates are shared instantly on WhatsApp — ask our assistant.</p>')
    return "".join(parts), offers


async def render(page: str) -> str:
    """app/site/templates/index.html ke {{...}} placeholders bharo."""
    card = await rate_card()
    esc = html.escape

    tabs, panels = [], []
    for i, (key, c) in enumerate(card.items()):
        slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
        on = i == 0
        selected = 'aria-selected="true"' if on else 'aria-selected="false" tabindex="-1"'
        tabs.append(
            f'<button class="tab" role="tab" id="t-{slug}" aria-controls="p-{slug}" '
            f'{selected}>{esc(c["title"])}</button>'
        )
        lis = "".join(f'<li data-price="{esc(p)}">{esc(label)}</li>' for label, p, _, _ in c["items"])
        hidden = "" if on else " hidden"
        panels.append(
            f'<div class="panel" role="tabpanel" id="p-{slug}" aria-labelledby="t-{slug}"{hidden}>'
            f'<h3>{esc(c["heading"])}</h3><ul class="items">{lis}</ul></div>'
        )
    if not card:
        panels.append('<div class="panel"><p class="muted empty">Our latest rates are '
                      'shared instantly on WhatsApp — ask our assistant.</p></div>')

    # Dry cleaning ka FAQ jawab — jo item card mein hain unhi se
    dry = card.get("dry")
    if dry:
        parts = [f"{_inr(a)} for a {n}" for n, a in (
            ("shirt", _find(card, "dry", "shirt")),
            ("saree", _find(card, "dry", "saree")),
            ("two-piece suit", _find(card, "dry", "suit (2-piece)")),
        ) if a is not None]
        low = _inr(min(i[2] for i in dry["items"]))
        faq = ("Dry cleaning at Kwik Klin starts at " +
               (", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else low) +
               ". The full rate list is on this page.")
    else:
        faq = "See the full rate list on this page, or ask our assistant on WhatsApp for any item."

    # JSON-LD offers: har category ka sabse kam daam
    offers = []
    for key, c in card.items():
        low = min(c["items"], key=lambda i: i[2])
        offers.append({
            "@type": "Offer",
            "itemOffered": {"@type": "Service", "name": c["title"]},
            "priceSpecification": {
                "@type": "UnitPriceSpecification", "price": float(low[2]), "priceCurrency": "INR",
                "unitText": "per kg" if low[3] == "kg" else "per piece",
            },
        })
    amounts = [i[2] for c in card.values() for i in c["items"]]
    price_range = f"{_inr(min(amounts))} – {_inr(max(amounts))}" if amounts else "₹"

    # Chat widget ke "Rate list" jawab ke liye — har tab ke pehle 2 item
    popular = [f"{label} ({c['title']}) — {p}" for c in card.values() for label, p, _, _ in c["items"][:2]][:6]

    def js(v) -> str:  # <script> ke andar safe JSON
        return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")

    fills = {
        "{{RATE_TABS}}": "".join(tabs),
        "{{RATE_PANELS}}": "".join(panels),
        "{{FROM_DRY}}": _from_price(card, "dry"),
        "{{FROM_WASH}}": _from_price(card, "wash"),
        "{{FROM_KG}}": _from_price(card, "kg"),
        "{{FROM_IRON}}": _from_price(card, "iron"),
        "{{FROM_HOME}}": _from_price(card, "home"),
        "{{FAQ_DRY_HTML}}": esc(faq),
        "{{FAQ_DRY_JSON}}": js(faq),
        "{{OFFERS_JSON}}": js(offers),
        "{{PRICE_RANGE_JSON}}": js(price_range),
        "{{POPULAR_JSON}}": js(popular),
    }
    for k, v in fills.items():
        page = page.replace(k, v)
    return page
