"""Deterministic checks on AI-written marketing copy before it can go out.

The prompts tell the model never to invent a discount, a phone number or a
link, and never to cut off mid-message. A prompt is not a policy gate: on
autopilot nobody reads the copy before hundreds of customers do. These
checks run in code, and a failing draft is replaced by a safe template.
"""

from __future__ import annotations

import re

_LINK_RE = re.compile(
    r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(?:com|in|net|org|app|co|io|link|ly)\b\S*",
    re.I,
)
# 10+ digits, optionally with +, spaces or dashes between them.
_PHONE_RE = re.compile(r"\+?\d(?:[\s-]?\d){9,}")
_MONEY_RE = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b|rupees?\b)",
    re.I,
)


def _amounts(text: str) -> set[str]:
    found = set()
    for match in _MONEY_RE.finditer(text or ""):
        raw = (match.group(1) or match.group(2) or "").replace(",", "")
        try:
            found.add(format(float(raw), "g"))
        except ValueError:
            continue
    return found


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def copy_problems(
    copy: str,
    *,
    allowed_text: str = "",
    must_include: str = "",
    min_chars: int = 0,
) -> list[str]:
    """Why this copy must not be sent as-is; empty means it passed.

    allowed_text holds what the copy may legitimately repeat (the offer, the
    shop's own contact number). must_include is a phrase a complete message
    ends with (the shop name); its absence means the model was cut off.
    """
    problems: list[str] = []
    text = copy or ""
    allowed_lower = (allowed_text or "").casefold()

    for link in _LINK_RE.findall(text):
        if link.casefold() not in allowed_lower:
            problems.append(f"link not in offer: {link[:60]}")

    allowed_digits = _digits(allowed_text)
    for phone in _PHONE_RE.findall(text):
        if _digits(phone) not in allowed_digits:
            problems.append(f"phone number not supplied: {phone[:20]}")

    invented = _amounts(text) - _amounts(allowed_text)
    if invented:
        problems.append("amount/discount not in offer: " + ", ".join(sorted(invented)))

    if min_chars and len(text.strip()) < min_chars:
        problems.append("too short — probably cut off")
    if must_include and must_include.casefold() not in text.casefold():
        problems.append(f"missing '{must_include}' — probably cut off")
    return problems
