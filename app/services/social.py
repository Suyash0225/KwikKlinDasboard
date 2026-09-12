"""Daily social marketing: generate a poster + caption every day, publish
to Instagram automatically (when linked) and WhatsApp the owner a
ready-to-post pack for Google Business Profile (no usable API there).

Design:
- Poster is drawn with PIL (1080x1080, brand colours) — no external
  services, works offline.
- Caption comes from the LLM (owner's marketing_instructions applied),
  with a solid static fallback — the day NEVER goes empty.
- Instagram publish uses the Graph API content-publishing flow and needs
  ig_user_id + ig_access_token in Settings; images are fetched by Meta
  from our public /social/<file> route via public_base_url.
- Idempotent per day (sent_events), quiet-hours friendly by schedule.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import structlog
from PIL import Image, ImageDraw, ImageFont

from app.database import async_session_factory
from app.services import app_settings, audit
from app.services.llm_client import LLMError
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

IST = ZoneInfo("Asia/Kolkata")
MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"

# One theme per weekday — rotation means it never repeats within a week.
THEMES = [
    ("Monday Fresh Start", "Hafte ki shuruaat saaf kapdon se!", "Wash & Iron par bharosa — same day option available"),
    ("Suit & Blazer Day", "Meeting ho ya shaadi — crease-free confidence", "Dry clean specialists — suit, blazer, sherwani"),
    ("Kambal-Razai Care", "Bhaari kapde, halki tension", "Blanket / razai / curtain deep clean"),
    ("Ladies Special", "Saree, lehenga, suit — naye jaise", "Delicate fabrics, expert haath"),
    ("Family Bundle", "Poore ghar ke kapde, ek saath", "Per-kg wash & fold — sabse kifayati"),
    ("Weekend Ready", "Weekend plans? Kapde ready!", "Aaj do, kal pehno"),
    ("Sunday Self-care", "Aaram karo, dhulai hum karenge", "Free pickup & delivery*"),
]


def _font(size: int, bold: bool = True):
    try:
        return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def draw_poster(theme_title: str, headline: str, subline: str, out_path: Path) -> None:
    """1080x1080 brand poster — bold, readable on a phone feed."""
    W = H = 1080
    img = Image.new("RGB", (W, H), "#fff7ed")
    d = ImageDraw.Draw(img)
    # header band
    d.rectangle([0, 0, W, 200], fill="#f97316")
    d.text((60, 48), "KWIK KLIN", font=_font(84), fill="white")
    d.text((60, 148), "LAUNDRY  ·  DRY CLEAN  ·  VARANASI", font=_font(30, False), fill="#ffedd5")
    # theme chip
    d.rounded_rectangle([60, 260, 60 + 26 * len(theme_title) + 40, 330], radius=16, fill="#1c1917")
    d.text((84, 274), theme_title.upper(), font=_font(38), fill="#fdba74")
    # headline (wrap by words to ~18 chars/line)
    words, lines, cur = headline.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 22 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    y = 400
    for line in lines[:4]:
        d.text((60, y), line, font=_font(88), fill="#1c1917")
        y += 108
    d.text((60, y + 20), subline, font=_font(40, False), fill="#57534e")
    # footer band
    d.rectangle([0, H - 170, W, H], fill="#1c1917")
    d.text((60, H - 138), "WhatsApp karein:", font=_font(34, False), fill="#a8a29e")
    d.text((60, H - 92), "+91 96968 56069", font=_font(56), fill="#fdba74")
    img.save(out_path, "PNG")


async def _caption(db, theme_title: str, headline: str, subline: str) -> str:
    fallback = (
        f"{headline}\n{subline}\n\n📍 Kwik Klin, Varanasi\n"
        f"📲 WhatsApp: +91 96968 56069\n"
        "#KwikKlin #Laundry #DryClean #Varanasi #WashAndFold"
    )
    extra = ""
    try:
        extra = (await app_settings.get(db, "marketing_instructions") or "").strip()
    except Exception:
        pass
    try:
        from app.services import llm_client

        with llm_client.track("social"):
            cap = await llm_client.ask(
                system=(
                    "Write ONE short Instagram caption (max 4 lines + up to 6 "
                    "hashtags) for Kwik Klin laundry, Varanasi, in warm Hinglish. "
                    "Include WhatsApp number +91 96968 56069. No prices unless "
                    "given. " + (f"Owner's style rules: {extra}" if extra else "")
                ),
                user_text=f"Theme: {theme_title}. Headline: {headline}. Detail: {subline}.",
                max_tokens=250,
            )
        return cap.strip() or fallback
    except LLMError:
        return fallback


async def post_to_instagram(db, image_public_url: str, caption: str) -> str:
    """Two-step IG publish. Returns 'posted' | 'skipped' | 'failed:<why>'."""
    ig_user = (await app_settings.get(db, "ig_user_id") or "").strip()
    ig_token = (await app_settings.get(db, "ig_access_token") or "").strip()
    if not ig_user or not ig_token:
        return "skipped"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r1 = await c.post(
                f"https://graph.facebook.com/v21.0/{ig_user}/media",
                data={"image_url": image_public_url, "caption": caption, "access_token": ig_token},
            )
            if r1.status_code != 200:
                return f"failed:{r1.text[:150]}"
            creation_id = r1.json().get("id")
            r2 = await c.post(
                f"https://graph.facebook.com/v21.0/{ig_user}/media_publish",
                data={"creation_id": creation_id, "access_token": ig_token},
            )
            if r2.status_code != 200:
                return f"failed:{r2.text[:150]}"
            return "posted"
    except httpx.HTTPError as exc:
        return f"failed:{exc}"


async def run_daily_social(force: bool = False) -> str:
    """Generate today's poster+caption, IG-publish, WhatsApp the owner."""
    from app.services.scheduler import _claim
    from app.services.whatsapp import SendError, send_image, send_message

    now = datetime.now(IST)
    day_key = now.strftime("%Y-%m-%d")
    async with async_session_factory() as db:
        if not await app_settings.get(db, "social_daily_enabled"):
            return "disabled"
        if not force and not await _claim(f"social:{day_key}"):
            return "already_done"

        theme_title, headline, subline = THEMES[now.weekday()]
        fname = f"social-{day_key}.png"
        MEDIA_DIR.mkdir(exist_ok=True)
        path = MEDIA_DIR / fname
        try:
            draw_poster(theme_title, headline, subline, path)
        except Exception:
            log.exception("poster_draw_failed")
            return "draw_failed"
        caption = await _caption(db, theme_title, headline, subline)

        # Instagram (only when linked): Meta fetches from our public route
        base = (await app_settings.get(db, "public_base_url") or "").rstrip("/")
        ig_status = "skipped"
        if base:
            ig_status = await post_to_instagram(db, f"{base}/social/{fname}", caption)

        # owner's ready-to-post pack on WhatsApp (image + caption text)
        owner_note = {
            "posted": "✅ Instagram par post ho gaya.",
            "skipped": "ℹ️ Instagram abhi linked nahi (Settings mein IG id/token daalo).",
        }.get(ig_status, f"⚠️ Instagram post fail: {ig_status[:120]}")
        try:
            await send_image(
                db, to_phone=manager_phone(), file_path=str(path),
                mime_type="image/png", caption=f"📣 Aaj ka poster — {theme_title}",
                local_url=f"/social/{fname}", sent_by="bot",
            )
            await send_message(
                db, to_phone=manager_phone(),
                text=(
                    f"{owner_note}\n\n📋 Caption (copy karke Google Business "
                    f"par bhi daal do — 10 second):\n\n{caption}"
                ),
            )
            delivered = "sent"
        except SendError:
            log.warning("social_pack_not_sent_to_owner")
            delivered = "not_sent"

        await audit.record(
            actor_role="system", actor="marketing", action="daily_social",
            args={"theme": theme_title, "ig": ig_status}, result=delivered,
        )
        log.info("daily_social_done", ig=ig_status, owner=delivered)
        return ig_status
