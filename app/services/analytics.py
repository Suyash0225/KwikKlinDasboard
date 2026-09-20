"""Owner analytics aggregation: GA4 + Google Business Profile + WhatsApp + AI."""

import json
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.models import Conversation, LlmUsage
from app.models.enums import Direction


async def _ga4_report(days: int = 30) -> dict:
    """Read GA4 via the Data API when a service-account credential is configured.

    GOOGLE_APPLICATION_CREDENTIALS must point to a service-account JSON file and
    GA4_PROPERTY_ID must be the numeric GA4 property id. The service account
    must be granted Viewer/Analyst access to that GA4 property.
    """
    if not settings.GA4_PROPERTY_ID or not settings.GOOGLE_APPLICATION_CREDENTIALS:
        return {"configured": False, "reason": "GA4 reporting credentials are not configured"}

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_APPLICATION_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        creds.refresh(Request())

        body = {
            "dateRanges": [{"startDate": f"{max(1, days)}daysAgo", "endDate": "today"}],
            "metrics": [
                {"name": "activeUsers"},
                {"name": "newUsers"},
                {"name": "sessions"},
                {"name": "screenPageViews"},
                {"name": "engagementRate"},
            ],
            "dimensions": [{"name": "date"}],
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
            "limit": 100,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{settings.GA4_PROPERTY_ID}:runReport",
                headers={"Authorization": f"Bearer {creds.token}"},
                json=body,
            )
        if r.status_code != 200:
            return {"configured": True, "error": f"GA4 API {r.status_code}", "detail": r.text[:300]}

        data = r.json()
        totals = (data.get("totals") or [{}])[0].get("metricValues") or []
        names = ["active_users", "new_users", "sessions", "page_views", "engagement_rate"]
        summary = {}
        for name, item in zip(names, totals):
            summary[name] = float(item.get("value") or 0)

        rows = []
        for row in data.get("rows") or []:
            vals = row.get("metricValues") or []
            rows.append({
                "date": (row.get("dimensionValues") or [{}])[0].get("value", ""),
                **{n: float(v.get("value") or 0) for n, v in zip(names, vals)},
            })
        return {"configured": True, "summary": summary, "daily": rows}
    except Exception as exc:
        return {"configured": True, "error": str(exc)[:300]}


async def owner_snapshot(db, days: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    recent = now - timedelta(days=days)

    # Outbound WhatsApp: real status values written by Meta/DotPe webhooks.
    wa_rows = (
        await db.execute(
            select(Conversation.status, Conversation.billing_category, func.count())
            .where(
                Conversation.direction == Direction.OUTBOUND,
                Conversation.created_at >= recent,
            )
            .group_by(Conversation.status, Conversation.billing_category)
        )
    ).all()
    wa = {"total": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0,
          "service": 0, "utility": 0, "marketing": 0, "unknown": 0}
    for status, category, count in wa_rows:
        n = int(count or 0)
        wa["total"] += n
        key = str(status or "unknown").lower()
        if key in wa:
            wa[key] += n
        else:
            wa["unknown"] += n
        cat = str(category or "").lower()
        if cat in ("service", "utility", "marketing"):
            wa[cat] += n

    # AI tokens — raw provider usage already recorded by llm_client.
    ai_rows = (
        await db.execute(
            select(
                func.sum(LlmUsage.input_tokens),
                func.sum(LlmUsage.output_tokens),
                func.count(),
            ).where(LlmUsage.at >= recent)
        )
    ).one()
    ai = {
        "input_tokens": int(ai_rows[0] or 0),
        "output_tokens": int(ai_rows[1] or 0),
        "calls": int(ai_rows[2] or 0),
    }
    ai["total_tokens"] = ai["input_tokens"] + ai["output_tokens"]

    ai_today = (
        await db.execute(
            select(func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.output_tokens), func.count())
            .where(LlmUsage.at >= day_start)
        )
    ).one()
    ai["today"] = {
        "input_tokens": int(ai_today[0] or 0),
        "output_tokens": int(ai_today[1] or 0),
        "calls": int(ai_today[2] or 0),
    }
    ai_month = (
        await db.execute(
            select(func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.output_tokens), func.count())
            .where(LlmUsage.at >= month_start)
        )
    ).one()
    ai["month"] = {
        "input_tokens": int(ai_month[0] or 0),
        "output_tokens": int(ai_month[1] or 0),
        "calls": int(ai_month[2] or 0),
    }

    # Google Business Profile performance uses the already-connected owner
    # OAuth refresh token; no Google credential is sent to the browser.
    gbp = {"configured": False}
    try:
        from app.services import google_business as gbp_service
        conn = await gbp_service.get_connection(db)
        if conn.get("refresh_token") and conn.get("location"):
            gbp = await gbp_service.fetch_performance(conn["refresh_token"], conn["location"], days=days)
            gbp["configured"] = True
    except Exception as exc:
        gbp = {"configured": True, "error": str(exc)[:300]}

    ga4 = await _ga4_report(days)
    return {
        "period_days": days,
        "generated_at": now.isoformat(),
        "website": ga4,
        "google_business": gbp,
        "whatsapp": wa,
        "ai": ai,
    }
