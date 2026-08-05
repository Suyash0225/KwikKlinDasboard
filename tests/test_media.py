"""Every WhatsApp message type, handled — not turned into "[document]".

Before this, only text, taps and photos meant anything: a voice note, PDF,
video or shared pin became a dead marker, the file was never fetched, and
the customer got silence back.
"""

import pytest
from sqlalchemy import select

import app.routers.webhook as webhook_module
from app.database import async_session_factory
from app.models import Conversation
from app.services.ai_agent import _media_ack
from tests.conftest import (
    TEST_CUSTOMER_PHONE,
    TEST_CUSTOMER_PHONE_RAW,
    meta_payload,
    sign_body,
)


def _media_msg(mtype: str, wamid: str, **part) -> bytes:
    return meta_payload(messages=[{
        "from": TEST_CUSTOMER_PHONE_RAW, "id": wamid, "type": mtype,
        mtype: {"id": "media-123", **part},
    }])


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    """Record download attempts and hand back a predictable filename."""
    calls: list[str] = []

    async def fake_download(media_id, dest_dir):
        calls.append(media_id)
        return "in-abc123.pdf"

    monkeypatch.setattr(
        "app.services.whatsapp.download_media", fake_download, raising=True
    )
    return calls


@pytest.mark.parametrize(
    "mtype,marker",
    [("audio", "audio"), ("voice", "voice"), ("video", "video"),
     ("document", "document"), ("sticker", "image")],
)
async def test_every_media_type_is_downloaded_and_stored(
    client, sent, downloads, mtype, marker
) -> None:
    body = _media_msg(mtype, f"wamid.TESTmedia-{mtype}")
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    assert downloads, f"{mtype} must be fetched from Meta, not dropped"

    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(
                    Conversation.wa_message_id == f"wamid.TESTmedia-{mtype}"
                )
            )
        ).scalars().one()
    assert conv.message_text.startswith(f"[{marker}:/admin/media/in-abc123.pdf]"), (
        f"{mtype} stored as {conv.message_text!r}"
    )


async def test_document_keeps_its_filename(client, sent, downloads) -> None:
    """A PDF's own name is the only human-readable label it has."""
    body = _media_msg(
        "document", "wamid.TESTmedia-docname", filename="Sharma-bill.pdf",
    )
    await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(
                    Conversation.wa_message_id == "wamid.TESTmedia-docname"
                )
            )
        ).scalars().one()
    assert "Sharma-bill.pdf" in conv.message_text


# --- non-file types the old code flattened to "[location]" etc. ---

def test_location_keeps_coordinates_and_name() -> None:
    out = webhook_module._extract_text({
        "type": "location",
        "location": {"latitude": 25.31, "longitude": 82.97, "name": "Sigra",
                     "address": "Varanasi"},
    })
    assert out.startswith("[location:25.31,82.97]")
    assert "Sigra" in out and "Varanasi" in out


def test_contact_card_becomes_readable_text() -> None:
    out = webhook_module._extract_text({
        "type": "contacts",
        "contacts": [{"name": {"formatted_name": "Ravi"},
                      "phones": [{"phone": "+919876543210"}]}],
    })
    assert "Ravi" in out and "+919876543210" in out


def test_reaction_is_captured() -> None:
    out = webhook_module._extract_text({"type": "reaction", "reaction": {"emoji": "👍"}})
    assert "👍" in out


# --- the customer hears back instead of silence ---

@pytest.mark.parametrize(
    "marker,expect",
    [("[audio:/admin/media/x.ogg]", "Voice note"),
     ("[document:/admin/media/x.pdf] bill.pdf", "File"),
     ("[video:/admin/media/x.mp4]", "Video"),
     ("[location:25.3,82.9] Sigra", "Location"),
     ("[image:/admin/media/x.jpg]", "Photo")],
)
def test_media_gets_an_acknowledgement(marker, expect) -> None:
    ack = _media_ack(marker)
    assert ack and expect.lower() in ack.lower()


@pytest.mark.parametrize("marker", ["[button:rate_good] Good", "[interactive:list]", "[reaction] 👍"])
def test_our_own_buttons_are_not_chatted_back_at(marker) -> None:
    assert _media_ack(marker) is None


async def test_voice_note_customer_is_not_left_in_silence(client, sent, downloads) -> None:
    """End to end: a voice note arrives and something goes back."""
    body = _media_msg("audio", "wamid.TESTmedia-ack")
    await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    replies = [c for c in sent if c["to"] == TEST_CUSTOMER_PHONE]
    assert replies, "a customer who sends a voice note must get a reply"
    assert "voice note" in (replies[0]["text"] or "").lower()


# --- file extensions ---

def test_extension_map_covers_the_common_types() -> None:
    from app.services.whatsapp import MEDIA_EXT

    for mime, ext in [
        ("application/pdf", ".pdf"), ("audio/ogg", ".ogg"),
        ("video/mp4", ".mp4"), ("image/jpeg", ".jpg"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ]:
        assert MEDIA_EXT[mime] == ext


# --- voice notes are understood, not just filed ---

async def test_voice_note_is_transcribed_and_acted_on(client, sent, monkeypatch) -> None:
    """The words in a voice note must reach the agent as a real message."""
    import app.routers.webhook as wh
    import app.services.ai_agent as ai

    async def fake_download(media_id, dest_dir):
        from pathlib import Path

        (Path(dest_dir) / "in-voice1.ogg").write_bytes(b"fake-ogg-bytes")
        return "in-voice1.ogg"

    async def fake_transcribe(blob, mime):
        assert blob == b"fake-ogg-bytes"
        assert mime == "audio/ogg"
        return "Bhaiya do shirt aur ek pant dhulwana hai"

    seen: dict = {}

    async def fake_reply(db, customer, text, **kw):
        seen["text"] = text
        return "Ji bilkul, pickup laga dete hain."

    monkeypatch.setattr("app.services.whatsapp.download_media", fake_download)
    monkeypatch.setattr("app.services.llm_client.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(wh, "build_ai_reply", fake_reply)

    body = _media_msg("audio", "wamid.TESTvoice1", mime_type="audio/ogg; codecs=opus")
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200

    # stored so the Inbox can BOTH play the note and show what was said
    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTvoice1")
            )
        ).scalars().one()
    assert conv.message_text.startswith("[audio:/admin/media/in-voice1.ogg]")
    assert "do shirt aur ek pant" in conv.message_text

    # and the agent saw the WORDS, not the marker
    assert seen.get("text") == "Bhaiya do shirt aur ek pant dhulwana hai"
    assert any("pickup laga dete hain" in (c["text"] or "") for c in sent)


def test_transcript_is_unwrapped_for_the_agent() -> None:
    from app.services.ai_agent import _voice_transcript

    assert _voice_transcript("[audio:/admin/media/x.ogg] kal aa jaana") == "kal aa jaana"
    assert _voice_transcript("[voice:/admin/media/x.ogg] theek hai") == "theek hai"
    # no transcript -> the caller must fall back to acknowledging the file
    assert _voice_transcript("[audio:/admin/media/x.ogg]") is None
    assert _voice_transcript("[document:/admin/media/x.pdf] bill.pdf") is None


async def test_unclear_voice_note_falls_back_to_acknowledgement(
    client, sent, monkeypatch
) -> None:
    """When we truly can't hear it, say so — never invent a message."""
    async def fake_download(media_id, dest_dir):
        from pathlib import Path

        (Path(dest_dir) / "in-voice2.ogg").write_bytes(b"noise")
        return "in-voice2.ogg"

    async def unclear(blob, mime):
        return None

    monkeypatch.setattr("app.services.whatsapp.download_media", fake_download)
    monkeypatch.setattr("app.services.llm_client.transcribe_audio", unclear)

    body = _media_msg("audio", "wamid.TESTvoice2")
    await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    replies = [c for c in sent if c["to"] == TEST_CUSTOMER_PHONE]
    assert replies and "voice note" in (replies[0]["text"] or "").lower()


async def test_transcription_failure_never_loses_the_message(monkeypatch) -> None:
    """A crash in transcription must not drop the customer's voice note."""
    import app.routers.webhook as wh
    from pathlib import Path

    async def boom(blob, mime):
        raise RuntimeError("gemini down")

    monkeypatch.setattr("app.services.llm_client.transcribe_audio", boom)
    p = Path(wh.__file__).resolve().parent.parent / "media" / "in-testcrash.ogg"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"x")
    try:
        assert await wh._transcribe(p, {"mime_type": "audio/ogg"}) is None
    finally:
        p.unlink(missing_ok=True)
