"""Run the agent behaviour evals.

Live (real Gemini, nothing sent or written; needs GEMINI_API_KEY):

    python -m evals.run --suite all --attempts 3 --save /tmp/outputs.jsonl

Replay saved outputs through the same checks (no key, no network):

    python -m evals.run --suite all --outputs /tmp/outputs.jsonl

Exit code 0 = gate passed, 1 = gate failed, 2 = bad input/config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals import harness


async def _live(suites: list[str], attempts: int, concurrency: int, only: set[str]) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    jobs = []

    async def one(suite: str, case: dict, attempt: int) -> dict:
        request = await harness.build_request(suite, case)
        async with sem:
            try:
                output = await harness.call_live(request)
                error = None
            except Exception as exc:  # an API error is a failed attempt
                output, error = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        return {
            "suite": suite, "id": case["id"], "attempt": attempt, "model": request["model"],
            "output": output, "error": error, "_case": case, "_context": request["context"],
        }

    for suite in suites:
        for case in harness.load_cases(suite):
            if only and case["id"] not in only:
                continue
            for attempt in range(attempts):
                jobs.append(one(suite, case, attempt))
    return await asyncio.gather(*jobs)


async def _replay(suites: list[str], path: str, only: set[str]) -> list[dict]:
    cases = {(s, c["id"]): c for s in suites for c in harness.load_cases(s)}
    rows = []
    for saved in harness.read_jsonl(path):
        key = (saved.get("suite"), saved["id"])
        if key[0] not in suites or (only and key[1] not in only):
            continue
        if key not in cases:
            raise ValueError(f"saved output for unknown case {key}")
        case = cases[key]
        # Rebuild the context so the checks see the same facts the model saw.
        request = await harness.build_request(key[0], case)
        rows.append({**saved, "_case": case, "_context": request["context"]})
    seen = {(r["suite"], r["id"]) for r in rows}
    for key, case in cases.items():
        if only and key[1] not in only:
            continue
        if key not in seen:  # a missing output is a failure, not a skip
            rows.append({"suite": key[0], "id": key[1], "attempt": 0, "output": None,
                         "error": "missing output", "_case": case, "_context": {}})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", default="all", choices=["all", *harness.SUITES])
    parser.add_argument("--outputs", help="replay a saved JSONL of outputs instead of calling Gemini")
    parser.add_argument("--save", help="write live outputs here (JSONL) for later replay")
    parser.add_argument("--attempts", type=int, default=1, help="live calls per case (measures flakiness)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument("--min-pass", type=float, default=0.85)
    parser.add_argument("--min-stable", type=float, default=0.80)
    args = parser.parse_args(argv)

    suites = list(harness.SUITES) if args.suite == "all" else [args.suite]
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    try:
        if args.outputs:
            rows = asyncio.run(_replay(suites, args.outputs, only))
        else:
            from app.config import settings

            if not settings.GEMINI_API_KEY:
                print("GEMINI_API_KEY is not set — use --outputs to replay saved outputs.", file=sys.stderr)
                return 2
            rows = asyncio.run(_live(suites, args.attempts, args.concurrency, only))
    except (OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2

    results = []
    for row in rows:
        case = row["_case"]
        failures = [row["error"]] if row.get("error") else harness.check(
            row["suite"], case, row["output"], row["_context"]
        )
        results.append({
            "suite": row["suite"], "id": row["id"], "attempt": row.get("attempt", 0),
            "tags": case.get("tags", []), "critical": bool(case.get("critical")),
            "failures": failures,
        })

    if args.save and not args.outputs:
        with Path(args.save).open("w", encoding="utf-8") as handle:
            for row in rows:
                saved = {k: v for k, v in row.items() if not k.startswith("_")}
                handle.write(json.dumps(saved, ensure_ascii=False) + "\n")

    report = harness.score(results)
    passed, reasons = harness.gate(report, args.min_pass, args.min_stable)
    report["gate"] = {"passed": passed, "reasons": reasons}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")

    for suite, s in report["suites"].items():
        print(
            f"{suite:9} pass {s['pass_rate']:.0%} ({s['passed_attempts']}/{s['attempts']})  "
            f"stable {s['stable_rate']:.0%}  flaky {s['flaky_cases']}  "
            f"critical-fail {s['critical_failures']}"
        )
    for f in report["failures"]:
        flag = "!!" if f["critical"] else "  "
        print(f"{flag} {f['suite']}/{f['id']}#{f['attempt']}: {'; '.join(f['why'])}")
    print(f"QUALITY_GATE={'PASS' if passed else 'FAIL'}" + (f" — {'; '.join(reasons)}" if reasons else ""),
          file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
