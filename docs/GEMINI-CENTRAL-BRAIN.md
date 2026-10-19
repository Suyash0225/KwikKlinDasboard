# Gemini Central Business Brain

## Goal
Keep Gemini as the shared reasoning and natural-language layer while Kwik Klin's
Python services remain the authority for persisted facts, calculations, and actions.

## Runtime boundaries
- `app/services/llm_client.py` is the central Gemini provider boundary. Keep model
  selection, bounded retries, circuit breaker, usage attribution, and budget checks there.
- The owner-facing business assistant is the manager-query loop in `bill_agent.py`.
  It chooses tools from `agent_tools.TOOL_SPECS`; each tool runs deterministic
  application code and returns an observation for Gemini to explain.
- Customer and staff workflows keep their separate permissions and narrow schemas.
  They may use the same Gemini provider, but must not inherit owner permissions.
- Python/domain services validate and execute writes. Gemini must never invent live
  orders, balances, rates, staff replies, or cost figures.
- Tool failures must degrade safely; no unsupported “done” claims. Write actions,
  outbound messages, campaigns, task status changes, and financial changes retain
  their existing explicit confirmation, audit, tenant/RLS, and outbound-policy gates.

## Cost calculator limits
The owner-only `cost_analysis` tool uses tenant-scoped Expense, Order, and Rate rows.
It counts delivered-order volume only, excludes structured billing adjustments, uses
Decimal arithmetic, and reports missing/unclassified data. Expense records do not yet
carry an allocation basis, and historical line quantities may lack explicit units.
Therefore per-piece and per-kg ratios are labelled **shared-expense proxies**, not
audited unit costs or service margins. Do not recommend price changes from these
ratios until expense allocation and reliable production-volume capture are added.

## Verification and rollout
- Run focused tests plus the complete CI suite before merge.
- Do not send WhatsApp messages, mutate production data, or deploy as part of code review.
- After merge, deployment and a production health check remain a separate, explicit step.
