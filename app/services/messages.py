"""ALL human-facing strings live here, keyed by message key + language.

Rules:
- No other module may contain customer/staff-facing text. Ever.
- "hi" = Hinglish (Hindi in Latin script) — what our customers actually type.
- Unknown language falls back to "hi", then "en".
- Editing copy here must never require touching logic elsewhere.
- INTERNAL info (orders.notes, delay reasons) must never appear in any
  string here — customers get polite notices only.
"""

import structlog

from app.config import settings
from app.models import OrderStatus

log = structlog.get_logger()

DEFAULT_LANG = "hi"

MESSAGES: dict[str, dict[str, str]] = {
    # --- generic ---
    "ack_received": {
        "hi": "Namaste! Aapka message mil gaya 🙏 Hum jald hi jawaab denge. — {shop}",
        "en": "Hello! We received your message and will reply shortly. — {shop}",
    },
    "error_fallback": {
        "hi": "Maaf kijiye, abhi thodi dikkat aa gayi hai. Hum jald hi aapse sampark karenge. — {shop}",
        "en": "Sorry, something went wrong on our side. We will get back to you shortly. — {shop}",
    },
    # --- order lifecycle notifications ---
    "order_confirmed": {
        "hi": "Namaste! Aapka order {order_number} humein mil gaya 🧺 ({items_count} items). Ready hote hi khabar karenge. — {shop}",
        "en": "Hello! Your order {order_number} is received ({items_count} items). We'll update you when it's ready. — {shop}",
    },
    "order_confirmed_with_date": {
        "hi": "Namaste! Aapka order {order_number} humein mil gaya 🧺 ({items_count} items). Expected delivery: {date}. — {shop}",
        "en": "Hello! Your order {order_number} is received ({items_count} items). Expected delivery: {date}. — {shop}",
    },
    # No rate matched yet (customer said "chandni, kurta pajama" — we price
    # it when the clothes arrive). Showing "Total: ₹— | Baaki: ₹—" looked
    # broken, so the money line is simply left out until we know it.
    "order_confirmed_no_price": {
        "hi": "Namaste! Aapka order {order_number} mil gaya 🧺\nKapde: {items}\nKapde aate hi bill bana ke bhej denge.\nDelivery: {date}\n— {shop}",
        "en": "Hello! Your order {order_number} is received 🧺\nItems: {items}\nWe'll send the bill once the clothes reach us.\nDelivery: {date}\n— {shop}",
    },
    "order_confirmed_bill": {
        "hi": "Namaste! Aapka order {order_number} mil gaya 🧺\nKapde: {items}\nTotal: ₹{total} | Advance: ₹{advance} | Baaki: ₹{due}\nDelivery: {date}\n— {shop}",
        "en": "Hello! Your order {order_number} is received 🧺\nItems: {items}\nTotal: ₹{total} | Advance: ₹{advance} | Due: ₹{due}\nDelivery: {date}\n— {shop}",
    },
    # --- lead follow-up ladder (Marketing Agent spec) ---
    "lead_day1": {
        "hi": "{name} ji, Kwik Klin se yaad dila rahe hain 🙏 Aaj ya kal kabhi bhi free pickup ho sakta hai — bas time bata dijiye. — {shop}",
        "en": "{name} ji, a gentle reminder from Kwik Klin 🙏 Free pickup today or tomorrow — just tell us a time. — {shop}",
    },
    "lead_day3": {
        "hi": "{name} ji, ek baar try karke dekhiye — free pickup + delivery, kapde naye jaise. Bata dijiye kab aayein? — {shop}",
        "en": "{name} ji, give us one try — free pickup + delivery. When shall we come? — {shop}",
    },
    "lead_day7": {
        "hi": "{name} ji, aapko pareshan nahi karenge 🙏 Kabhi bhi zarurat ho to yahi message kar dijiye, hum pahunch jayenge. Dhanyavaad! — {shop}",
        "en": "{name} ji, we won't disturb you again 🙏 Whenever you need us, just message here. Thank you! — {shop}",
    },
    "pickup_done": {
        "hi": "Namaste! Aapke {count} kapde pickup ho gaye hain ✅ Delivery {date} tak ho jayegi. Order: {order_number} — {shop}",
        "en": "Hello! Your {count} clothes have been picked up ✅ Delivery by {date}. Order: {order_number} — {shop}",
    },
    "pickup_confirmed_customer": {
        "hi": "Namaste {name} ji! Aapka order {order_number} confirm ho gaya 🧺 Pickup: {pickup}. Delivery pickup ke baad {sla} din mein. — {shop}",
        "en": "Hello {name}! Your order {order_number} is confirmed 🧺 Pickup: {pickup}. Delivery within {sla} days of pickup. — {shop}",
    },
    "thankyou_rating": {
        "hi": "Aapka order {order_number} deliver ho gaya ✅ Dhanyawad, {shop} ko mauka dene ke liye! 🙏\nHamari seva kaisi lagi?",
        "en": "Your order {order_number} has been delivered ✅ Thank you for choosing {shop}! 🙏\nHow was our service?",
    },
    "rate_good_reply": {
        "hi": "Bahut-bahut dhanyawad! ⭐ Aap jaise customers se hi {shop} chalta hai. Agli baar bhi yaad kijiyega 🙏",
        "en": "Thank you so much! ⭐ Customers like you keep {shop} going. See you next time 🙏",
    },
    "rate_mid_reply": {
        "hi": "Dhanyawad! Hum aur behtar karne ki koshish karenge 🙏 — {shop}",
        "en": "Thank you! We'll keep working to do better 🙏 — {shop}",
    },
    "rate_bad_reply": {
        "hi": "Maaf kijiye aapko achha anubhav nahi mila 🙏 Humne manager ko turant bataya hai — wo aapse baat karenge. — {shop}",
        "en": "We're sorry you had a poor experience 🙏 Our manager has been informed and will contact you. — {shop}",
    },
    "rate_bad_admin_alert": {
        "hi": "😞 KHARAB RATING: {customer_name} ({phone}) ne '{rating}' diya. Turant baat kar lein — Inbox se.",
        "en": "😞 BAD RATING: {customer_name} ({phone}) rated '{rating}'. Please reach out — via Inbox.",
    },
    "order_ready": {
        "hi": "Khushkhabri! Aapka order {order_number} taiyar hai ✨ Jald hi delivery hogi. — {shop}",
        "en": "Good news! Your order {order_number} is ready ✨ Delivery soon. — {shop}",
    },
    "order_out_for_delivery": {
        "hi": "Aapka order {order_number} delivery ke liye nikal chuka hai 🛵 — {shop}",
        "en": "Your order {order_number} is out for delivery 🛵 — {shop}",
    },
    "order_delivered": {
        "hi": "Aapka order {order_number} deliver ho gaya ✅ Dhanyawad, {shop} ko mauka dene ke liye! 🙏",
        "en": "Your order {order_number} has been delivered ✅ Thank you for choosing {shop}! 🙏",
    },
    "delay_notice": {
        "hi": "Namaste, aapke order {order_number} ki expected delivery ab {date} hai. Asuvidha ke liye maafi 🙏 — {shop}",
        "en": "Hello, the expected delivery for order {order_number} is now {date}. Sorry for the inconvenience 🙏 — {shop}",
    },
    # --- rule-based status replies (webhook) ---
    "status_reply": {
        "hi": "Aapka order {order_number} {status_label}",
        "en": "Your order {order_number} {status_label}",
    },
    "status_reply_with_date": {
        "hi": "Aapka order {order_number} {status_label} Expected delivery: {date}. — {shop}",
        "en": "Your order {order_number} {status_label} Expected delivery: {date}. — {shop}",
    },
    "orders_list_header": {
        "hi": "Aapke {count} orders chal rahe hain:",
        "en": "You have {count} orders in progress:",
    },
    "order_not_found": {
        "hi": "Maaf kijiye, ye order number humein nahi mila. Number check karke dobara bhejein 🙏 — {shop}",
        "en": "Sorry, we couldn't find that order number. Please check and resend 🙏 — {shop}",
    },
    # --- AI agent / escalations (Phase 4) ---
    "complaint_ack": {
        "hi": "Maaf kijiye aapko pareshani hui 🙏 Humne aapki baat turant apne manager tak pahuncha di hai — wo jald hi aapse sampark karenge. — {shop}",
        "en": "We're sorry for the trouble 🙏 Your message has been passed to our manager — they will contact you shortly. — {shop}",
    },
    "escalated_ack": {
        "hi": "Humne aapki baat manager tak pahuncha di hai, wo jald hi aapse sampark karenge 🙏 — {shop}",
        "en": "We've passed this to our manager — they will contact you shortly 🙏 — {shop}",
    },
    # Staff/manager-facing alert — internal, so quoting the message is fine.
    "escalation_alert": {
        "hi": "🔔 Dhyan dein: {customer_name} ({phone}) ka message bot handle nahi kar paya:\n\n\"{question}\"\n\nDashboard Inbox se jawaab dein.",
        "en": "🔔 Attention: the bot could not handle a message from {customer_name} ({phone}):\n\n\"{question}\"\n\nReply from the dashboard Inbox.",
    },
    # --- staff/manager bill-by-text (Phase 4c) — all internal-facing ---
    "bill_draft_header": {
        "hi": "📝 Bill draft — {customer_name}:",
        "en": "📝 Bill draft — {customer_name}:",
    },
    "bill_draft_from_photo": {
        "hi": "(parche se padha — neeche check kar lein)",
        "en": "(read from the slip — please check below)",
    },
    "bill_draft_needs_answer": {
        "hi": "❓/⚠️ wale item main sahi nahi padh paya — sahi likh dein (jaise '2 kurta dry clean'), phir 'haan'.",
        "en": "I couldn't read the ❓/⚠️ items — send the correct line (e.g. '2 kurta dry clean'), then 'haan'.",
    },
    "bill_photo_unreadable": {
        "hi": "📷 Parcha saaf nahi padh paya — isliye maine apne se kuch nahi likha. Ek saaf photo bhej dein, ya kapde type kar dein (jaise 'Sharma ji 3 shirt wash 2 saree dry clean'), main bill bana dunga.",
        "en": "📷 I couldn't read the slip, so I've added nothing on my own. Send a clearer photo, or type the items (e.g. 'Sharma ji 3 shirt wash 2 saree dry clean') and I'll make the bill.",
    },
    "bill_draft_total": {"hi": "Total: ₹{total}", "en": "Total: ₹{total}"},
    "bill_draft_advance": {"hi": "Advance: ₹{advance}", "en": "Advance: ₹{advance}"},
    "bill_draft_delivery": {
        "hi": "Delivery: {date}",
        "en": "Delivery: {date}",
    },
    "bill_draft_confirm": {
        "hi": "Sab theek? 'haan' → bill ban jaega | badalna ho to likh dein | 'nahi' → cancel",
        "en": "All good? 'haan' → creates the bill | describe any change | 'nahi' → cancel",
    },
    "bill_need_phone": {
        "hi": "⚠️ Customer ka number nahi mila — number bhej dein, draft saved hai.",
        "en": "⚠️ No customer number — send it, the draft is saved.",
    },
    "bill_created": {
        "hi": "✅ Order {order_number} ban gaya (₹{total}). Customer ko confirmation bhej di gayi hai.",
        "en": "✅ Order {order_number} created (₹{total}). Customer has been notified.",
    },
    "bill_cancelled": {"hi": "❌ Draft cancel kar diya.", "en": "❌ Draft cancelled."},
    # Voice note aayi par shabd nahi nikle. Chup rehna sabse bura tha —
    # owner ne voice se bill bolna chaha aur use kuch bhi wapas nahi mila,
    # na jawab na wajah. Ye owner Settings se badal sakta hai.
    "voice_unclear_staff": {
        "hi": (
            "🎧 Voice note mil gaya, par abhi awaaz samajh nahi paya. "
            "Ek line likh dijiye — bill, status ya kaam — main turant kar dunga."
        ),
        "en": (
            "🎧 Got your voice note, but I couldn't make out the words. "
            "Please type one line — bill, status or task — and I'll do it right away."
        ),
    },
    "staff_cmd_unknown": {
        "hi": "🤔 Samajh nahi aaya. Bill: customer + kapde + service. Delay: order number + nayi date. Status: order number + stage. Message bhejna: 'Ravi ko bolo ...'",
        "en": "🤔 Didn't understand. Bill: customer + items + service. Delay: order number + new date. Status: order number + stage. Relay: 'Ravi ko bolo ...'",
    },
    "relay_message": {
        "hi": "📨 {sender} ki taraf se: {message}",
        "en": "📨 From {sender}: {message}",
    },
    "relay_done": {
        "hi": "✅ {name} ko bhej diya: \"{message}\"",
        "en": "✅ Sent to {name}: \"{message}\"",
    },
    "task_assigned": {
        "hi": "✅ {name} ko de diya [{code}]: \"{message}\"\nMain khud follow-up karta rahunga jab tak jawab na de.",
        "en": "✅ Assigned to {name} [{code}]: \"{message}\"\nI'll keep following up until they reply.",
    },
    "task_assigned_undelivered": {
        "hi": "⚠️ {code} save kar liya ({name}) par abhi unke paas message nahi ja paya — unka WhatsApp window band hai aur template bhi fail hua. Dashboard ke Tasks mein dikh raha hai; window khulte hi main bhej dunga.",
        "en": "⚠️ Saved {code} for {name} but could not deliver it yet — their window is closed and the template failed. It's in Tasks; I'll send it once the window opens.",
    },
    "task_done_ack": {
        "hi": "👍 {code} band kar diya. Shukriya!",
        "en": "👍 {code} closed. Thanks!",
    },
    "task_unknown_code": {
        "hi": "⚠️ {code} naam ka koi kaam nahi mila.",
        "en": "⚠️ No task called {code}.",
    },
    "relay_done_template": {
        "hi": "✅ {name} ka window band tha — template se bhej diya: \"{message}\"",
        "en": "✅ {name}'s window was closed — sent via template: \"{message}\"",
    },
    "relay_target_unknown": {
        "hi": "⚠️ '{target}' staff list mein nahi mila. Staff: {names}",
        "en": "⚠️ '{target}' is not in the staff list. Staff: {names}",
    },
    "relay_window_closed": {
        "hi": "⚠️ {name} ka 24h WhatsApp window band hai — pehle wo bot ko koi bhi message bhejein, phir bhej paunga.",
        "en": "⚠️ {name}'s 24h WhatsApp window is closed — they must message the bot first.",
    },
    "relay_failed": {
        "hi": "⚠️ {name} ko bhejna fail ho gaya — thodi der baad try karein.",
        "en": "⚠️ Sending to {name} failed — try again shortly.",
    },
    "order_not_found_staff": {
        "hi": "⚠️ Order {order_number} nahi mila.",
        "en": "⚠️ Order {order_number} not found.",
    },
    "delay_needs_date": {
        "hi": "⚠️ {order_number} ke liye nayi date samajh nahi aayi — date ke saath dobara bhejein (jaise 'kal' ya '5 Aug').",
        "en": "⚠️ Couldn't read the new date for {order_number} — resend with a date.",
    },
    "delay_done": {
        "hi": "✅ {order_number} ki delivery ab {date}. Customer ko polite notice chala gaya (wajah sirf notes mein hai).",
        "en": "✅ {order_number} delivery is now {date}. Customer got a polite notice (reason stays internal).",
    },
    "status_done": {
        "hi": "✅ {order_number} → {status_name}",
        "en": "✅ {order_number} → {status_name}",
    },
    "status_invalid": {
        "hi": "⚠️ {order_number} abhi {old} mein hai — wahan se {new} allowed nahi.",
        "en": "⚠️ {order_number} is in {old} — moving to {new} is not allowed.",
    },
    "ai_down_staff": {
        "hi": "⚠️ AI agent abhi uplabdh nahi hai (API key ya network). Bill dashboard se bana lein; thodi der baad dobara try karein.",
        "en": "⚠️ The AI agent is unavailable right now (API key or network). Use the dashboard for bills; try again shortly.",
    },
    # --- scheduler: standup + reminders ---
    # {greet} waqt se banta hai (scheduler._greeting) — sham 6 baje
    # "Good morning" bhejna bot ko bewakoof dikhata hai.
    "standup_header": {
        "hi": "{greet} {name}! Abhi {count} kaam pending hain:",
        "en": "{greet} {name}! {count} jobs pending right now:",
    },
    "standup_footer": {
        "hi": "Kaun se ho gaye? Kahin koi dikkat? Bas reply kar dein (jaise: '1 aur 2 ho gaya, 3 kal hoga').",
        "en": "Which are done, and is anything stuck? Just reply (e.g. '1 and 2 done, 3 tomorrow').",
    },
    "standup_recorded": {
        "hi": "Shukriya {name}! Record kar liya:",
        "en": "Thanks {name}! Recorded:",
    },
    "payment_reminder": {
        "hi": "Namaste! Aapke order {order_number} ka ₹{amount} baaki hai. Jab suvidha ho, de dijiyega 🙏 — {shop}",
        "en": "Hello! ₹{amount} is pending for your order {order_number}. Please pay at your convenience 🙏 — {shop}",
    },
    "payment_reminder_firm": {
        "hi": "Namaste, aapke order {order_number} ka ₹{amount} kaafi dino se baaki hai. Kripya jald bhugtaan karein — cash/UPI dono chalega. Dhanyawad 🙏 — {shop}",
        "en": "Hello, ₹{amount} for order {order_number} has been pending for a while. Please clear it soon — cash or UPI. Thank you 🙏 — {shop}",
    },
    "overdue_admin_flag": {
        "hi": "📋 Purane udhaar (15+ din):\n{listing}",
        "en": "📋 Long-pending dues (15+ days):\n{listing}",
    },
    "start_confirmed": {
        "hi": "Wapas swagat hai! 🙏 Ab hum aapko zaroori updates bhejte rahenge. — {shop}",
        "en": "Welcome back! 🙏 We'll keep you posted with important updates. — {shop}",
    },
    "stop_confirmed": {
        "hi": "Theek hai, ab hum aapko koi message nahi bhejenge. Kabhi zaroorat ho to bas 'START' likh dijiyega 🙏 — {shop}",
        "en": "Understood — we will not message you anymore. If you ever need us again, just send 'START' 🙏 — {shop}",
    },
    # --- work orders & admin power commands (Phase overnight) ---
    "work_order": {
        "hi": "🧺 {headline}!\nOrder: {order_number}\nCustomer: {customer_name}\nKapde: {items}\nDelivery: {delivery}\nPriority: {priority}\nInstruction: {extra}",
        "en": "🧺 {headline}!\nOrder: {order_number}\nCustomer: {customer_name}\nItems: {items}\nDelivery: {delivery}\nPriority: {priority}\nInstruction: {extra}",
    },
    "relay_message_customer": {
        "hi": "{message}\n— {shop}",
        "en": "{message}\n— {shop}",
    },
    "priority_set_notified": {
        "hi": "✅ {order_number} ab {priority} hai — staff ko saaf instruction bhej di gayi.",
        "en": "✅ {order_number} is now {priority} — staff has been given a clear instruction.",
    },
    "priority_set_no_staff": {
        "hi": "✅ {order_number} ab {priority} hai. ⚠️ Par koi staff assigned nahi — Settings mein default washer set karein.",
        "en": "✅ {order_number} is now {priority}. ⚠️ But no staff is assigned — set a default washer in Settings.",
    },
    "priority_set_notify_failed": {
        "hi": "✅ {order_number} ab {priority} hai. ⚠️ Staff ko message nahi ja paya — khud bata dein.",
        "en": "✅ {order_number} is now {priority}. ⚠️ Could not message the staff — please tell them directly.",
    },
    "assign_done": {
        "hi": "✅ {order_number} ab {name} ke paas hai. Work order: {notified}",
        "en": "✅ {order_number} is now with {name}. Work order: {notified}",
    },
    "note_done": {
        "hi": "✅ Note save ho gaya ({order_number}) aur {notified}.",
        "en": "✅ Note saved on {order_number}; {notified}.",
    },
    "payment_confirm_prompt": {
        "hi": "💰 {order_number} par ₹{amount} ({method}) record karun? Abhi baaki: ₹{due}\n'haan' → record | 'nahi' → cancel",
        "en": "💰 Record ₹{amount} ({method}) on {order_number}? Currently due: ₹{due}\n'haan' → record | 'nahi' → cancel",
    },
    "payment_done": {
        "hi": "✅ ₹{amount} record ho gaya ({order_number}). Ab baaki: ₹{due} [{status}]",
        "en": "✅ ₹{amount} recorded on {order_number}. Remaining due: ₹{due} [{status}]",
    },
    "order_ambiguous": {
        "hi": "🤔 '{name}' ke kai orders chal rahe hain — kaun sa?\n{listing}\nOrder number ke saath dobara bhejein.",
        "en": "🤔 '{name}' has multiple active orders — which one?\n{listing}\nResend with the order number.",
    },
    "order_for_customer_not_found": {
        "hi": "⚠️ '{name}' ka koi active order nahi mila.",
        "en": "⚠️ No active order found for '{name}'.",
    },
    "role_not_allowed": {
        "hi": "⚠️ {status_name} aapke role ka kaam nahi hai — manager ko batayein.",
        "en": "⚠️ {status_name} is not your role's job — please tell the manager.",
    },
    "cancel_needs_dashboard": {
        "hi": "⚠️ {order_number} cancel WhatsApp se nahi hota — galti se na ho isliye dashboard se karein.",
        "en": "⚠️ Cancelling {order_number} must be done from the dashboard (safety).",
    },
}

# How each status reads in a sentence: "Aapka order KK-... <label>"
STATUS_LABELS: dict[str, dict[OrderStatus, str]] = {
    "hi": {
        OrderStatus.RECEIVED: "mil gaya hai, jald kaam shuru hoga 🧺.",
        OrderStatus.PICKUP_ASSIGNED: "ke liye pickup lagaya gaya hai 🛵.",
        OrderStatus.PICKED_UP: "ke kapde utha liye gaye hain ✅.",
        OrderStatus.IN_WASH: "abhi dhulai mein hai 🧼.",
        OrderStatus.IN_DRY: "dhul chuka hai, sukh raha hai.",
        OrderStatus.IN_IRON: "istri ho rahi hai 👔.",
        OrderStatus.READY: "taiyar hai ✨ Jald delivery hogi.",
        OrderStatus.OUT_FOR_DELIVERY: "delivery ke liye nikal chuka hai 🛵.",
        OrderStatus.DELIVERED: "deliver ho chuka hai ✅.",
        OrderStatus.ON_HOLD: "thodi der ke liye ruka hua hai, jald shuru hoga.",
        OrderStatus.CANCELLED: "cancel ho chuka hai.",
    },
    "en": {
        OrderStatus.RECEIVED: "has been received 🧺.",
        OrderStatus.PICKUP_ASSIGNED: "has a pickup assigned 🛵.",
        OrderStatus.PICKED_UP: "has been picked up ✅.",
        OrderStatus.IN_WASH: "is being washed 🧼.",
        OrderStatus.IN_DRY: "is drying.",
        OrderStatus.IN_IRON: "is being ironed 👔.",
        OrderStatus.READY: "is ready ✨ Delivery soon.",
        OrderStatus.OUT_FOR_DELIVERY: "is out for delivery 🛵.",
        OrderStatus.DELIVERED: "has been delivered ✅.",
        OrderStatus.ON_HOLD: "is briefly on hold.",
        OrderStatus.CANCELLED: "has been cancelled.",
    },
}


# Owner-edited formats (Settings -> Message formats). Kept in module cache
# for sync access everywhere; persisted in settings_kv, loaded at startup.
_OVERRIDES: dict[str, str] = {}

# Keys the owner may safely rewrite from the UI (customer-facing copy).
EDITABLE_KEYS = [
    "order_confirmed_bill", "thankyou_rating", "order_ready",
    "order_out_for_delivery", "delay_notice", "payment_reminder",
    "payment_reminder_firm", "ack_received", "complaint_ack",
    "escalated_ack", "rate_good_reply", "rate_mid_reply", "rate_bad_reply",
    "stop_confirmed", "start_confirmed",
]


def allowed_placeholders(key: str) -> set[str]:
    """Placeholders the default text uses (+{shop}) — the editable set."""
    import string

    names: set[str] = {"shop"}
    for text in MESSAGES.get(key, {}).values():
        for _, field, _, _ in string.Formatter().parse(text):
            if field:
                names.add(field)
    return names


def set_override(key: str, text: str | None) -> None:
    """Install/remove an owner override in the live cache (hot reload)."""
    if text:
        _OVERRIDES[key] = text
    else:
        _OVERRIDES.pop(key, None)


def load_overrides(data: dict[str, str]) -> None:
    _OVERRIDES.clear()
    _OVERRIDES.update({k: v for k, v in (data or {}).items() if k in MESSAGES and v})
    if _OVERRIDES:
        log.info("message_overrides_loaded", count=len(_OVERRIDES))


def get_override(key: str) -> str | None:
    return _OVERRIDES.get(key)


def get_message(key: str, lang: str = DEFAULT_LANG, **fmt: str) -> str:
    """Return the string for `key` in `lang`, formatted.

    Owner overrides (any language) win over the built-in copy. {shop} is
    always available. Raises KeyError for an unknown key — that is a
    programming error we want to hear about loudly.
    """
    try:
        by_lang = MESSAGES[key]
    except KeyError:
        log.error("unknown_message_key", key=key)
        raise
    text = _OVERRIDES.get(key) or by_lang.get(lang) or by_lang.get(DEFAULT_LANG) or by_lang["en"]
    fmt.setdefault("shop", settings.SHOP_NAME)
    return text.format(**fmt)


def status_label(status: OrderStatus, lang: str = DEFAULT_LANG) -> str:
    """Human wording for a status, for use after 'Aapka order X ...'."""
    by_lang = STATUS_LABELS.get(lang) or STATUS_LABELS[DEFAULT_LANG]
    return by_lang[status]
