"""Google Maps link/pin ka hisaab (services/geo.py) — DB nahi chahiye."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import geo


@pytest.mark.parametrize("url, lat, lng", [
    ("https://www.google.com/maps/@25.317645,82.973915,17z", "25.317645", "82.973915"),
    ("https://maps.google.com/?q=25.3176,82.9739", "25.3176", "82.9739"),
    ("https://www.google.com/maps/search/?api=1&query=25.3176,82.9739", "25.3176", "82.9739"),
    ("https://www.google.com/maps/place/Assi+Ghat/@25.2878,83.0063,15z/data=!3m1", "25.2878", "83.0063"),
    ("geo:25.3176,82.9739?q=ghar", "25.3176", "82.9739"),
    ("https://www.google.com/maps/dir/?api=1&destination=25.3176,82.9739", "25.3176", "82.9739"),
])
def test_parse_map_link_reads_common_google_formats(url, lat, lng) -> None:
    got = geo.parse_map_link(url)
    assert got == (Decimal(lat).quantize(Decimal("0.000001")), Decimal(lng).quantize(Decimal("0.000001")))


def test_short_link_keeps_the_link_but_has_no_pin() -> None:
    """maps.app.goo.gl mein coordinates nahi hote — link hi navigation hai."""
    assert geo.parse_map_link("https://maps.app.goo.gl/AbCdEf123") is None
    cust = SimpleNamespace(lat=None, lng=None, map_url=None, address="Lanka", location_at=None, location_by=None)
    assert geo.set_location(cust, map_url="https://maps.app.goo.gl/AbCdEf123", by="owner")
    assert cust.lat is None and cust.map_url == "https://maps.app.goo.gl/AbCdEf123"
    assert geo.payload(cust) == {"has_location": True, "nav_url": "https://maps.app.goo.gl/AbCdEf123"}
    assert geo.waypoint(cust) == "Lanka", "bina pin ke route pate se"


def test_gps_pin_wins_and_builds_directions() -> None:
    cust = SimpleNamespace(lat=None, lng=None, map_url=None, address="", location_at=None, location_by=None)
    assert geo.set_location(cust, lat=25.31764512, lng=82.97391599, by="Ravi")
    assert (cust.lat, cust.lng) == (Decimal("25.317645"), Decimal("82.973916"))
    assert cust.location_by == "Ravi" and cust.location_at is not None
    assert geo.nav_url(cust.lat, cust.lng) == (
        "https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=25.317645,82.973916"
    )
    assert geo.waypoint(cust) == "25.317645,82.973916"


def test_bad_or_empty_input() -> None:
    cust = SimpleNamespace(lat=Decimal("1"), lng=Decimal("2"), map_url="x", address="", location_at=1, location_by="a")
    assert geo.set_location(cust, map_url="") is True, "khali link = pin hatao"
    assert cust.lat is None and cust.map_url is None and cust.location_by is None
    with pytest.raises(ValueError):
        geo.set_location(cust, map_url="Lanka ke paas wali gali")
    assert geo.set_location(cust) is False
    assert not geo.valid_coords(0, 0), "0,0 = GPS fail, pin nahi"
    assert not geo.valid_coords(95, 10)
    assert geo.payload(None) == {"has_location": False, "nav_url": ""}
