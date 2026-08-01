"""Phone number normalization to E.164.

Every phone number in the database is stored in E.164 form: "+919876543210".
Normalize at the boundary (webhook input, admin input) and the rest of the
system never has to think about formats again.

Assumption baked in here: national numbers are 10 digits (true for Indian
mobiles). If the shop ever serves another country, revisit this file.
"""

import re

from app.config import settings

# Characters people type inside numbers that we silently remove.
_SEPARATORS = re.compile(r"[\s\-\.\(\)]")

# E.164: max 15 digits. We also refuse anything under 8 as garbage.
_MIN_DIGITS = 8
_MAX_DIGITS = 15

# Indian mobile numbers start with 6, 7, 8 or 9.
_INDIA_MOBILE_FIRST_DIGITS = "6789"


def normalize_phone(raw: str, country_code: str | None = None) -> str:
    """Normalize a phone number to E.164 ("+919876543210").

    Accepts the formats real people and the WhatsApp API actually send:
        "9876543210"        -> +919876543210   (local 10-digit)
        "919876543210"      -> +919876543210   (cc prefixed, no +)
        "+91 98765 43210"   -> +919876543210   (formatted)
        "0919876543210"     -> +919876543210   (trunk-prefixed)

    country_code defaults to settings.DEFAULT_COUNTRY_CODE ("91").
    Raises ValueError on anything that can't be confidently normalized —
    callers must catch it and escalate, never guess.
    """
    cc = country_code if country_code is not None else settings.DEFAULT_COUNTRY_CODE

    if not isinstance(raw, str):
        raise ValueError(f"phone must be a string, got {type(raw).__name__}")

    cleaned = _SEPARATORS.sub("", raw.strip())
    if not cleaned:
        raise ValueError("phone is empty")

    has_plus = cleaned.startswith("+")
    digits = cleaned[1:] if has_plus else cleaned

    if not digits.isdigit():
        raise ValueError(f"phone contains non-digits: {raw!r}")

    if has_plus:
        # Caller gave an explicit country code — trust it, just validate.
        number = digits
    else:
        # Strip trunk/international dialing prefixes: "0", "00", "0091"...
        digits = digits.lstrip("0")
        if len(digits) == 10:
            # Local number: prepend the default country code.
            number = cc + digits
        elif digits.startswith(cc) and len(digits) == len(cc) + 10:
            # Already has the country code, just missing the "+".
            number = digits
        else:
            raise ValueError(f"can't determine country code for: {raw!r}")

    if not (_MIN_DIGITS <= len(number) <= _MAX_DIGITS):
        raise ValueError(f"phone has invalid length ({len(number)} digits): {raw!r}")

    # India-specific sanity check: a 12-digit number starting "91" is an
    # Indian mobile, and those never start with 0-5. Catches garbage like
    # order IDs or landline typos being mistaken for mobiles.
    if number.startswith("91") and len(number) == 12:
        if number[2] not in _INDIA_MOBILE_FIRST_DIGITS:
            raise ValueError(f"not a valid Indian mobile number: {raw!r}")

    return f"+{number}"
