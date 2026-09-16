"""Approved-template texts must match what the code sends."""

import re

from app.services.templates import STANDARD_SPECS, TEMPLATES, build_template, clean_param


def test_every_spec_matches_registry():
    for name, spec in STANDARD_SPECS.items():
        assert name in TEMPLATES, name
        ids = sorted({int(n) for n in re.findall(r"\{\{(\d+)\}\}", spec["body"])})
        assert ids == list(range(1, TEMPLATES[name]["param_count"] + 1)), name
        assert len(spec["samples"]) == len(ids), name
        body = spec["body"].strip()
        # Meta rejects a body that starts or ends with a variable
        assert not body.startswith("{{") and not body.endswith("}}"), name
        assert len(body) <= 1024


def test_rating_buttons_match_webhook_texts():
    texts = [b["text"] for b in STANDARD_SPECS["kk_thankyou_rating"]["buttons"]]
    assert texts == ["⭐ Excellent", "🙂 It was okay", "😞 Needs work"]


def test_params_are_cleaned_for_meta():
    assert clean_param("3 Shirt\n2 Trouser\t") == "3 Shirt · 2 Trouser"
    assert clean_param("a     b") == "a b"
    assert clean_param("") == "-"
    p = build_template("kk_order_ready", ["KK\n1"])
    assert p["components"][0]["parameters"][0]["text"] == "KK · 1"
