from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_campaign_send_requires_a_fresh_successful_preview():
    source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    preview = source[source.index("async function previewCampaignRecipients"):source.index("function renderCampaignPreviewRows")]
    create = source[source.index("async function createCampaign(btn)"):source.index("function approveCampaign(id)")]

    # A failed request cannot leave a prior successful preview available.
    assert "window.CAMPAIGN_PREVIEW_DATA=null" in preview
    assert "return d;}catch(e)" in preview
    assert "return null;}}" in preview

    # The send action must fetch a fresh preview and stop on failure/zero recipients.
    assert "const preview=await previewCampaignRecipients(true)" in create
    assert "if(!preview)" in create
    assert "if(eligible<=0)" in create
    assert "Fresh preview:" in create
    assert create.index("previewCampaignRecipients(true)") < create.index('/approve')
