"""Owner ka niyam: customer ko jaane wala har automated message AI ke naam
se sign hota hai, ek halki 'galti mumkin hai' line ke saath.

Signing whatsapp.send_message ke andar hota hai (single choke point) —
ye tests us helper ke niyam pakka karte hain.
"""

from app.services.whatsapp import AI_NOTE, AI_SIGNATURE, sign_ai


def test_sign_appends_bold_sign_then_small_italic_note() -> None:
    out = sign_ai("Namaste ji, order taiyar hai")
    assert out.startswith("Namaste ji, order taiyar hai\n\n")
    assert AI_SIGNATURE in out
    assert AI_NOTE in out
    # sign bold (*...*), note italic (_..._) — note kabhi sign se upar nahi
    assert AI_SIGNATURE.startswith("*") and AI_SIGNATURE.endswith("*")
    assert AI_NOTE.startswith("_") and AI_NOTE.endswith("_")
    assert out.index(AI_SIGNATURE) < out.index(AI_NOTE)


def test_sign_never_doubles() -> None:
    once = sign_ai("hello")
    assert sign_ai(once) == once


def test_empty_text_stays_untouched() -> None:
    assert sign_ai("") == ""
