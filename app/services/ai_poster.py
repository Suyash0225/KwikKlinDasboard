"""Gemini-generated artwork with deterministic Kwik Klin branding and legible copy.

Only the background is AI-generated; the actual logo and all text are composited
locally so brand identity and policy-sensitive copy do not depend on image text.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.config import settings

MODEL_DEFAULT = "gemini-3.1-flash-image"
IMAGE_TIMEOUT_SECONDS = 75
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class AIPosterError(RuntimeError):
    """AI artwork could not be generated or validated."""


def _image_part(payload: dict[str, Any]) -> tuple[str, bytes] | None:
    for candidate in payload.get("candidates") or []:
        for part in ((candidate.get("content") or {}).get("parts") or []):
            data = part.get("inlineData") or part.get("inline_data") or {}
            encoded = data.get("data")
            mime = str(data.get("mimeType") or data.get("mime_type") or "")
            if encoded and mime in {"image/png", "image/jpeg", "image/webp"}:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                    if 10 * 1024 <= len(raw) <= MAX_IMAGE_BYTES:
                        with Image.open(io.BytesIO(raw)) as image:
                            image.verify()
                        return mime, raw
                except Exception:
                    continue
    return None


async def generate_background(prompt: str, out_path: Path) -> Path:
    """Generate one square lifestyle background; never put copy/logo in model hands."""
    api_key = str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        raise AIPosterError("GEMINI_API_KEY is not configured")
    model = str(getattr(settings, "GEMINI_IMAGE_MODEL", "") or MODEL_DEFAULT).strip()
    if not model or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in model):
        raise AIPosterError("GEMINI_IMAGE_MODEL is invalid")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_SECONDS) as client:
            response = await client.post(url, params={"key": api_key}, json=request)
        if response.status_code != 200:
            # Never include request URL or key in logs/errors.
            raise AIPosterError(f"Gemini image API returned HTTP {response.status_code}")
        part = _image_part(response.json() or {})
        if not part:
            raise AIPosterError("Gemini returned no valid image")
        _, raw = part
        with Image.open(io.BytesIO(raw)) as image:
            image.convert("RGB").save(out_path, format="PNG", optimize=True)
        return out_path
    except AIPosterError:
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise AIPosterError(f"Gemini image generation failed: {type(exc).__name__}") from exc


def create_fallback_background(out_path: Path) -> Path:
    """Create a clean navy/cyan fabric-inspired background without external APIs."""
    image = Image.new("RGB", (1080, 1080), "#09203f")
    pixels = image.load()
    top = (8, 31, 59)
    bottom = (0, 139, 164)
    for y in range(1080):
        ratio = y / 1079
        shade = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        for x in range(1080):
            pixels[x, y] = shade
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((640, 220, 1250, 830), fill=(110, 226, 241, 28), outline=(110, 226, 241, 70), width=6)
    draw.ellipse((-220, 620, 430, 1270), fill=(255, 255, 255, 15), outline=(255, 255, 255, 35), width=5)
    draw.rounded_rectangle((650, 80, 980, 140), radius=30, fill=(255, 255, 255, 20))
    image.save(out_path, "PNG", optimize=True)
    return out_path


def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf", "arialbd.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "arial.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in str(text or "").split():
        trial = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), trial, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines[:3]


def compose_poster(
    background_path: Path,
    logo_path: Path,
    out_path: Path,
    theme: str,
    headline: str,
    subline: str,
) -> Path:
    """Overlay true logo and policy-safe copy on generated art; never print contact details."""
    logo_path = Path(logo_path)
    if not logo_path.is_file():
        raise AIPosterError("Kwik Klin logo asset is missing")
    with Image.open(background_path) as source:
        background = source.convert("RGB")
    w = h = 1080
    scale = max(w / background.width, h / background.height)
    background = background.resize((int(background.width * scale), int(background.height * scale)), Image.Resampling.LANCZOS)
    left = (background.width - w) // 2
    top = (background.height - h) // 2
    canvas = background.crop((left, top, left + w, top + h)).convert("RGBA")
    # Keep text readable regardless of the generated image's colours.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, w, 250), fill=(9, 28, 54, 218))
    od.rectangle((0, 790, w, h), fill=(9, 28, 54, 230))
    od.rounded_rectangle((54, 295, 1026, 770), radius=34, fill=(255, 255, 255, 224))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)
    draw.text((58, 44), "KWIK KLIN", font=_font(74), fill=(255, 255, 255, 255))
    draw.text((62, 142), "LAUNDRY  •  DRY CLEAN  •  VARANASI", font=_font(25, False), fill=(110, 226, 241, 255))
    if logo_path.is_file():
        try:
            with Image.open(logo_path) as source_logo:
                logo = source_logo.convert("RGBA")
            logo.thumbnail((138, 138), Image.Resampling.LANCZOS)
            canvas.alpha_composite(logo, (w - logo.width - 52, 36))
        except OSError:
            pass
    draw = ImageDraw.Draw(canvas)
    draw.text((92, 330), str(theme or "LAUNDRY CARE").upper()[:42], font=_font(27), fill=(0, 125, 155, 255))
    headline_font = _font(65)
    lines = _wrap(draw, headline, headline_font, 875)
    y = 390
    for line in lines:
        draw.text((92, y), line, font=headline_font, fill=(9, 28, 54, 255), stroke_width=0)
        y += 82
    sub_font = _font(29, False)
    sub_lines = _wrap(draw, subline, sub_font, 875)
    y = min(665, max(y + 5, 665 - 38 * len(sub_lines)))
    for line in sub_lines:
        draw.text((92, y), line, font=sub_font, fill=(38, 62, 78, 255))
        y += 38
    draw.text((60, 840), "Doorstep laundry care in Varanasi", font=_font(34, False), fill=(210, 244, 249, 255))
    draw.text((60, 906), "Explore Kwik Klin online", font=_font(45), fill=(110, 226, 241, 255))
    canvas.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path


async def create_branded_campaign_poster(
    out_path: Path,
    logo_path: Path,
    theme: str,
    headline: str,
    subline: str,
) -> str:
    """Create a campaign poster with Gemini artwork and locally rendered brand copy.

    Returns 'gemini' when image generation succeeds, otherwise 'fallback'.
    The fallback keeps campaigns usable when the image API is unavailable; the
    caller still validates the final file before exposing it to the campaign UI.
    """
    import tempfile

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kwikklin-campaign-art-") as tmp:
        background = Path(tmp) / "background.png"
        # Keep model output purely visual. Offer copy, logo, prices and contact
        # details are always rendered locally from the sanitized draft.
        # Use the actual campaign theme to direct the art, rather than returning
        # the same generic folded-laundry image for every festival or occasion.
        # These values are visual guidance only; never let the image model invent copy.
        theme_text = " ".join(str(theme or "").split())[:100]
        headline_text = " ".join(str(headline or "").split())[:180]
        subline_text = " ".join(str(subline or "").split())[:240]
        theme_lower = theme_text.casefold()
        visual_direction = (
            "Dussehra/Vijayadashami-inspired premium festive scene: warm marigold "
            "orange and gold accents, elegant diya glow, tasteful festive bokeh, "
            "a subtle celebratory Indian textile setting, no deity or religious figure"
            if any(term in theme_lower for term in ("dussehra", "dussera", "vijayadashami", "vijay dashami"))
            else "Diwali-inspired premium festive scene with warm diya-like golden light and elegant festive bokeh"
            if any(term in theme_lower for term in ("diwali", "deepavali"))
            else "Holi-inspired premium scene with refined powder-color accents on folded fabric, clean and sophisticated"
            if "holi" in theme_lower
            else "Raksha Bandhan-inspired premium festive textile scene with tasteful rakhi colors and warm celebratory details"
            if any(term in theme_lower for term in ("raksha bandhan", "rakshabandhan", "rakhi"))
            else "Navratri-inspired premium festive textile scene with rich jewel tones and tasteful festive decor"
            if "navratri" in theme_lower
            else "season-appropriate, theme-specific premium commercial art direction inspired by the campaign subject"
        )
        prompt = (
            "Create a premium, photorealistic square 1:1 advertising background for "
            "Kwik Klin, an Indian laundry and dry-cleaning service. Campaign theme: "
            + theme_text + ". Intended message concept: " + headline_text + ". "
            "Supporting context: " + subline_text + ". Visual direction: "
            + visual_direction + ". Integrate fresh, beautifully folded garments "
            "and luxurious textile detail naturally into the scene. Use polished "
            "art direction, controlled lighting, strong composition, clear focal "
            "hierarchy, generous negative space for typography to be overlaid later, "
            "and restrained navy/cyan brand accents where appropriate. Make the "
            "visual clearly fit this particular occasion, not a generic laundry stock image. "
            "Background artwork only. No text, letters, numbers, logo, "
            "watermark, phone, signage, price, discount, coupon, offer badge, or UI. "
            "All text and the real brand logo will be added locally after generation."
        )
        try:
            await generate_background(prompt, background)
            source = "gemini"
        except Exception:
            # Fail closed to a deterministic branded background, not a missing image.
            create_fallback_background(background)
            source = "fallback"
        compose_poster(
            background, Path(logo_path), out_path,
            str(theme or "Laundry Care")[:42],
            str(headline or "Fresh clothes, less hassle")[:180],
            str(subline or "Laundry and dry cleaning services in Varanasi")[:700],
        )
    return source
