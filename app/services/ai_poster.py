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
