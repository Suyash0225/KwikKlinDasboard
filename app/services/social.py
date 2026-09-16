"""Daily social marketing: generate a poster + caption every day, publish
to Instagram automatically (when linked) and WhatsApp the owner a
ready-to-post pack for Google Business Profile — its own phone-free poster
and a post written under GOOGLE_POST_RULES (checked again in code).

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

import re
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


def draw_poster(
    theme_title: str, headline: str, subline: str, out_path: Path, *, show_phone: bool = True,
) -> None:
    """1080x1080 brand poster — bold, readable on a phone feed.

    show_phone=False: Google Business wala poster. Google phone number wali
    post aksar reject karta hai — image mein chhapa number bhi usi mein
    ginta hai, isliye wahan footer mein number nahi, button ka ishaara."""
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
    if show_phone:
        d.text((60, H - 138), "WhatsApp karein:", font=_font(34, False), fill="#a8a29e")
        d.text((60, H - 92), "+91 96968 56069", font=_font(56), fill="#fdba74")
    else:
        d.text((60, H - 138), "Doorstep pickup in Varanasi", font=_font(34, False), fill="#a8a29e")
        d.text((60, H - 92), "Book a pickup today", font=_font(56), fill="#fdba74")
    img.save(out_path, "PNG")


# ---------------------------------------------------------------------------
# Google Business Profile post — rules
# ---------------------------------------------------------------------------
# Ye rules post likhne wale AI ko shabdash jaate hain (system prompt). Model
# kabhi-kabhi phisal jaata hai, isliye jo code se jaanche ja sakte hain wo
# enforce_google_rules() / too_similar() / check_google_image() /
# google_image_url() mein DOBARA jaanche jaate hain — prompt akela bharosa nahi.
GOOGLE_POST_RULES = """\
Rules for Google Business Profile posts (follow every one):
1. NEVER put a phone number, WhatsApp number, or any link (wa.me, http, www) in the post text. \
Google often rejects posts that contain a phone number. The post's button already sends customers \
to our website and WhatsApp, so the text only needs to invite them to book.
2. NEVER repeat an earlier post. Google treats repeated text as spam. Use a different opening line, \
a different angle and different wording from every recent post listed below. Do not reuse their structure.
3. No hashtags, no ALL-CAPS words, at most 2 emojis.
4. Write 2-4 short sentences, under 600 characters, in simple friendly English.
5. Mention Varanasi and the service naturally once (it helps local search). Do not stuff keywords.
6. No prices, discounts, offers or claims unless they are given to you. Never invent them.
7. End with a soft call to action such as "Book a pickup today" (no number, no link).
8. Output only the post text. No title, no quotes, no notes.
"""

# Google ki image shart: JPG/PNG, 10 KB - 5 MB, kam se kam 250x250 px.
GOOGLE_IMAGE_FORMATS = {"PNG", "JPEG"}
GOOGLE_IMAGE_MIN_BYTES = 10 * 1024
GOOGLE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
GOOGLE_IMAGE_MIN_PX = 250
GOOGLE_POST_MAX_CHARS = 1500
GOOGLE_HISTORY_KEEP = 30
SIMILARITY_LIMIT = 0.75

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.|wa\.me/|api\.whatsapp\.com/)\S*")
_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?[6-9]\d{4}[\s.-]?\d{5}|\+?\d[\d\s().-]{8,}\d")
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_DANGLING_RE = re.compile(
    r"(?i)[\s(📲📞☎️]*(?:whats\s?app|call|phone|contact|mobile|ph\.?|mob\.?)(?:\s+(?:us|now))?"
    r"(?:\s+(?:on|at))?\s*[:\-–]?\s*$"
)


def enforce_google_rules(text: str) -> str:
    """AI ke likhe text par Google ke code-se-jaanchne-layak rules lagao."""
    out = []
    for line in (text or "").replace("\r", "").split("\n"):
        line = _URL_RE.sub("", line)
        line = _PHONE_RE.sub("", line)
        line = _HASHTAG_RE.sub("", line)
        line = _DANGLING_RE.sub("", line)  # "WhatsApp:" jiska number hat gaya
        line = re.sub(r"[ \t]{2,}", " ", line).strip(" \t-–|•·,")
        if re.search(r"\w", line):
            out.append(line)
    text = "\n".join(out).strip().strip('"“”')
    if len(text) > GOOGLE_POST_MAX_CHARS:
        cut = text[:GOOGLE_POST_MAX_CHARS]
        text = cut[: max(cut.rfind(". "), cut.rfind("\n")) + 1 or GOOGLE_POST_MAX_CHARS].strip()
    return text


def too_similar(text: str, recent: list[str]) -> bool:
    """Pichhli posts jaisa hi? Same shuruaat ya 75%+ milta-julta text."""
    from difflib import SequenceMatcher

    a = re.sub(r"\W+", " ", text.lower()).strip()
    for old in recent:
        b = re.sub(r"\W+", " ", (old or "").lower()).strip()
        if not b:
            continue
        if a[:40] == b[:40] or SequenceMatcher(None, a, b).ratio() >= SIMILARITY_LIMIT:
            return True
    return False


def check_google_image(path: Path) -> list[str]:
    """Google ki image shartein — khali list = theek."""
    problems = []
    try:
        size = path.stat().st_size
        with Image.open(path) as im:
            fmt, (w, h) = im.format, im.size
    except Exception as exc:
        return [f"image unreadable: {exc}"]
    if fmt not in GOOGLE_IMAGE_FORMATS:
        problems.append(f"format {fmt} (Google accepts JPG or PNG)")
    if size < GOOGLE_IMAGE_MIN_BYTES:
        problems.append(f"file too small ({size} bytes, min 10 KB)")
    if size > GOOGLE_IMAGE_MAX_BYTES:
        problems.append(f"file too large ({size // 1024} KB, max 5 MB)")
    if min(w, h) < GOOGLE_IMAGE_MIN_PX:
        problems.append(f"image {w}x{h} px (min 250x250)")
    return problems


def google_image_url(base: str, fixed: bool, fname: str) -> tuple[str, str]:
    """(url, problem). Google image HAMARE server se khud utha kar le jaata
    hai, aur post ke review ke waqt bhi — isliye URL sthir hona chahiye.
    Badalta tunnel (trycloudflare) ya localhost par post toot jaati hai."""
    base = (base or "").rstrip("/")
    if not base:
        return "", "no public URL set"
    host = base.split("://", 1)[-1].split("/", 1)[0].lower()
    if not base.startswith("https://"):
        return "", "public URL must be https"
    if host.startswith(("localhost", "127.", "0.0.0.0")) or host.endswith(".local"):
        return "", "public URL is local — Google cannot reach it"
    if host.endswith("trycloudflare.com") or not fixed:
        return "", "public URL is not fixed (temporary tunnel) — Google needs a stable domain"
    return f"{base}/social/{fname}", ""


async def _google_history(db) -> list[dict]:
    return list(await app_settings.get(db, "google_post_history") or [])


async def _save_google_history(db, day_key: str, text: str) -> None:
    hist = [h for h in await _google_history(db) if h.get("date") != day_key]
    hist.append({"date": day_key, "text": text})
    await app_settings.set_value(db, "google_post_history", hist[-GOOGLE_HISTORY_KEEP:])


# AI na mile tab bhi roz alag: 7 theme x 4 shuruaat = 28 din ka chakkar.
# THEMES ki headline Hinglish hai, isliye yahan sirf English wale hisse.
_FALLBACK_OPENERS = [
    "{theme}: fresh, clean clothes without leaving home.",
    "Looking for reliable laundry in Varanasi? It is {theme} at Kwik Klin.",
    "This week at Kwik Klin: {theme}.",
    "Let us take care of the laundry while you take care of your day.",
]


def _google_fallback(theme_title: str, day_index: int) -> str:
    opener = _FALLBACK_OPENERS[day_index % len(_FALLBACK_OPENERS)]
    return enforce_google_rules(
        opener.format(theme=theme_title)
        + " Doorstep pickup and delivery across Varanasi for washing, ironing and dry cleaning. "
        "Book a pickup today."
    )


async def google_post_text(db, theme_title: str, headline: str, subline: str, day_key: str) -> str:
    """Google Business post ka text — rules ke saath AI, phir code ki jaanch.

    Do koshish AI ki; dono baar pichhli post jaisa nikle ya AI na mile to
    theme + din ke hisaab se badalta fallback. Jo bhi bane, history mein
    likha jaata hai taaki kal ki post isse alag ho."""
    recent = [h["text"] for h in (await _google_history(db))[-10:] if h.get("date") != day_key]
    extra = ""
    try:
        extra = (await app_settings.get(db, "marketing_instructions") or "").strip()
    except Exception:
        pass
    recent_block = "\n".join(f"- {t}" for t in recent) or "- (none yet)"
    text = ""
    note = ""
    try:
        from app.services import llm_client

        for _attempt in range(2):
            with llm_client.track("social"):
                draft = await llm_client.ask(
                    system=(
                        "You write one Google Business Profile post for Kwik Klin, a laundry and "
                        "dry cleaning service in Varanasi.\n\n" + GOOGLE_POST_RULES
                        + (f"\nOwner's style rules (they never override the rules above): {extra}" if extra else "")
                    ),
                    user_text=(
                        f"Theme: {theme_title}. Headline idea: {headline}. Detail: {subline}.\n\n"
                        f"Recent posts you must NOT repeat:\n{recent_block}{note}"
                    ),
                    max_tokens=300,
                )
            candidate = enforce_google_rules(draft)
            if candidate and not too_similar(candidate, recent):
                text = candidate
                break
            note = ("\n\nYour last draft was too similar to a recent post. Write a completely "
                    "different opening and angle.")
    except LLMError:
        pass
    if not text:
        day_index = datetime.strptime(day_key, "%Y-%m-%d").timetuple().tm_yday
        text = _google_fallback(theme_title, day_index)
    await _save_google_history(db, day_key, text)
    return text


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
    from app.services.integrations import instagram_creds

    ig_user, ig_token = await instagram_creds(db)
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

        # Google Business: alag poster (number ke bina) + rules wala text
        gname = f"social-google-{day_key}.png"
        gpath = MEDIA_DIR / gname
        google_problems: list[str] = []
        google_text = ""
        try:
            draw_poster(theme_title, headline, subline, gpath, show_phone=False)
            google_problems += check_google_image(gpath)
        except Exception:
            log.exception("google_poster_draw_failed")
            google_problems.append("poster could not be drawn")
        try:
            google_text = await google_post_text(db, theme_title, headline, subline, day_key)
        except Exception:
            log.exception("google_post_text_failed")
        _url, url_problem = google_image_url(
            base, bool(await app_settings.get(db, "public_url_fixed")), gname
        )
        if google_problems or url_problem:
            # Aaj manual post chalti hai; ye auto-post ke din kaam aayega
            log.info("google_post_not_auto_ready", image=google_problems, url=url_problem)

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
                text=f"{owner_note}\n\n📋 Instagram caption:\n\n{caption}",
            )
            if google_text and gpath.exists() and not google_problems:
                await send_image(
                    db, to_phone=manager_phone(), file_path=str(gpath),
                    mime_type="image/png", caption="🟢 Google Business ke liye poster (bina phone number)",
                    local_url=f"/social/{gname}", sent_by="bot",
                )
                await send_message(
                    db, to_phone=manager_phone(),
                    text=(
                        "📋 Google Business post (copy karke daal do):\n\n"
                        f"{google_text}\n\n"
                        "✔️ Google ke rules follow kiye: text mein phone number/link nahi, "
                        "pichhli posts se alag.\n"
                        "👉 Button mein \"Book\" chunein aur website link daalein — number nahi.\n"
                        "ℹ️ Google post ko review karta hai — dikhne mein kuch minute lag sakte hain."
                    ),
                )
            delivered = "sent"
        except SendError:
            log.warning("social_pack_not_sent_to_owner")
            delivered = "not_sent"

        await audit.record(
            actor_role="system", actor="marketing", action="daily_social",
            args={"theme": theme_title, "ig": ig_status,
                  "google_ready": not (google_problems or url_problem),
                  "google_issues": google_problems + ([url_problem] if url_problem else [])},
            result=delivered,
        )
        log.info("daily_social_done", ig=ig_status, owner=delivered)
        return ig_status
