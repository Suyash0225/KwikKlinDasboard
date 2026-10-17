"""Deterministic per-customer visit rhythm derived from existing orders.

No new tables or outbound actions: this enriches the existing RFM segment flow.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus


def _season(month: int) -> str:
    """Broad seasons for historical item patterns; campaign calendar is separate."""
    if month in (11, 12, 1, 2):
        return "winter"
    if month in (3, 4, 5, 6):
        return "summer"
    if month in (7, 8, 9):
        return "monsoon"
    return "festive"


def _item_counts(items: Any) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in {"extra_charge", "due_charge"}:
            continue
        label = item.get("type") or item.get("garment") or item.get("item") or item.get("service")
        label = " ".join(str(label or "").split()).strip()
        if not label:
            continue
        try:
            qty = int(item.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty <= 0:
            qty = 1
        counts[label.casefold()] += qty
    return counts


def _top_labels(counts: Counter[str], labels: dict[str, str], limit: int = 3) -> list[str]:
    return [
        labels[key]
        for key, _ in sorted(
            counts.items(),
            key=lambda pair: (-pair[1], labels.get(pair[0], pair[0]).casefold()),
        )[:limit]
    ]


def build_customer_rhythms(order_rows: list[dict[str, Any]]) -> dict[UUID, dict[str, Any]]:
    """Build average visit gap, latest visit, usual items and seasonal items.

    Same-day orders count as one visit for gap calculation, avoiding a false
    zero-day rhythm when a customer places more than one order on one day.
    Item frequencies still include each order's quantities.
    """
    grouped: dict[UUID, dict[date, dict[str, Any]]] = defaultdict(dict)
    labels: dict[str, str] = {}

    for row in order_rows:
        customer_id = row.get("customer_id")
        created_at = row.get("created_at")
        if customer_id is None or not isinstance(created_at, datetime):
            continue
        visit_day = created_at.date()
        day_bucket = grouped[customer_id].setdefault(
            visit_day,
            {
                "created_at": created_at,
                "items": Counter(),
                "season": _season(visit_day.month),
            },
        )
        if created_at > day_bucket["created_at"]:
            day_bucket["created_at"] = created_at
        item_rows = row.get("items") or []
        day_bucket["items"].update(_item_counts(item_rows))
        if isinstance(item_rows, list):
            for item in item_rows:
                if not isinstance(item, dict) or item.get("kind") in {"extra_charge", "due_charge"}:
                    continue
                label = " ".join(
                    str(item.get("type") or item.get("garment") or item.get("item") or item.get("service") or "").split()
                ).strip()
                if label:
                    labels.setdefault(label.casefold(), label)

    result: dict[UUID, dict[str, Any]] = {}
    for customer_id, visits in grouped.items():
        ordered_days = sorted(visits)
        gaps = [
            (ordered_days[i] - ordered_days[i - 1]).days
            for i in range(1, len(ordered_days))
        ]
        overall: Counter[str] = Counter()
        by_season: dict[str, Counter[str]] = defaultdict(Counter)
        last_visit = None
        for visit_day in ordered_days:
            visit = visits[visit_day]
            overall.update(visit["items"])
            by_season[visit["season"]].update(visit["items"])
            if last_visit is None or visit["created_at"] > last_visit:
                last_visit = visit["created_at"]
        result[customer_id] = {
            "last_visit_at": last_visit,
            "average_visit_gap_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "usual_items": _top_labels(overall, labels),
            "seasonal_items": {
                season: _top_labels(counts, labels)
                for season, counts in sorted(by_season.items())
                if counts
            },
        }
    return result


async def load_customer_rhythms(db: AsyncSession) -> dict[UUID, dict[str, Any]]:
    """Read tenant-scoped order history through the existing RLS session."""
    rows = (
        await db.execute(
            select(Order.customer_id, Order.created_at, Order.items)
            .where(Order.status != OrderStatus.CANCELLED)
            .order_by(Order.customer_id, Order.created_at)
        )
    ).mappings().all()
    return build_customer_rhythms([dict(row) for row in rows])
