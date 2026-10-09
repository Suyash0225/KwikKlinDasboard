# AI Lead Generation Implementation Plan

Status: Phases 1 and the queued-campaign opt-out regression are merged. Remaining unchecked items are not complete.

## Guardrails
- Reuse existing Lead, Customer, Order, Campaign, CampaignRecipient, LlmUsage, WAHA, GA4, and Google Business Profile services.
- Never place customer name, phone, address, message body, or order identifiers in GA4 event parameters.
- Never send a WhatsApp campaign automatically from a code change or test.
- Marketing sends must pass the existing eligibility/opt-out gates and require explicit campaign approval.
- No production edits or deployment from this work. Merge only after required CI passes.
- Treat website WhatsApp clicks as intent, not confirmed leads or bookings. Orders and revenue in the backend remain the source of truth.

## Phase 1 — Source and event attribution
- [x] Add GA4 `whatsapp_click` and `generate_lead` events to the public website.
- [x] Include bounded UTM fields in website WhatsApp prefill text.
- [x] Parse website/Google source markers into the existing Lead.source field.
- [x] Add focused source parsing tests.
- [x] Verify required focused CI regression tests before merge.
- [ ] Verify browser/GA4 behavior against a real tagged website session.

## Phase 2 — Consent and safe follow-up
- [ ] Audit every proactive send path: lead follow-up, engagement, campaign, reminders, and agent suggestions.
- [x] Ensure the latest opt-out state is checked immediately before campaign send, including queued recipients.
- [ ] Audit all other proactive send paths for equivalent last-moment checks.
- [ ] Add consent provenance only if existing tables/settings do not already provide it; migration must be tenant-safe and reversible.
- [x] Add regression test for queued campaign opt-out.
- [ ] Add tests for STOP variants, broader race conditions, and human takeover.
- [ ] Keep utility/order updates separate from marketing consent and honor global opt-out rules already implemented.

## Phase 3 — Conversion and ROI attribution
- [ ] Define a stable attribution contract across Lead, Customer, Order, and CampaignRecipient before adding schema.
- [ ] Capture confirmed order and paid revenue from backend records; do not infer bookings from clicks.
- [ ] Preserve original source and campaign identifiers through conversion.
- [ ] Show unattributed outcomes as unattributed rather than fabricating campaign credit.
- [ ] Add tenant-isolation, duplicate-lead, conversion, and revenue calculation tests.

## Phase 4 — AI budget and resilient fallback
- [x] Add regression coverage for rate-card USD cost calculation.
- [ ] Verify per-day FX rate handling and free-model pricing labels.
- [ ] Add/enforce an atomic hard monthly spend cap before making paid provider requests.
- [x] Add a preflight budget estimate using the configured monthly USD budget and token rate card (not an atomic cap; concurrent requests may overlap).
- [ ] On quota/provider failure, use deterministic fallback or a human queue; never silently drop inbound messages.
- [ ] Add tests for 429/503, timeout, exhausted budget, free-tier unavailability, and usage logging failures.
- [ ] Keep paid providers disabled by default for the zero-additional-spend target.

## Phase 5 — Google Business Profile and campaign safety
- [ ] Verify token refresh, location selection, post publication result, retry behavior, and status history.
- [ ] Keep post publishing and bulk campaign delivery approval-controlled unless an explicitly approved automation policy exists.
- [ ] Use neutral, policy-compliant review requests; do not incentivize reviews or selectively solicit only positive reviews.
- [ ] Add failure/reconnect and attribution tests.

## Phase 6 — Release validation
- [ ] Run focused tests, full pytest suite, migrations on disposable CI PostgreSQL, and JavaScript syntax checks.
- [ ] Fix each actionable failure and rerun the failed test plus affected regression tests.
- [ ] Review CI output and changed files; no secrets or live customer data in fixtures/logs.
- [ ] Merge only when required checks are green.
- [ ] Production deployment remains a separate approved operation with backup, migration review, health checks, smoke tests, and rollback plan.

## Definition of done
A click is not a lead; a lead is not a booking; a booking is not revenue. Dashboard reporting must preserve these distinctions. No live WhatsApp send or production deployment is part of CI.
