from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_campaign_editor_wires_ai_draft_without_auto_sending():
    html = (ROOT / "app/static/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert 'id="camp-ai-goal"' in html
    assert 'onclick="generateCampaignDraft(this)"' in html
    assert 'id="camp-ai-gmb-preview"' in html
    assert 'api("/admin/api/campaigns/ai-draft"' in js
    assert "Nothing has been sent" in js
    start = js.index("async function generateCampaignDraft")
    end = js.index("function selectedCampaignType", start)
    draft_function = js[start:end]
    assert "/approve" not in draft_function
    assert "test-send" not in draft_function


def test_ai_google_copy_uses_policy_sanitizer_and_validates_poster():
    source = (ROOT / "app/routers/agent_admin.py").read_text(encoding="utf-8")
    assert "enforce_google_rules(draft[\"gmb_body\"])" in source
    assert "enforce_google_rules(draft[\"gmb_title\"])" in source
    assert "check_google_image(image_path)" in source


def test_control_panel_shows_per_location_google_post_results():
    control = (ROOT / "app/static/control.js").read_text(encoding="utf-8")
    assert "gb.auto_post_locations || []" in control
    assert 'p.status === "published"' in control
    assert 'p.status === "failed"' in control
    assert "p.error" in control

def test_campaign_image_preview_falls_back_to_local_upload_blob():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    start = js.index("async function uploadCampaignImage")
    end = js.index("function removeCampaignImage", start)
    upload_function = js[start:end]
    assert "window.CAMPAIGN_PREVIEW_URL" in upload_function
    assert "onerror=" in upload_function
    assert "this.src=window.CAMPAIGN_PREVIEW_URL" in upload_function


def test_ai_google_copy_gets_natural_local_search_context():
    from app.routers.agent_admin import _ensure_gmb_local_search_context

    body = _ensure_gmb_local_search_context("Fresh clothes, less hassle this week.")
    assert "laundry" in body.lower()
    assert "dry cleaning" in body.lower()
    assert "varanasi" in body.lower()

    body_with_service = _ensure_gmb_local_search_context("Book laundry service today.")
    assert "varanasi" in body_with_service.lower()
    assert body_with_service.lower().count("laundry") == 1

    body_with_location = _ensure_gmb_local_search_context("Enjoy convenient pickup in Varanasi.")
    assert "laundry and dry cleaning services" in body_with_location.lower()
    assert body_with_location.lower().count("varanasi") == 1

    complete = "Laundry and dry cleaning in Varanasi for fresh garments."
    assert _ensure_gmb_local_search_context(complete) == complete

    long_copy = _ensure_gmb_local_search_context("Lovely clothes this week. " * 60)
    assert len(long_copy) <= 700
    assert "varanasi" in long_copy.lower()
    assert "dry cleaning services in varanasi." in long_copy.lower()


def test_ai_campaign_creative_uses_gemini_artwork_with_branded_fallback():
    source = (ROOT / "app/routers/agent_admin.py").read_text(encoding="utf-8")
    assert "await ai_poster.create_branded_campaign_poster(" in source
    assert "draw_poster(" not in source[source.index("async def ai_campaign_draft"):source.index("@router.post(\"/campaigns\",")]
