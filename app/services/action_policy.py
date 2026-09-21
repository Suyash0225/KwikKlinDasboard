"""Central business action policy for Kwik Klin AI.

AI can recommend or request an action, but execution stays in deterministic
backend handlers. Facts such as money, dates, URLs and customer eligibility
must always come from the database.
"""

BUSINESS_ACTION_POLICY = {
    "customer_service": {
        "ORDER_STATUS": "Read the live order state and explain it; never invent a status or date.",
        "PRICE_QUERY": "Use the active rate card; never invent a price or discount.",
        "NEW_ORDER": "Collect missing order details, then let the order service create/update the order.",
        "BILL_REQUEST": "Find the relevant order and send the real signed bill/payment link.",
        "COMPLAINT": "Acknowledge, escalate to the owner, and pause automated replies when required.",
    },
    "operations": {
        "ASSIGN_WASHING": "Create a wash task only when the delivery date is within the configured 3-day window.",
        "ASSIGN_DELIVERY": "Create delivery work only after the order is READY / eligible for delivery.",
        "PAYMENT_REMINDER": "Use the real outstanding balance and signed bill link; never let AI calculate or invent the amount.",
        "DAILY_STAFF_BRIEFING": "At the configured 10:00 IST standup, send each washer/delivery worker only their pending/today work and send managers a business-wide pending-task and due-delivery summary. Use deterministic DB data; no LLM is required.",
    },
    "marketing": {
        "IDENTIFY_OPPORTUNITY": "Use real customer segments, order value and campaign history before proposing outreach.",
        "CREATE_OFFER": "Choose an offer for the business goal, subject to the configured maximum discount and validity limits.",
        "DRAFT_CAMPAIGN": "Generate a WhatsApp-ready campaign with a clear seasonal headline, problem/hook, offer headline + eligibility, 2-3 service benefits, CTA, business contact only if configured, brand sign-off and validity only when supplied. Use readable line breaks and natural Hindi/Hinglish; do not compress the campaign into 3 lines or invent offer details.",
        "SEND_CAMPAIGN": "Never send merely because AI suggested it; respect owner approval/autonomy, opt-outs, frequency caps, budget and quiet hours.",
        "LEAD_FOLLOWUP": "Prioritize genuine enquiries and interested leads before cold/re-engagement outreach.",
        "REFERRAL": "Ask existing happy customers for referrals only when the business rules permit it.",
    },
}

ACTION_EXECUTION_RULES = (
    "AI may choose or recommend an action, but it cannot invent database facts, "
    "money, dates, URLs, staff assignments or eligibility. The backend validates "
    "every action before execution. Marketing sends must respect owner approval/"
    "autonomy, opt-outs, frequency caps, budget and quiet hours."
)

def business_policy_text() -> str:
    lines = []
    for group, actions in BUSINESS_ACTION_POLICY.items():
        lines.append(group.upper() + ":")
        lines.extend(f"- {name}: {rule}" for name, rule in actions.items())
    return "\n".join(lines)
