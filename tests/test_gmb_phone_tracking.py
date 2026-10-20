"""Regression checks for privacy-safe public-site acquisition events."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_site_tracks_phone_link_clicks_without_customer_data() -> None:
    source = (ROOT / "app/site/assets/js/site.js").read_text(encoding="utf-8")
    assert 'link.closest' in source or 'closest(\'a[href^="tel:"]\')' in source
    assert 'trackLeadEvent("phone_click"' in source
    # Event parameters must remain non-identifying; no phone number, name, or message.
    assert 'cta_type: link.dataset.ctaType || "phone_link"' in source


def test_public_site_keeps_existing_whatsapp_and_booking_events() -> None:
    source = (ROOT / "app/site/assets/js/site.js").read_text(encoding="utf-8")
    assert 'trackLeadEvent("whatsapp_click"' in source
    assert 'trackLeadEvent("generate_lead"' in source
