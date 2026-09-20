"""Google Analytics 4 realtime + Google Business Profile performance.

These are reporting adapters only. If credentials are not configured, the
dashboard returns a truthful 'not connected' state rather than inventing data.
"""

from datetime import date, timedelta
import httpx

from app.config import settings
from app.services import google_business as gbp


class AnalyticsError(Exception):
    pass


async def _ga_access_token() -> str:
    if not (settings.GA4_REFRESH_TOKEN and settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise AnalyticsError("GA4 reporting is not connected")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": settings.GA4_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise AnalyticsError("GA4 Google authorization expired or lacks analytics.readonly")
    return r.json()["access_token"]


async def ga4_realtime() -> dict:
    if not settings.GA4_PROPERTY_ID:
        return {"configured": False, "active_users": 0, "views": 0, "events": 0}
    token = await _ga_access_token()
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{settings.GA4_PROPERTY_ID}:runRealtimeReport"
    body = {
        "metrics": [
            {"name": "activeUsers"},
            {"name": "screenPageViews"},
            {"name": "eventCount"},
        ],
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code != 200:
        raise AnalyticsError(f"GA4 realtime error {r.status_code}")
    rows = (r.json() or {}).get("rows") or []
    vals = rows[0]["metricValues"] if rows else []
    numbers = [int(v.get("value") or 0) for v in vals]
    return {
        "configured": True,
        "active_users": numbers[0] if len(numbers) > 0 else 0,
        "views": numbers[1] if len(numbers) > 1 else 0,
        "events": numbers[2] if len(numbers) > 2 else 0,
    }


async def ga4_daily(days: int = 7) -> dict:
    if not settings.GA4_PROPERTY_ID:
        return {"configured": False, "rows": []}
    token = await _ga_access_token()
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{settings.GA4_PROPERTY_ID}:runReport"
    body = {
        "dateRanges": [{"startDate": f"{max(1, days)}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "activeUsers"}, {"name": "screenPageViews"}, {"name": "eventCount"}],
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code != 200:
        raise AnalyticsError(f"GA4 report error {r.status_code}")
    out = []
    for row in (r.json() or {}).get("rows") or []:
        out.append({
            "date": (row.get("dimensionValues") or [{}])[0].get("value"),
            "active_users": int(row["metricValues"][0].get("value") or 0),
            "views": int(row["metricValues"][1].get("value") or 0),
            "events": int(row["metricValues"][2].get("value") or 0),
        })
    return {"configured": True, "rows": out}


async def gbp_performance(db, days: int = 30) -> dict:
    conn = await gbp.get_connection(db)
    location = str(conn.get("location") or "")
    if not location:
        return {"configured": False, "latest_date": None, "metrics": {}}

    token = await gbp._access_token(conn["refresh_token"])
    end = date.today()
    start = end - timedelta(days=max(1, min(days, 90)))
    params = [
        ("dailyMetrics", "WEBSITE_CLICKS"),
        ("dailyMetrics", "CALL_CLICKS"),
        ("dailyMetrics", "BUSINESS_DIRECTION_REQUESTS"),
        ("dailyMetrics", "BUSINESS_IMPRESSIONS_DESKTOP_MAPS"),
        ("dailyMetrics", "BUSINESS_IMPRESSIONS_MOBILE_MAPS"),
        ("dailyMetrics", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH"),
        ("dailyMetrics", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH"),
        ("dailyRange.startDate.year", str(start.year)),
        ("dailyRange.startDate.month", str(start.month)),
        ("dailyRange.startDate.day", str(start.day)),
        ("dailyRange.endDate.year", str(end.year)),
        ("dailyRange.endDate.month", str(end.month)),
        ("dailyRange.endDate.day", str(end.day)),
    ]
    url = f"https://businessprofileperformance.googleapis.com/v1/{location}:fetchMultiDailyMetricsTimeSeries"
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
    if r.status_code != 200:
        raise AnalyticsError(f"GBP performance error {r.status_code}")
    body = r.json() or {}
    totals = {}
    latest = None
    for group in body.get("multiDailyMetricTimeSeries") or []:
        series_list = group.get("dailyMetricTimeSeries") or []
        if isinstance(series_list, dict):
            series_list = [series_list]
        for series in series_list:
            metric = str(series.get("dailyMetric") or "")
            values = (series.get("timeSeries") or {}).get("datedValues") or []
            total = 0
            for v in values:
                total += int(v.get("value") or 0)
                d = v.get("date") or {}
                if d:
                    stamp = f"{d.get('year',0):04d}-{d.get('month',0):02d}-{d.get('day',0):02d}"
                    latest = max(latest or "", stamp)
            if metric:
                totals[metric] = totals.get(metric, 0) + total
    return {"configured": True, "latest_date": latest, "metrics": totals}


async def growth_snapshot(db) -> dict:
    website = {"configured": False, "error": None}
    try:
        website = {**await ga4_realtime(), "error": None}
    except Exception as exc:
        website["error"] = str(exc)
    gmb = {"configured": False, "error": None}
    try:
        gmb = {**await gbp_performance(db, 30), "error": None}
    except Exception as exc:
        gmb["error"] = str(exc)
    return {"website": website, "gmb": gmb}
