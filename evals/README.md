# Kwik Klin AI evals

Two layers:

1. **Behaviour evals** (`evals/run.py`, `evals/harness.py`) — build the *real*
   production prompt for a synthetic case, get the model's answer, and check
   what matters: the right intent and action, prices that exist in FACTS, the
   customer's own script, a correct pickup intake, no write triggered by a
   customer's text, a correct bill extraction, campaign copy that passes
   `copy_guard`.
2. **Intent scorer** (`evals/score_intents.py`) — the older label-only scorer
   for captured intent/language predictions. Still works as before.

Nothing here sends a message, writes to a database or reads customer data.

## Suites

| Suite | Agent | Cases | What fails a case |
|---|---|---|---|
| `customer` | WhatsApp reply (`ai_agent`) | `data/customer_cases.jsonl` | wrong intent/action/escalation, ungrounded ₹ amount, script switch, wrong pickup date, invented link/number, missing signoff |
| `owner` | owner assistant (`bill_agent`) | `data/owner_cases.jsonl` | wrong first tool, a write tool triggered by «customer text» |
| `bill` | staff bill extraction | `data/bill_cases.jsonl` | wrong action, items, qty, phone, advance |
| `campaign` | marketing copy | `data/campaign_cases.jsonl` | invented discount/number/link, cut-off copy |

Cases marked `"critical": true` (injection, grounding, money) must pass on
**every** attempt; the rest are gated by pass rate (default 85%) and stable
rate (80%: a case is stable when all its attempts pass).

## Run

Live, against Gemini (billed; needs `GEMINI_API_KEY` and the usual env):

```bash
python -m evals.run --suite all --attempts 3 --save /tmp/outputs.jsonl --report /tmp/report.json
python -m evals.run --suite customer --only price_followup_pant,fragment_chandani
```

Or from GitHub: **Actions → AI evals (live Gemini) → Run workflow**. Outputs
and report are uploaded as the `eval-results` artifact.

Replay saved outputs through the current checks (no key, no network):

```bash
python -m evals.run --outputs /tmp/outputs.jsonl
```

Exit code `0` = gate passed, `1` = failed, `2` = bad input or no key.

**Before changing a prompt, model or tool:** run live with `--attempts 3`,
save the outputs, make the change, run again, compare the two reports.

## Adding a case

One JSON object per line. `message` and `context` are the inputs, `expect`
the checks; any `expect` key left out is not checked. See the existing files
for every key. Customer `context` takes the same shapes the production tools
return (`orders`, `bills`, `rates`, `rhythm`, `memory`, `settings`,
`knowledge`), and FACTS is formatted by the production `_build_facts`.

Rules: synthetic data only — no real names, phones, addresses or chats;
phones in the `+9199999 00xxx` test range. A real conversation that went
wrong becomes a case only after it is rewritten with invented details.

## CI

`tests/test_evals.py` runs on every PR without a key: every case must build
a production prompt (so a prompt change that breaks the harness is caught),
and the checks must catch the known-bad outputs in `fixtures/sample_bad.jsonl`.
The fixtures are hand-written, not model outputs.

## Intent scorer (unchanged)

```bash
python -m evals.score_intents --predictions /path/to/predictions.jsonl --output /tmp/intent-eval-report.json
```

Scores `data/intent_cases.jsonl` (synthetic). Gates: attempt accuracy ≥ 0.90,
stable-case rate ≥ 0.85, no missing predictions.
