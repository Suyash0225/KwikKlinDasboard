"""Grahak ka thikana — Google Maps pin (lat/lng) ya link.

Pata (address) likha hua hota hai; naya delivery boy usse ghar nahi dhoondh
paata. Pin se Maps seedha darwaze tak le jaata hai. Pin do raste se aata hai:
  - staff pickup/delivery par darwaze par khada ho kar phone ke GPS se
  - owner dashboard se Google Maps ka link daal kar

Short link (maps.app.goo.gl/...) se coordinates nahi nikalte — tab link hi
navigation ban jaata hai.
"""

import re
from decimal import Decimal

_PAIR = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
# Pehle wo jagah jahan Google khud pin rakhta hai; aakhir mein koi bhi jodi
_HINTS = (
    re.compile(r"[?&](?:q|query|destination|ll|center)=(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)"),
    re.compile(r"/@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)"),
    re.compile(r"^geo:(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)"),
    re.compile(r"/(?:place|search)/(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)"),
    _PAIR,
)


def valid_coords(lat, lng) -> bool:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    # 0,0 samundar hai — GPS fail hone par phone kabhi-kabhi yahi deta hai
    return -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)


def parse_map_link(url: str | None) -> tuple[Decimal, Decimal] | None:
    """Google Maps / geo: link se (lat, lng), warna None."""
    raw = (url or "").strip()
    if not raw:
        return None
    for rx in _HINTS:
        m = rx.search(raw)
        if m and valid_coords(m.group(1), m.group(2)):
            return Decimal(m.group(1)).quantize(Decimal("0.000001")), Decimal(m.group(2)).quantize(Decimal("0.000001"))
    return None


def valid_map_link(url: str | None) -> bool:
    raw = (url or "").strip()
    return bool(raw) and len(raw) <= 500 and (raw.startswith(("https://", "http://", "geo:")))


def nav_url(lat, lng, map_url: str | None = None) -> str:
    """Ek tap par Google Maps directions — pin ho to pin, warna link."""
    if valid_coords(lat, lng):
        return f"https://www.google.com/maps/dir/?api=1&travelmode=driving&destination={float(lat):.6f},{float(lng):.6f}"
    return (map_url or "").strip()


def waypoint(cust) -> str:
    """Route ke liye padav: pin ho to 'lat,lng' (sateek), warna pata."""
    if cust is None:
        return ""
    if valid_coords(cust.lat, cust.lng):
        return f"{float(cust.lat):.6f},{float(cust.lng):.6f}"
    return (cust.address or "").strip()


def payload(cust) -> dict:
    """API ke liye: {"has_location", "nav_url"} — har jagah ek hi shakal."""
    if cust is None:
        return {"has_location": False, "nav_url": ""}
    url = nav_url(cust.lat, cust.lng, cust.map_url)
    return {"has_location": bool(url), "nav_url": url}


def set_location(cust, *, lat=None, lng=None, map_url: str | None = None, by: str = "") -> bool:
    """Grahak par pin/link likho. Kuch mila to True.

    GPS (lat/lng) aaya to wahi sach hai; link aaya to usse coordinates
    nikaalne ki koshish, na nikle to sirf link.
    """
    from datetime import datetime, timezone

    if lat is not None and lng is not None and valid_coords(lat, lng):
        cust.lat = Decimal(str(float(lat))).quantize(Decimal("0.000001"))
        cust.lng = Decimal(str(float(lng))).quantize(Decimal("0.000001"))
        if map_url is not None:
            cust.map_url = map_url.strip()[:500] or None
    elif map_url is not None:
        link = map_url.strip()[:500]
        if not link:
            cust.lat = cust.lng = cust.map_url = None
            cust.location_at = cust.location_by = None
            return True
        if not valid_map_link(link):
            raise ValueError("Google Maps ka link daaliye (https://... ya geo:)")
        parsed = parse_map_link(link)
        cust.map_url = link
        cust.lat, cust.lng = parsed if parsed else (None, None)
    else:
        return False
    cust.location_at = datetime.now(timezone.utc)
    cust.location_by = (by or "")[:120] or None
    return True
