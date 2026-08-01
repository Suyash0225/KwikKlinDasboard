"""Tests for app.utils.phone.normalize_phone.

All tests pass an explicit country_code so results don't depend on the
machine's .env. One test at the bottom checks the settings default is wired.
"""

import pytest

from app.config import settings
from app.utils.phone import normalize_phone

E164 = "+919876543210"


# --- the four formats from the spec ---

def test_local_ten_digit() -> None:
    assert normalize_phone("9876543210", country_code="91") == E164


def test_cc_prefixed_without_plus() -> None:
    assert normalize_phone("919876543210", country_code="91") == E164


def test_formatted_with_plus_and_spaces() -> None:
    assert normalize_phone("+91 98765 43210", country_code="91") == E164


def test_trunk_zero_prefixed() -> None:
    assert normalize_phone("0919876543210", country_code="91") == E164


# --- other real-world formats ---

def test_plain_plus_form_is_idempotent() -> None:
    assert normalize_phone(E164, country_code="91") == E164
    # normalizing twice changes nothing
    assert normalize_phone(normalize_phone("9876543210", country_code="91"), country_code="91") == E164


def test_dashes_dots_parens() -> None:
    assert normalize_phone("98765-43210", country_code="91") == E164
    assert normalize_phone("(91) 98765.43210", country_code="91") == E164


def test_leading_trailing_whitespace() -> None:
    assert normalize_phone("  9876543210  ", country_code="91") == E164


def test_double_zero_international_prefix() -> None:
    assert normalize_phone("00919876543210", country_code="91") == E164


def test_local_number_with_single_trunk_zero() -> None:
    assert normalize_phone("09876543210", country_code="91") == E164


# --- invalid input must raise ValueError, never guess ---

@pytest.mark.parametrize(
    "bad",
    [
        "",                      # empty
        "   ",                   # whitespace only
        "hello",                 # letters
        "98765abc10",            # mixed
        "12345",                 # too short
        "9" * 20,                # too long
        "1234567890",            # 10 digits but starts with 1 — not an Indian mobile
        "911234567890",          # cc + invalid first digit
        "+911234567890",         # same, with plus
        "98765 4321",            # 9 digits
        "+",                     # bare plus
        "98-76-54",              # too short after cleaning
    ],
)
def test_invalid_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(bad, country_code="91")


def test_non_string_raises() -> None:
    with pytest.raises(ValueError):
        normalize_phone(9876543210, country_code="91")  # type: ignore[arg-type]


# --- settings wiring ---

def test_default_country_code_comes_from_settings() -> None:
    expected = f"+{settings.DEFAULT_COUNTRY_CODE}9876543210"
    assert normalize_phone("9876543210") == expected
