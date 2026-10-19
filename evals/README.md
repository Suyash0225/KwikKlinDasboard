# Kwik Klin AI quality evaluation

This is a **no-send, no-LLM-call** evaluation layer. It scores captured intent/language predictions against a reviewed golden dataset; it does not invoke Gemini, read production customer data, send WhatsApp messages, or publish Google posts.

## Run

1. Capture model outputs in a UTF-8 JSONL file, one row per attempt:

   ```json
   {"id":"ord_hi_01","intent":"ORDER_STATUS","language":"hi"}
   ```

   Repeat an `id` to measure non-determinism. Do not put customer names, phone numbers, order IDs, addresses, or real conversation text into prediction logs.

2. Run the scorer:

   ```bash
   python -m evals.score_intents --predictions /path/to/predictions.jsonl --output /tmp/intent-eval-report.json
   ```

3. Exit code `0` means the quality gate passed; `1` means metrics missed the thresholds; `2` means the input files are invalid.

Default gates are attempt accuracy >= 0.90, stable-case rate >= 0.85, and zero missing predictions. Tune only after reviewing labelled cases with the owner/product reviewer.

## Dataset policy

- `data/intent_cases.jsonl` contains synthetic, non-customer examples.
- Ambiguous cases use `intent_any`; both acceptable intents count as correct.
- Language is evaluated separately from intent.
- Missing outputs count as failures. Repeated case IDs expose flaky predictions.
- Review per-tag scores; aggregate accuracy can hide failures in complaints, injection attempts, or shorthand.
- Keep labels reviewed and version changes in Git.

## Important limitation

This scorer evaluates intent and language labels only. It is not a substitute for integration tests that prove database facts, tool permissions, tenant isolation, complaint escalation, consent checks, or real WhatsApp/Google delivery. Add separate assertions for those behaviours before treating a release as production-ready.
