# AI Growth Manager: implementation and release plan

## Goal

Turn Kwik Klin's existing Gemini owner agent into a measurable business workflow without adding another autonomous agent or duplicating existing attribution, consent, budget, and outbound-policy services.

## Existing capabilities to reuse

- Owner tools in `app/services/agent_tools.py`: orders, customer history, tasks, expenses, money, cost analysis, garment demand, marketing draft creation, and the new read-only `growth_review` cohort summary plus `ai_health` aggregate observability report.
- Central Gemini client and budget controls in `app/services/llm_client.py` and `app/services/quota.py`.
- Deterministic action rules in `app/services/action_policy.py`.
- Shared WhatsApp promotional policy in `app/services/outbound_policy.py`.
- Lead/campaign attribution and collected/billed revenue reporting in `app/routers/agent_admin.py` and existing dashboard UI.
- GA4 source events and UTM handling described in `docs/AI_LEAD_GENERATION_IMPLEMENTATION.md`.
- FAQ/correction/document retrieval in the existing agent training system.

Do not build duplicate tables, a second LLM client, or a separate marketing sender unless a reviewed gap proves it necessary.

## Implemented in this change

- Adds `growth_review(7d|30d)` as a read-only owner tool. It summarizes orders created in the selected window, billed total, paid-to-date against that order cohort, outstanding balance, and lead counts/current converted stage by source.
- Adds `ai_health(7d|30d)` as a read-only owner tool reporting aggregate AI calls, failures, latency, token volume, and failure rate by purpose without customer identifiers or message content.
- The tool explicitly labels cohort metrics so it does not misrepresent paid-to-date as money collected during the selected window. It does not send messages or create campaigns.
- Adds an offline intent/language evaluation scorer and 36 synthetic golden cases. The scorer measures attempt accuracy, stable-case rate, flakiness, missing outputs, and per-tag accuracy without calling an LLM.

## Growth workflow

1. **Read verified facts.** Use backend orders, payments, campaign recipients, leads and recorded expenses. Treat clicks and generated drafts as intent, not sales.
2. **Generate recommendations.** Suggest repeat-customer recovery, service-demand opportunities, overdue follow-ups and campaign candidates. State the time window and evidence behind each recommendation.
3. **Validate deterministically.** Re-check tenant scope, opt-out/consent, customer eligibility, frequency caps, quiet hours, offer limits, budget and campaign approval immediately before send.
4. **Require owner approval.** The model may create a draft, but must not independently send a promotional campaign or invent a discount.
5. **Measure outcomes.** Link source/campaign to confirmed orders and collected payments using existing attribution. Report unattributed conversions separately.
6. **Learn from results.** Compare segments and campaign outcomes using a fixed observation window. Do not claim causality from small samples; compare against a baseline or holdout when volume permits.

## Dashboard metrics

- Funnel: website sessions -> WhatsApp clicks -> recorded leads -> confirmed orders -> collected payments.
- Marketing: eligible recipients, queued, attempted, delivered, failed, replies, confirmed orders, collected revenue, spend, opt-outs and complaint rate.
- AI quality: evaluation accuracy, stable-case rate, flaky cases, missing outputs, tool-call failures, escalations and human handovers.
- AI operations: model/purpose, input/output tokens, estimated spend, latency, timeouts, rate limits and fallback counts.
- Unit economics: collected revenue is not profit. Display known costs and clearly mark missing rent, utilities, staff, supplies or other expenses; do not present incomplete margin estimates as actual profit.

Do not send customer identifiers or message bodies to GA4. Keep PII out of default logs and evaluation artifacts.

## Release gates

- Run focused tests and the full CI suite; record the actual result, not an old baseline.
- Run `python -m pytest tests/test_intent_eval.py` and score a reviewed prediction export with `python -m evals.score_intents`.
- Verify tenant isolation and the current opt-out state at the final send boundary.
- Verify GA4 events in a real tagged browser session and reconcile them to backend leads/orders.
- Verify Google Business Profile account/location selection, poster URL reachability, image attachment and publication status using a controlled owner-approved check.
- Confirm retries are idempotent and do not duplicate tasks, sends, or posts.
- Keep production deployment separate: backup, migration review, health checks, smoke tests and rollback plan.

## Rollout

1. Evaluation and read-only reporting.
2. Owner-visible recommendations and campaign drafts.
3. Small, owner-approved campaign cohort.
4. Compare conversions, collected revenue, opt-outs, complaints and cost against a baseline.
5. Expand only when results are positive and safety/error metrics remain within agreed limits.

No live sends or publication are part of CI or the evaluation runner.
