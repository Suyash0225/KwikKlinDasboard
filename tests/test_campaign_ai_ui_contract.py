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
