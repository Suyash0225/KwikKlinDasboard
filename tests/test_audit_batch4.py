"""Audit batch 4 — AI ko jo dikhta hai wo poora ho, aur sahi dukaan ka naam."""

import uuid

from app.services import shop_identity, tenant_context
from app.services.ai_agent import _location_address, _media_text
from app.services.messages import get_message


def test_photo_and_location_content_reaches_the_ai() -> None:
    assert _media_text("[image:/admin/media/a.jpg] Image content: red saree, stain iska rate?") == (
        "(Customer sent a photo) Image content: red saree, stain iska rate?"
    )
    assert _media_text("[image:/admin/media/a.jpg]") is None          # kuch padha hi nahi
    loc = _media_text("[location:25.31,82.97] Lanka, Varanasi")
    assert "Lanka, Varanasi" in loc and "maps.google.com/?q=25.31,82.97" in loc
    assert _location_address("[location:25.31,82.97] Lanka, Varanasi") == "Lanka, Varanasi (pin 25.31,82.97)"
    assert _media_text("[button:x] y") is None and _media_text("[audio:/x] hi") is None


def test_fixed_messages_use_this_shops_name() -> None:
    tid = uuid.uuid4()
    shop_identity._NAMES[tid] = "Sharma Dry Cleaners"
    token = tenant_context.current_tenant_id.set(tid)
    try:
        assert "Sharma Dry Cleaners" in get_message("ack_received")
    finally:
        tenant_context.current_tenant_id.reset(token)
        shop_identity._NAMES.pop(tid, None)
