"""Score saved AI intent predictions against a versioned JSONL golden dataset.

This module never calls an LLM or sends messages. Feed it predictions captured
from a controlled evaluation run; missing predictions count as failures.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: each JSONL row must be an object")
            rows.append(row)
    return rows


def _intent_matches(case: dict[str, Any], prediction: dict[str, Any]) -> bool:
    expected = case.get("intent_any")
    if expected is None:
        expected = [case.get("intent")]
    if not isinstance(expected, list) or not expected or not all(isinstance(x, str) for x in expected):
        raise ValueError(f"{case.get('id', '<unknown>')}: expected intent or non-empty intent_any is required")
    return prediction.get("intent") in expected


def score_evaluation(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Score one or more attempts per case. Prediction rows require id, intent, language."""
    case_by_id: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Every case requires a non-empty string id")
        if case_id in case_by_id:
            raise ValueError(f"Duplicate case id: {case_id}")
        case_by_id[case_id] = case

    predictions_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        case_id = prediction.get("id")
        if case_id not in case_by_id:
            raise ValueError(f"Prediction references unknown case id: {case_id}")
        if not isinstance(prediction.get("intent"), str) or not isinstance(prediction.get("language"), str):
            raise ValueError(f"Prediction for {case_id} requires string intent and language")
        predictions_by_id[case_id].append(prediction)

    attempts = passed_attempts = 0
    per_case: dict[str, dict[str, Any]] = {}
    per_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "total": 0})
    flaky_cases = 0
    stable_pass_cases = 0

    for case_id, case in case_by_id.items():
        rows = predictions_by_id.get(case_id, [])
        # A missing prediction is a failure rather than silently disappearing.
        outcomes = [
            _intent_matches(case, row) and row.get("language") == case.get("language")
            for row in rows
        ]
        if not rows:
            outcomes = [False]
        attempts += len(outcomes)
        passed_attempts += sum(outcomes)
        if any(outcomes) and not all(outcomes):
            flaky_cases += 1
        if all(outcomes):
            stable_pass_cases += 1
        per_case[case_id] = {
            "attempts": len(outcomes),
            "passed": sum(outcomes),
            "stable": all(outcomes),
            "flaky": any(outcomes) and not all(outcomes),
        }
        for tag in case.get("tags", []):
            per_tag[tag]["total"] += len(outcomes)
            per_tag[tag]["passed"] += sum(outcomes)

    total_cases = len(case_by_id)
    return {
        "case_count": total_cases,
        "attempt_count": attempts,
        "passed_attempts": passed_attempts,
        "attempt_accuracy": passed_attempts / attempts if attempts else 0.0,
        "stable_case_rate": stable_pass_cases / total_cases if total_cases else 0.0,
        "flaky_case_count": flaky_cases,
        "flaky_rate": flaky_cases / total_cases if total_cases else 0.0,
        "missing_prediction_count": sum(1 for case_id in case_by_id if not predictions_by_id.get(case_id)),
        "per_tag": {
            tag: {
                "passed": values["passed"],
                "total": values["total"],
                "accuracy": values["passed"] / values["total"] if values["total"] else 0.0,
            }
            for tag, values in sorted(per_tag.items())
        },
        "cases": per_case,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evals/data/intent_cases.jsonl")
    parser.add_argument("--predictions", required=True, help="JSONL rows: id, intent, language; repeat ids to measure stability")
    parser.add_argument("--output", help="Optional path to write the JSON report")
    parser.add_argument("--min-accuracy", type=float, default=0.90)
    parser.add_argument("--min-stable-rate", type=float, default=0.85)
    args = parser.parse_args(argv)
    try:
        report = score_evaluation(_read_jsonl(args.cases), _read_jsonl(args.predictions))
    except (OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    passed = (
        report["attempt_accuracy"] >= args.min_accuracy
        and report["stable_case_rate"] >= args.min_stable_rate
        and report["missing_prediction_count"] == 0
    )
    print(
        f"QUALITY_GATE={'PASS' if passed else 'FAIL'} "
        f"(accuracy>={args.min_accuracy:.2f}, stable>={args.min_stable_rate:.2f}, missing=0)",
        file=sys.stderr,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
