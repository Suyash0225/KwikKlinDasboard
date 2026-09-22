# Kwik Klin AI Servant — Deep Design Note

Date: 2026-09-23

## 1. Goal

The AI is not a chatbot attached to the laundry. It is a digital servant/operator for Kwik Klin: it understands customer, staff and owner messages; reads the business system; performs only permitted actions through deterministic backend services; follows up on unfinished work; and escalates exceptions to the authorized admin.

Core loop:
UNDERSTAND -> RESOLVE -> READ FACTS -> PLAN -> VALIDATE -> EXECUTE -> COMMUNICATE -> CONFIRM -> FOLLOW UP

The model must never be the source of truth for prices, balances, dates, order status, customer identity, staff identity, payment links or permissions.

## 2. Research findings applied

- Purpose-built laundry systems consistently center the order lifecycle: intake/tagging -> processing -> quality check -> packing -> pickup/delivery -> payment. Garment/item traceability is a core control, not an optional reporting feature.
- Laundry software commonly combines order tracking, billing, pickup/delivery, customer history, WhatsApp notifications, staff workload and reports in one operating view.
- Current AI-agent guidance favors bounded tools, explicit permissions, validation, audit trails and human approval for irreversible/high-impact actions.
- Customer complaints need evidence-first investigation: intake condition, order history, processing/quality records and communications should be checked before promising a resolution.
- WhatsApp is the natural front door for Indian SMB service workflows, so the servant should treat WhatsApp conversations, voice notes and mixed Hindi/Hinglish as first-class inputs.

Sources reviewed: Google Gemini API model/structured-output documentation; current laundry-management products and workflow documentation from Chimti, SMART Laundry, AccoTick and related laundry operations references; current AI-agent/customer-service workflow guidance.

## 3. Gemini strategy

Use the existing Gemini provider architecture. Do not add a second model vendor just to create more agents.

Recommended production split:
- Fast path / classification / extraction / tool routing / voice transcription: Gemini 3.5 Flash-Lite.
- Complex owner reasoning / multi-step tool orchestration / difficult customer cases: Gemini 3.5 Flash.
- Use stable model IDs rather than latest aliases for production reproducibility.
- Structured JSON should be used for decisions/tool selection. Free-form text should only be the final customer/owner message.

Google currently documents Gemini 3.5 Flash-Lite as a stable, low-latency, high-throughput multimodal model with function calling and structured-output support. Gemini 3.5 Flash is positioned for stronger sustained agentic and multi-step work. Structured output is intended for extraction, classification and agentic tool inputs.

## 4. Servant roles

One servant brain, different authority scopes:

### Customer mode
- Answer order status, delivery date, bill/payment link, prices and service FAQs from live facts.
- Collect pickup information across multiple messages.
- Understand Hindi, Hinglish, English, voice notes and customer photos.
- Create/continue safe pickup workflows.
- Detect complaints and escalate without inventing compensation.

### Staff mode
- Show only that staff member's work.
- Understand natural replies in the context of an open task.
- Convert updates into task/order state through deterministic services.
- Capture ETA/problems and notify admin when necessary.

### Owner/admin mode
- Answer business questions from live DB facts.
- Look up orders, customers, staff chats, money, expenses, tasks, leads and operational exceptions.
- Create expenses/customers/tasks where explicitly instructed.
- Prepare or execute approved marketing workflows according to existing campaign controls.
- Give a concise confirmation after a real database action.

## 5. Business command classes

Read:
- business snapshot
- today's/next-day delivery risk
- late orders
- unpaid/outstanding money
- revenue/collection/expense
- customer history
- lead pipeline and follow-ups
- staff workload and unanswered messages
- open tasks
- open escalations
- marketing performance

Write:
- create/update task
- assign work
- record expense
- save customer
- update shop facts
- request pickup
- send real bill/payment link
- update permitted order state through domain services
- record payment only through deterministic payment services
- create/draft campaign; sending must respect existing approval/compliance controls

High-risk / human approval:
- refunds or compensation
- changing sensitive pricing/rate cards
- deleting data
- irreversible order cancellation
- disputed payment decisions
- customer liability decisions for damaged/lost garments
- bulk marketing send without an approved campaign state

## 6. Tool architecture

Every tool must have:
1. Explicit name and description.
2. Narrow arguments.
3. Tenant scope.
4. Permission scope.
5. Deterministic validation.
6. Idempotency where possible.
7. Audit event.
8. A safe failure response.
9. No raw SQL access from the model.
10. No model-generated IDs, URLs, money or recipient IDs.

Tool result format should distinguish:
- FACT: verified DB value
- ACTION: action actually executed
- NEEDS_INPUT: exact missing field
- AMBIGUOUS: multiple possible targets
- BLOCKED: permission/policy prevented action
- ERROR: system failure

## 7. Laundry-specific operational intelligence

The servant should reason around the physical laundry pipeline:

INTAKE -> CONDITION CHECK -> TAG -> SORT -> WASH/DRY CLEAN -> DRY -> PRESS -> QC -> PACK -> READY -> PICKUP/DELIVERY -> PAYMENT -> FEEDBACK

At each stage it should know:
- what is currently waiting
- who owns the next step
- when it is due
- whether it is late
- whether customer communication has already happened
- whether an exception blocks the order
- whether a staff task exists already
- whether a duplicate task would be created

## 8. Exception-first design

The servant should proactively surface exceptions rather than merely answer questions.

Priority order:
1. Customer safety/trust issue or damaged/lost garment.
2. Order likely to miss promised delivery.
3. Pickup/delivery task without response.
4. Payment discrepancy or high outstanding amount.
5. Open customer escalation.
6. Hot/unanswered lead.
7. Staff workload imbalance.
8. Routine reporting.

Never silently mark an order ready/delivered because the model believes it should be ready. The state machine remains authoritative.

## 9. Customer experience

Customer replies should be short, warm and factual. Use the customer's language. Do not expose internal notes, staff names, escalation reasons or internal IDs.

For a customer asking 'mera order kaha hai?', retrieve the exact order first. For 'kal milega?', resolve the actual expected delivery date from DB. For 'bill bhejo', send the actual bill/payment link. For complaints, inspect evidence and escalate when a decision is needed.

## 10. Owner experience

The owner should be able to type natural commands such as:

- 'aaj business ka kya scene hai?'
- 'kaun kaun late hai?'
- 'kal ki deliveries dikhao'
- 'Ajit ke paas kitna kaam hai?'
- 'leads ka kya haal hai?'
- 'Ravi ko Sharma ji ka pickup yaad dila do'
- '100 petrol ka kharcha likh do'
- 'is number ko Rahul ke naam se save kar do'

The answer should contain the useful result first, not a generic dashboard link.

## 11. Proactive servant

Scheduled jobs remain deterministic. AI is used only where language/reasoning is valuable.

10:00: staff standup and open-task review.
09:00: hot-lead digest.
11:00: due-delivery/payment checks.
15:00 and 18:00: staff/task follow-up windows already defined by the existing system.
21:00: owner business summary.

Future servant intelligence can rank exceptions inside these windows, but the schedule, recipient and idempotency must remain code-controlled.

## 12. Marketing servant

Marketing should be lifecycle-driven rather than random blasting:
NEW -> FIRST ORDER -> ACTIVE -> AT-RISK -> LAPSED -> WIN-BACK

AI may identify a segment, explain why, draft copy and creative brief. Existing opt-out, frequency, quiet-hour, discount and approval controls remain authoritative. Campaign sending should never be triggered merely because the model suggested it.

## 13. Knowledge/RAG

Trusted knowledge layers:
1. Live transactional DB facts: highest priority for order/payment/status questions.
2. Shop settings/rate card: current public business facts.
3. Approved FAQ/corrections/documents: stable business knowledge.
4. Conversation history: context, not business truth.
5. Model general knowledge: only for generic language help; never for shop-specific facts.

If sources disagree, live deterministic business state wins for operational facts. The agent should say it needs confirmation when the source of truth is missing.

## 14. Speed architecture

Customer path should aim for one primary LLM call wherever possible.
Use deterministic routing before LLM when a clear command/button/order-number path exists.
Use fast Gemini for extraction/routing.
Use the stronger Gemini model only when multi-step reasoning or tool use is actually required.
Do not add a background QA LLM call to the customer request path.
Do not silently make a second provider/model call after a slow failure unless an explicit, bounded fallback is justified.

## 15. Tenant isolation

Every servant action must inherit the active tenant context. Background jobs must iterate tenants and enter tenant context before opening business DB sessions. No global owner phone, rate card, order list or lead list may be used as a cross-tenant fallback.

Secrets stay in the existing environment/secret mechanism. Never return API keys to browser code or logs.

## 16. Audit and observability

Every write should record:
- tenant
- actor role
- actor
- action
- target
- before/after where appropriate
- model decision if relevant
- deterministic validation result
- outbound message result

Metrics to expose:
- customer first-response latency
- AI response latency
- fallback count
- tool-call count
- tool failure count
- unresolved escalations
- late orders
- pickup no-response rate
- payment reminder recovery
- lead conversion
- repeat-customer rate
- campaign reply rate

## 17. Testing strategy

Test each workflow in four dimensions:
- happy path
- ambiguity
- safety/privacy
- infrastructure failure

Important laundry cases:
- same customer has multiple active orders
- two customers share a similar name
- customer sends voice + text fragments
- staff replies without task code
- staff reports a damaged garment after customer was already told READY
- pickup requested twice
- duplicate task creation
- WhatsApp send fails after DB action succeeds
- LLM times out
- Gemini returns malformed structured output
- rate card changes while a conversation is active
- tenant A cannot read tenant B

## 18. Definition of done

The servant is production-ready only when it can understand a request, resolve the exact target, retrieve authoritative facts, choose a bounded tool, validate it, execute through a domain service, communicate the committed result, record an audit trail, and recover/escalate when something fails.

## 19. Current implementation baseline

The existing Kwik Klin code already contains important foundations: customer-scoped agent tools, owner tool loop, deterministic action boundaries, task lifecycle, operational scheduler, knowledge base, lead pipeline, marketing controls, tenant context and Gemini LLM client.

The next development should extend these foundations rather than add another model-provider abstraction.