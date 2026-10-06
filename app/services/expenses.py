"""Kharcha — categories aur ek row likhne ka ek hi rasta.

Teen jagah se kharcha aata hai: owner dashboard, staff panel aur AI agent.
Teeno ek hi category list dekhein, warna reports ka donut "Petrol" aur
"petrol" do alag tukde dikhata hai.

Category list = built-in (code mein) + owner ki apni (settings_kv, per
dukaan). Owner apni category hata sakta hai; built-in nahi, kyunki agent ke
anumaan (petrol -> Transport) unhi par tike hain. Hatayi hui category purane
kharchon se nahi mitti — wo sirf naye kharche ki list se jaati hai.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense
from app.services import app_settings

BUILTIN_CATEGORIES = (
    "Fuel", "Material", "Vehicle/Maintenance",
    "Detergent", "Electricity", "Rent", "Salary", "Transport", "Maintenance", "Other",
)
MAX_CUSTOM = 30
_CLOSING_KEY = "staff_expense_closings"
_CLOSING_KEEP_DAYS = 45


class CategoryError(ValueError):
    pass


def _clean(name: str) -> str:
    return " ".join((name or "").split())[:40]


async def custom_categories(db: AsyncSession) -> list[str]:
    raw = await app_settings.get(db, "expense_categories") or []
    return [c for c in raw if isinstance(c, str) and c.strip()]


async def all_categories(db: AsyncSession) -> list[str]:
    """Built-in pehle ("Other" aakhir mein), phir owner ki apni."""
    builtin = [c for c in BUILTIN_CATEGORIES if c != "Other"]
    seen = {c.lower() for c in builtin} | {"other"}
    extra = []
    for c in await custom_categories(db):
        if c.lower() not in seen:
            seen.add(c.lower())
            extra.append(c)
    return builtin + extra + ["Other"]


async def add_category(db: AsyncSession, name: str) -> str:
    clean = _clean(name)
    if len(clean) < 2:
        raise CategoryError("Category name is too short")
    existing = await all_categories(db)
    same = next((c for c in existing if c.lower() == clean.lower()), None)
    if same:
        raise CategoryError(f"'{same}' is already a category")
    custom = await custom_categories(db)
    if len(custom) >= MAX_CUSTOM:
        raise CategoryError(f"Up to {MAX_CUSTOM} own categories — remove one first")
    await app_settings.set_value(db, "expense_categories", custom + [clean])
    return clean


async def remove_category(db: AsyncSession, name: str) -> None:
    if name.lower() in {c.lower() for c in BUILTIN_CATEGORIES}:
        raise CategoryError("Built-in categories cannot be removed")
    custom = await custom_categories(db)
    left = [c for c in custom if c.lower() != name.lower()]
    if len(left) == len(custom):
        raise CategoryError("No such category")
    await app_settings.set_value(db, "expense_categories", left)


async def canonical_category(db: AsyncSession, name: str) -> str | None:
    """List wali spelling lautao ("petrol" -> "Petrol"), na mile to None."""
    low = _clean(name).lower()
    return next((c for c in await all_categories(db) if c.lower() == low), None)


def row(e: Expense) -> dict:
    return {
        "id": str(e.id),
        "category": e.category,
        "amount": str(e.amount),
        "spent_on": e.spent_on.isoformat(),
        "description": e.description,
        "added_by": e.added_by,
    }


async def record(
    db: AsyncSession,
    *,
    category: str,
    amount: Decimal,
    spent_on: date,
    description: str | None = None,
    added_by: str | None = None,
    staff_id=None,
) -> Expense:
    exp = Expense(
        category=category,
        amount=amount,
        spent_on=spent_on,
        description=(description or "").strip()[:300] or None,
        added_by=added_by,
        staff_id=staff_id,
    )
    db.add(exp)
    await db.commit()
    return exp


async def is_daily_closing_submitted(db: AsyncSession, staff_id, spent_on: date) -> bool:
    raw = await app_settings.get(db, _CLOSING_KEY)
    data = raw if isinstance(raw, dict) else {}
    return bool(data.get(f"{spent_on.isoformat()}:{staff_id}"))


async def mark_daily_closing_submitted(db: AsyncSession, staff_id, spent_on: date, *, total: Decimal = Decimal("0")) -> None:
    raw = await app_settings.get(db, _CLOSING_KEY)
    data = dict(raw) if isinstance(raw, dict) else {}
    data[f"{spent_on.isoformat()}:{staff_id}"] = {"submitted": True, "total": str(total)}
    cutoff = spent_on.toordinal() - _CLOSING_KEEP_DAYS
    kept = {}
    for key, value in data.items():
        try:
            d = date.fromisoformat(str(key)[:10])
        except (ValueError, TypeError):
            continue
        if d.toordinal() >= cutoff:
            kept[key] = value
    await app_settings.set_value(db, _CLOSING_KEY, kept)
