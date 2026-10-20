# AI Reliability Audit — Consolidated Fix Plan

**Base reviewed:** `main` at `094aafa14e768835897b2366ead53d7bd8c9db29`  
**Status:** Audit plan only. This document does not claim that the listed fixes have been implemented.

## Confirmed baseline

- GitHub Actions Python test workflow passed for the reviewed main commit.
- Customer-facing read tools are explicitly registered and fail closed for unknown tool names.
- Order creation is routed through deterministic backend services rather than raw SQL supplied by the model.
- Campaign wish-frequency fix is merged; it exempts only wish campaigns from the duplicate frequency check while retaining other outbound controls.

## Priority 0 — prevent unintended orders and pickups

- [ ] Trace every path from AI `intake.ready` to `_create_pickup_order`.
- [x] Require a standalone explicit affirmative before creating an order; a complete intake now returns a deterministic summary and asks the customer to confirm.
- [ ] Confirm confirmation state is bound to the current customer/tenant and the exact order summary, and expires or resets if the requested items/date change.
- [x] Add phrase-level regression tests for standalone confirmation versus a service request, mixed question, and cancellation. [ ] Still needed: full flow tests for changed details and duplicate webhook delivery.
- [ ] Ensure missing/ambiguous pickup date, address, garment list, or name results in a clarifying question, not an order.

## Priority 0 — price answers must come from the active rate card

- [ ] Trace rate-card retrieval, garment/service matching, and the final response composer.
- [x] Numeric price replies are now checked against active DB rates for a garment/service named in the customer's message; unmatched quotes are replaced with a manager-confirmation reply.
- [ ] If the item/service is missing or ambiguous, ask a short clarification or say the team will confirm; do not guess, extrapolate, or reuse a price for another garment.
- [x] Add unit tests for exact amount match, mismatched amount, wrong garment, and no-numeric-price response. [ ] Still needed: Hindi/Hinglish aliases, missing rates, conflicting services, and multi-item cases.
- [ ] Verify all amounts in customer replies are grounded in retrieved facts and formatted without changing the underlying amount.

## Priority 1 — Hinglish and follow-up intent regression

Add deterministic/mock-LLM tests for:
- [ ] `mujhe kapde dhulwane hain plz`: service enquiry, not an order unless required details and confirmation are collected.
- [ ] `aur chandani ka?`: resolve against recent conversation context; do not create an order or promise pickup from this fragment alone.
- [ ] `kal jo kapde diye the unka kya hua?`: retrieve the customer's own recent orders.
- [ ] `paisa kaise jama karu`: retrieve unpaid bill facts and send only the real signed payment link when appropriate.
- [ ] General conversation should not trigger order, bill, or campaign actions.
- [ ] Preserve saved customer name/address; do not repeatedly ask for fields already known.

## Priority 1 — action and outbound safety

- [ ] Test that customer-facing AI cannot bypass opt-out, tenant isolation, complaint escalation, quiet hours, or outbound policy.
- [ ] Verify staff messages cannot mutate an unrelated customer task; only explicit task-update intents through the task workflow should do so.
- [ ] Verify campaign sends still require owner approval/autonomy policy and that the wish-campaign frequency exception does not apply to ordinary promotions.
- [ ] Add idempotency tests for duplicate webhook events and retries around bill/order actions.

## Validation and release gates

- [ ] Run focused regression tests and the complete CI suite.
- [ ] Review the changed diff and all new tests; no secrets or production customer data in fixtures/logs.
- [ ] Merge only when required CI checks pass.
- [ ] Deploy separately, then verify service health and run a controlled test using a test customer/recipient.
- [ ] Do not replay the previous campaign's failed recipients automatically; inspect the failure reasons and use a controlled retry only after confirming safe eligibility.

## Known limits of this audit

A passing CI run proves only that the tests currently in the repository pass. It does not prove every real Gemini/WhatsApp conversation is correct. This plan requires implementation and regression coverage before it can be marked complete.
