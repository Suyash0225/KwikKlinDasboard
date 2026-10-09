# AI Lead Generation Implementation Plan

Status: Phases 1–5 have implementation PRs merged, including lead/revenue attribution, proactive-send consent checks, GBP publish retry coverage, and serialized AI budget reservations. Live browser/GA4 and real GBP credential/location verification remain external release checks; full-suite baseline is non-blocking and must not be described as fully green.

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
- [ ] Verify browser/GA4 behavior against a real tagged website session (requires live site + GA4 access).

## Phase 2 — Consent and safe follow-up
- [x] Audit proactive lead follow-up and campaign send paths; re-check current consent before proactive delivery.\n- [ ] Complete a final code audit of all reminder/engagement/agent-suggestion paths.
- [x] Ensure the latest opt-out state is checked immediately before campaign send, including queued recipients.
- [x] Add last-moment eligibility checks to the reviewed proactive follow-up paths.\n- [ ] Complete final cross-path audit.
- [ ] Add consent provenance only if existing tables/settings do not already provide it; migration must be tenant-safe and reversible.
- [x] Add regression test for queued campaign opt-out.
- [x] Add queued campaign opt-out regression and proactive lead-follow-up consent checks.\n- [ ] Add dedicated STOP-variant and human-takeover race tests.
- [ ] Keep utility/order updates separate from marketing consent and honor global opt-out rules already implemented.

## Phase 3 — Conversion and ROI attribution
- [x] Define and implement lead-source/campaign attribution copied to first confirmed order.
- [x] Report confirmed orders and collected/billed revenue from backend Order records.
- [x] Preserve source/campaign metadata through first conversion.
- [x] Report missing source as `unattributed`.
- [x] Add conversion and source/revenue attribution tests.\n- [ ] Extend explicit tenant-isolation and duplicate-lead attribution coverage.

## Phase 4 — AI budget and resilient fallback
- [x] Add regression coverage for rate-card USD cost calculation.
- [ ] Verify per-day FX rate handling and free-model pricing labels in a real dashboard session.
- [x] Add per-tenant serialized spend reservations before paid provider calls (estimated cost; not an exact invoice cap).
- [x] Estimate monthly spend from recorded token usage and the configured token rate card; reserve estimated cost atomically per tenant for in-flight calls.
- [ ] Complete failure-mode review for 429/503, exhausted budget, and usage logging; preserve deterministic fallback/human escalation.
- [ ] Add tests for 429/503, timeout, exhausted budget, free-tier unavailability, and usage logging failures.
- [ ] Keep paid providers disabled by default for the zero-additional-spend target.

## Phase 5 — Google Business Profile and campaign safety
- [x] Add mocked tests for Google Business post retry and duplicate prevention.\n- [ ] Verify token refresh, location selection, and publication status with the actual GBP account/credentials.
- [x] Keep post publishing and campaign delivery behind existing approval controls.
- [x] Use neutral, non-incentivized review requests for good, mid, and bad ratings; regression tests ensure review links are not gated on positive feedback.
- [x] Add mocked publish retry/duplicate-prevention tests.\n- [ ] Add live credential reconnect and attribution verification.

## Phase 6 — Release validation
- [x] Run required focused regression tests, disposable PostgreSQL migrations, and website/admin JavaScript syntax checks in CI.\n- [ ] Finish/review the non-blocking full-suite baseline audit; known baseline failures may remain.
- [x] Fix required focused-test failures and rerun; full-suite audit remains separately tracked.
- [ ] Review CI output and changed files; no secrets or live customer data in fixtures/logs.
- [x] Merge implementation PRs only after required focused checks passed.
- [ ] Production deployment remains a separate approved operation with backup, migration review, health checks, smoke tests, and rollback plan.

## Definition of done
A click is not a lead; a lead is not a booking; a booking is not revenue. Dashboard reporting must preserve these distinctions. No live WhatsApp send or production deployment is part of CI.
