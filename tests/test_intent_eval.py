from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.score_intents import score_evaluation, _read_jsonl


ROOT = Path(__file__).resolve().parents[1]


def test_golden_dataset_has_minimum_coverage_per_intent() -> None:
    cases = _read_jsonl(ROOT / "evals/data/intent_cases.jsonl")
    assert len(cases) >= 30
    counts: dict[str, int] = {}
    for case in cases:
        intents = case.get("intent_any") or [case.get("intent")]
        for intent in intents:
            counts[intent] = counts.get(intent, 0) + 1
        assert case.get("tags")
        assert case.get("language") in {"hi", "en"}
    for intent in ("ORDER_STATUS", "NEW_ORDER", "PRICE_QUERY", "BILL_REQUEST", "COMPLAINT", "GREETING", "OTHER"):
        assert counts[intent] >= 5


def test_score_tracks_accuracy_stability_flakiness_and_missing_cases() -> None:
    cases = [
        {"id": "a", "intent": "ORDER_STATUS", "language": "hi", "tags": ["order"]},
        {"id": "b", "intent_any": ["PRICE_QUERY", "NEW_ORDER"], "language": "hi", "tags": ["ambiguous"]},
        {"id": "c", "intent": "COMPLAINT", "language": "en", "tags": ["complaint"]},
    ]
    predictions = [
        {"id": "a", "intent": "ORDER_STATUS", "language": "hi"},
        {"id": "a", "intent": "OTHER", "language": "hi"},
        {"id": "b", "intent": "NEW_ORDER", "language": "hi"},
    ]
    report = score_evaluation(cases, predictions)
    assert report["attempt_count"] == 4
    assert report["passed_attempts"] == 2
    assert report["attempt_accuracy"] == 0.5
    assert report["stable_case_rate"] == 1 / 3
    assert report["flaky_case_count"] == 1
    assert report["missing_prediction_count"] == 1
    assert report["per_tag"]["order"]["accuracy"] == 0.5


def test_unknown_prediction_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown case id"):
        score_evaluation(
            [{"id": "known", "intent": "OTHER", "language": "en"}],
            [{"id": "not-known", "intent": "OTHER", "language": "en"}],
        )


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate case id"):
        score_evaluation(
            [
                {"id": "same", "intent": "OTHER", "language": "en"},
                {"id": "same", "intent": "OTHER", "language": "en"},
            ],
            [],
        )


def test_cli_dataset_is_valid_jsonl() -> None:
    path = ROOT / "evals/data/intent_cases.jsonl"
    rows = _read_jsonl(path)
    assert all(isinstance(row.get("msg"), str) and row["msg"] for row in rows)
