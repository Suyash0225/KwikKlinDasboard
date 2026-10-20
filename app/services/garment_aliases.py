"""What customers and staff call a garment or service, mapped to the rate card.

"2 chadar", "5 shirt press", "kambal dry clean" — the rate card says
Bedsheet, Iron, Blanket. Two layers:

* BUILTIN: common Hindi/Hinglish names and misspellings, the same for
  every shop.
* learned: per shop (setting "garment_aliases"), filled automatically when
  staff correct a bill line the rate card did not recognise (see learn()).

Everything is compared as a match key (key()): lowercase letters/digits,
a plural "s" dropped. An alias only helps when its target exists on that
shop's rate card; it never invents a row.
"""

from __future__ import annotations

import re

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# alias match key -> canonical match key
BUILTIN: dict[str, str] = {
    # spelling
    "pent": "pant", "paint": "pant", "pantt": "pant", "patloon": "pant", "patlun": "pant",
    "sari": "saree", "sadi": "saree", "saari": "saree",
    "jean": "jeans", "kurtha": "kurta", "shart": "shirt", "sirt": "shirt",
    "dryclean": "dryclean", "drycleaning": "dryclean", "dc": "dryclean",
    # Hindi/Hinglish garment names
    "chadar": "bedsheet", "chaddar": "bedsheet", "chadder": "bedsheet", "chador": "bedsheet",
    "bedshit": "bedsheet", "bedcover": "bedsheet",
    "kambal": "blanket", "kammal": "blanket",
    "rajai": "razai", "rajaai": "razai", "razaai": "razai", "rajayi": "razai",
    "parda": "curtain", "parde": "curtain", "pardaa": "curtain",
    "pyjama": "pajama", "payjama": "pajama", "pajami": "pajama",
    "lehnga": "lehenga", "lahenga": "lehenga", "lengha": "lehenga",
    "chunni": "dupatta", "chunri": "dupatta", "odhni": "dupatta",
    "jaket": "jacket", "sweter": "sweater", "swetar": "sweater",
    "takiya": "pillowcover", "takiyakavar": "pillowcover", "pillow": "pillowcover",
    # services
    "press": "iron", "istri": "iron", "istari": "iron", "ironing": "iron", "isteri": "iron",
    "dhulai": "wash", "dhulaai": "wash", "dhulwai": "wash", "washing": "wash",
}

_MAX_LEARNED = 200


def raw_key(word: str | None) -> str:
    """Match key without alias resolution."""
    k = re.sub(r"[^a-z0-9]+", "", (word or "").lower())
    if len(k) > 3 and k.endswith("s"):
        k = k[:-1]
    return k


def key(word: str | None, aliases: dict[str, str] | None = None) -> str:
    """Match key, with aliases (BUILTIN when none are given) resolved."""
    k = raw_key(word)
    table = BUILTIN if aliases is None else aliases
    return table.get(k, k)


async def load(db: AsyncSession) -> dict[str, str]:
    """BUILTIN plus this shop's learned aliases (learned wins)."""
    from app.services import app_settings

    try:
        learned = await app_settings.get(db, "garment_aliases") or {}
    except Exception:
        log.exception("garment_aliases_load_failed")
        learned = {}
    return {**BUILTIN, **{k: v for k, v in learned.items() if isinstance(v, str)}}


async def learn(db: AsyncSession, alias: str, garment: str, *, rate_keys: set[str]) -> bool:
    """Remember that this shop's `alias` means rate-card `garment`.

    Refused when it would shadow a real rate-card name, is too short to be
    a word, or points at something that is not on the card.
    """
    from app.services import app_settings

    a, target = raw_key(alias), raw_key(garment)
    if len(a) < 3 or a == target or a in rate_keys or target not in rate_keys:
        return False
    learned = dict(await app_settings.get(db, "garment_aliases") or {})
    if learned.get(a) == target:
        return False
    learned[a] = target
    if len(learned) > _MAX_LEARNED:  # keep the newest
        learned = dict(list(learned.items())[-_MAX_LEARNED:])
    await app_settings.set_value(db, "garment_aliases", learned)
    log.info("garment_alias_learned", alias=a, garment=target)
    return True


def names_by_garment(aliases: dict[str, str]) -> dict[str, list[str]]:
    """canonical key -> the other names for it (for prompt hints)."""
    grouped: dict[str, list[str]] = {}
    for alias, canonical in aliases.items():
        if alias != canonical:
            grouped.setdefault(canonical, []).append(alias)
    return grouped
