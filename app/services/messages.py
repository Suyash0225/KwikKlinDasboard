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

# Do bhashayein, do sunne wale:
#   - staff / owner ka WhatsApp bot (bill draft, relay, task, standup) —
#     Hinglish. Washerman aur delivery wale isi mein kaam karte hain.
#   - grahak ko jaane wale saare messages — default English (owner ka
#     faisla, 16 Sep 2026). Ab ye Settings → Message formats mein dukaan ki
#     choice hai (setting "customer_language": "en" ya "hi"), startup par
#     load hoti hai aur save karte hi turant lagti hai — set_customer_lang().
DEFAULT_LANG = "hi"
CUSTOMER_LANG = "en"   # the default; read the live value with customer_lang()
_customer_lang = CUSTOMER_LANG


def customer_lang() -> str:
    """Language for customer-facing messages right now ("en" or "hi")."""
    return _customer_lang


def set_customer_lang(lang: str | None) -> None:
    global _customer_lang
    _customer_lang = lang if lang in ("en", "hi") else CUSTOMER_LANG

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
    "ai_failover_ack": {
        "hi": "Aapka message mil gaya hai, thodi der mein jawab dete hain 🙏",
        "en": "Aapka message mil gaya hai, thodi der mein jawab dete hain 🙏",
    },
    # --- order lifecycle notifications ---
    # No rate matched yet (customer said "chandni, kurta pajama" — we price
    # it when the clothes arrive). Showing "Total: ₹— | Baaki: ₹—" looked
    # broken, so the money line is simply left out until we know it.
    "order_confirmed_no_price": {
        "hi": "Namaste! Aapka order {order_number} mil gaya 🧺\nKapde: {items}\nKapde aate hi bill bana ke bhej denge.\nDelivery: {date}\n— {shop}",
        "en": "Hello! Your order {order_number} is received 🧺\nItems: {items}\nWe'll send the bill once the clothes reach us.\nDelivery: {date}\n— {shop}",
    },
    "order_confirmed_bill": {
        "hi": "Namaste! Aapka order {order_number} mil gaya 🧺\nKapde: {items}\nTotal: ₹{total} | Advance: ₹{advance} | Baaki: ₹{due}\nDelivery: {date}{bill_line}\n— {shop}",
        "en": "Hello! Your order {order_number} is received 🧺\nItems: {items}\nTotal: ₹{total} | Advance: ₹{advance} | Due: ₹{due}\nDelivery: {date}{bill_line}\n— {shop}",
    },
    # --- lead conversion follow-up ladder ---
    "lead_followup_2h": {
        "hi": "{name} ji, aapne {shop} ki laundry service ke baare mein poocha tha 🙏 Agar aap chahein to main pickup arrange karwa sakta hoon. Aaj pickup kis time convenient rahega? — {shop}",
        "en": "{name} ji, you recently asked about {shop}'s laundry service 🙏 If you'd like, we can arrange a pickup. What time would be convenient today? — {shop}",
    },
    "lead_day1": {
        "hi": "{name} ji, {shop} se yaad dila rahe hain 🙏 Aaj ya kal kabhi bhi free pickup ho sakta hai — bas time bata dijiye. — {shop}",
        "en": "{name} ji, a gentle reminder from {shop} 🙏 Free pickup today or tomorrow — just tell us a time. — {shop}",
    },
    "lead_day3": {
        "hi": "{name} ji, agar laundry ki requirement abhi bhi hai to main aapka pickup confirm karwa sakta hoon 🧺 Aap bas 'haan' ya preferred pickup time bhej dijiye. — {shop}",
        "en": "{name} ji, if you still need laundry service, I can help confirm your pickup 🧺 Just reply 'yes' or send your preferred pickup time. — {shop}",
    },
    "lead_day7": {
        "hi": "{name} ji, aapko baar-baar message nahi karenge 🙏 Jab bhi laundry/pickup ki zarurat ho, isi WhatsApp par message kar dijiye. — {shop}",
        "en": "{name} ji, we won't keep messaging you 🙏 Whenever you need laundry or pickup, just message us here. — {shop}",
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
        "hi": "Aapka order {order_number} deliver ho gaya ✅ Dhanyawad, {shop} ko mauka dene ke liye! 🙏{staff_line}\nHamari seva kaisi lagi?",
        "en": "Your order {order_number} has been delivered ✅ Thank you for choosing {shop}! 🙏{staff_line}\nHow was our service?",
    },
    # --- rating -> staff points (services/staff_scores.py) ---
    # Grahak ko: kisne kaam kiya. Inaam wali line sirf tab jab owner ne
    # Settings mein inaam chalu kiya ho — jhootha vaada nahi.
    "rating_staff_line": {
        "hi": "\nAapke kapde {washer} ne dhoye aur {delivery} ne pahunchaye. {points_note}",
        "en": "\nYour clothes were washed by {washer} and delivered by {delivery}. {points_note}",
    },
    "rating_staff_line_one": {
        "hi": "\nAapka order {name} ne sambhala. {points_note}",
        "en": "\nYour order was handled by {name}. {points_note}",
    },
    "rating_points_note": {
        "hi": "Aapki rating seedhe unke points mein judti hai.",
        "en": "Your rating goes straight into their points.",
    },
    "rating_reward_note": {
        "hi": "Aapki rating se unhe points milte hain, aur achhe points par {shop} unhe inaam deta hai 🎁",
        "en": "Your rating earns them points, and good points earn them a reward from {shop} 🎁",
    },
    "rating_points_thanks": {
        "hi": "Aapki rating se {names} ko {points} points mil gaye 🙌",
        "en": "{names} just got {points} points from your rating 🙌",
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
        "hi": "Khushkhabri! Aapka order {order_number} taiyar hai ✨ Jald hi delivery hogi.{bill_line}\n— {shop}",
        "en": "Good news! Your order {order_number} is ready ✨ Delivery soon.{bill_line}\n— {shop}",
    },
    "order_out_for_delivery": {
        "hi": "Aapka order {order_number} delivery ke liye nikal chuka hai 🛵 — {shop}",
        "en": "Your order {order_number} is out for delivery 🛵 — {shop}",
    },
    "order_delivered": {
        "hi": "Aapka order {order_number} deliver ho gaya ✅ Dhanyawad, {shop} ko mauka dene ke liye! 🙏{bill_line}",
        "en": "Your order {order_number} has been delivered ✅ Thank you for choosing {shop}! 🙏{bill_line}",
    },
    "bill_requested": {
        "hi": "Bilkul! Aapke order {order_number} ka bill yahan hai 🧾\nTotal: ₹{total} | Baaki: ₹{due}{bill_line}\n— {shop}",
        "en": "Sure! Here is your bill for order {order_number} 🧾\nTotal: ₹{total} | Due: ₹{due}{bill_line}\n— {shop}",
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
        "hi": "Sab theek? Upar menu se choice select karein: Haan = bill banega | Bill Badalna Hai = edit | Cancel = draft hatao.",
        "en": "Please choose from the menu above: Yes = create bill | Edit = change draft | Cancel = discard draft.",
    },
    "bill_draft_phone": {
        "hi": "Customer number: {phone} — sahi hai na?",
        "en": "Customer number: {phone} — is this right?",
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
        "hi": "⚠️ AI agent abhi temporarily unavailable hai. Thodi der baad dobara try karein.",
        "en": "⚠️ The AI agent is temporarily unavailable. Please try again shortly.",
    },
    "ai_down_staff_quota": {
        "hi": "⚠️ AI quota abhi complete ho gayi hai. Thodi der baad dobara try karein.",
        "en": "⚠️ The AI quota is currently exhausted. Please try again shortly.",
    },
    "ai_down_staff_rate_limit": {
        "hi": "⚠️ AI service par abhi traffic zyada hai. Thodi der baad dobara try karein.",
        "en": "⚠️ The AI service is rate-limited right now. Please try again shortly.",
    },
    "ai_down_staff_parse": {
        "hi": "⚠️ AI response samajhne mein problem hui. Dobara try karein.",
        "en": "⚠️ The AI response could not be parsed. Please try again.",
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
    # --- haath se bheje jaane wale (bill menu -> 💬 Message) ---
    # {balance_line} server banata hai: "Poora payment ho gaya ✅" ya
    # "Baaki: ₹200". {review_link} dukaan ki apni choti link (/r/<slug>).
    "payment_thanks": {
        "hi": "Namaste {name} 🙏\nAapka ₹{amount} ka payment mil gaya — order {order_number}. Bahut dhanyawad!\n{balance_line}{bill_line}\n— {shop}",
        "en": "Hello {name} 🙏\nWe have received your payment of ₹{amount} for order {order_number}. Thank you!\n{balance_line}{bill_line}\n— {shop}",
    },
    # Loyalty reward mila (services/rewards.py) — apne aap jaata hai
    "reward_earned": {
        "hi": "🎁 Namaste {name}! {condition} — aapko mila *{reward}* agle bill par.\nCode: *{code}* (valid till {valid_to}). Agle bill par bas ye code bata dijiye.\n— {shop}",
        "en": "🎁 Hello {name}! {condition} — you have earned *{reward}* on your next bill.\nCode: *{code}* (valid till {valid_to}). Just mention this code on your next bill.\n— {shop}",
    },
    # Haath se bheja "Delivery update": poora order de diya (partial wala
    # upar 'partial_delivery' hi jaata hai). Web bill ka link saath.
    "delivery_update_full": {
        "hi": "Namaste {name} 🙏\nAapke order {order_number} ke sabhi {count} kapde deliver ho gaye ✅{bill_line}\nDhanyawad, {shop} ko mauka dene ke liye!\n— {shop}",
        "en": "Hello {name} 🙏\nAll {count} clothes from your order {order_number} have been delivered ✅{bill_line}\nThank you for choosing {shop}!\n— {shop}",
    },
    # Thodi delivery — baaki kapde abhi dukaan par. Apne aap jaata hai.
    "partial_delivery": {
        "hi": "Namaste {name} 🙏\nOrder {order_number} ke {given} kapde aaj de diye gaye ✅\n{pending} kapde abhi hamare paas hain — jald hi pahuncha denge.{bill_line}\n— {shop}",
        "en": "Hello {name} 🙏\nWe delivered {given} clothes from order {order_number} today ✅\n{pending} clothes are still with us — we will bring them to you soon.{bill_line}\n— {shop}",
    },
    # --- staff ko: rating, shabashi, kaam ki yaad (services/staff_scores.py) ---
    "staff_rating_good": {
        "hi": "🌟 Shabash {name} ji! {customer} ne order {order_number} ko 'Bahut achha' kaha.\n+{points} points · Is mahine: {month_points} points{reward_line}\nAapki mehnat se {shop} ka naam banta hai — dhanyawad! 🙏",
        "en": "🌟 Well done {name}! {customer} rated order {order_number} 'Excellent'.\n+{points} points · This month: {month_points} points{reward_line}\nYour effort builds {shop}'s name — thank you! 🙏",
    },
    "staff_rating_mid": {
        "hi": "🙂 {name} ji, {customer} ne order {order_number} ko 'Theek tha' kaha. +{points} points.\nThoda aur dhyan — samay par kaam, saaf press aur achhi packing — aur agli baar 'Bahut achha' milega 💪",
        "en": "🙂 {name}, {customer} rated order {order_number} 'It was okay'. +{points} points.\nA little more care — on time, crisp pressing, neat packing — and next time it will be 'Excellent' 💪",
    },
    "staff_rating_bad": {
        "hi": "⚠️ {name} ji, {customer} ne order {order_number} par 'Sudhar chahiye' kaha.\nManager customer se baat kar rahe hain. Kripya dekhiye kahan kami rahi — deri, daag, packing ya baat-cheet — taaki agli baar aisa na ho. Kuch kehna ho to app mein manager ko batayein 🙏",
        "en": "⚠️ {name}, {customer} said order {order_number} 'Needs work'.\nThe manager is talking to the customer. Please look at what went wrong — delay, stains, packing or how we spoke — so it does not happen again. Tell your manager in the app if you want to explain 🙏",
    },
    "staff_points_milestone": {
        "hi": "🏆 Badhai ho {name} ji! Is mahine aapke {month_points} points ho gaye.{reward_line}\nAise hi customers ko khush rakhiye 🙌",
        "en": "🏆 Congratulations {name}! You have {month_points} points this month.{reward_line}\nKeep the customers this happy 🙌",
    },
    "staff_reward_line": {
        "hi": "\n🎁 {note}",
        "en": "\n🎁 {note}",
    },
    "staff_nudge_late": {
        "hi": "⏰ {name} ji, aapke {count} kaam deri mein hain:\n{lines}\n\nIn orders par customer ko delivery ka vaada kiya gaya tha. Deri se customer ka bharosa tootta hai aur wo doosri laundry par ja sakta hai — isse {shop} ki service par seedha asar padta hai.\nYe kaam aapke naam par hain, aur deri ka record owner dekhte hain. Kripya aaj hi poora karke app mein update kar dijiye 🙏\nKoi rukawat ho to app mein manager ko abhi batayein.",
        "en": "⏰ {name}, {count} of your jobs are late:\n{lines}\n\nThe customer was promised these by now. Delays break their trust and they may move to another laundry — that hits {shop}'s service directly.\nThese jobs are in your name and the owner sees the delay record. Please finish them today and update the app 🙏\nIf something is blocking you, tell your manager in the app now.",
    },
    "staff_nudge_due": {
        "hi": "📋 {name} ji, aaj ye {count} kaam poore hone hain:\n{lines}\n\nSamay par kaam se hi customer ka bharosa banta hai 💪 Ho jaaye to app mein update kar dijiye.",
        "en": "📋 {name}, these {count} jobs are due today:\n{lines}\n\nOn-time work is what earns the customer's trust 💪 Update the app when done.",
    },
    "staff_day_thanks": {
        "hi": "🌟 {name} ji, aaj aapne {count} kaam samay par poore kiye — koi bhi late nahi! Aapki wajah se customers khush hain. Dhanyawad 🙏",
        "en": "🌟 {name}, you finished {count} jobs on time today — nothing late! Customers are happy because of you. Thank you 🙏",
    },
    "manager_late_digest": {
        "hi": "🚨 {days}+ din late orders ({count}):\n{lines}\nStaff ko yaad dila diya gaya hai. Zaroorat ho to customer ko khud call karein.",
        "en": "🚨 Orders {days}+ days late ({count}):\n{lines}\nStaff have been reminded. Call the customer yourself if needed.",
    },
    # Delivery wala darwaze par gaya, koi nahi mila — staff app ka "Not home"
    "not_home": {
        "hi": "Namaste {name} 🙏\n{shop} se aaj aapke ghar aaye the (order {order_number}), par aap nahi mile.\nKripya bata dijiye kab aayein — hum usi samay pahunch jayenge.\n— {shop}",
        "en": "Hello {name} 🙏\nWe came to your home today for order {order_number} but could not find you.\nPlease tell us a good time and we will come back then.\n— {shop}",
    },
    "service_thanks": {
        "hi": "Namaste {name} 🙏\n{shop} ko apne kapde saunpne ke liye dil se dhanyawad! Ummeed hai order {order_number} aapko pasand aaya ✨\nAgli baar bas WhatsApp kar dijiye — pickup ghar se ho jayega 🧺\n— {shop}",
        "en": "Hello {name} 🙏\nThank you for trusting {shop} with your clothes! We hope you loved order {order_number} ✨\nNext time just WhatsApp us — we will pick up from your home 🧺\n— {shop}",
    },
    "review_request": {
        "hi": "Namaste {name} 🙏\nAgar aap chahein, apna imaandaar feedback Google par share kar sakte hain. Har tarah ka feedback hamare liye madadgar hai.\n👉 {review_link}\nDhanyawad!\n— {shop}",
        "en": "Hello {name} 🙏\nIf you wish, you can share your honest feedback on Google. All feedback helps us improve.\n👉 {review_link}\nThank you!\n— {shop}",
    },
    "review_invitation_line": {
        "hi": "Agar aap chahein, apna imaandaar feedback Google par share kar sakte hain. Har tarah ka feedback hamare liye madadgar hai.\n{review_link}",
        "en": "If you wish, you can share your honest feedback on Google. All feedback helps us improve.\n{review_link}",
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
        "hi": (
            "🧺 *{headline}*\n"
            "━━━━━━━━━━━━━━\n"
            "📦 *Order:* {order_number}\n"
            "👤 *Customer:* {customer_name}\n"
            "👕 *Kapde:* {items}\n"
            "📅 *Delivery:* {delivery}\n"
            "⚡ *Priority:* {priority}\n\n"
            "📌 *Instruction:* {extra}\n"
            "━━━━━━━━━━━━━━\n"
            "✅ Kaam ho jaye to *Done* button dabayein."
        ),
        "en": (
            "🧺 *{headline}*\n"
            "━━━━━━━━━━━━━━\n"
            "📦 *Order:* {order_number}\n"
            "👤 *Customer:* {customer_name}\n"
            "👕 *Items:* {items}\n"
            "📅 *Delivery:* {delivery}\n"
            "⚡ *Priority:* {priority}\n\n"
            "📌 *Instruction:* {extra}\n"
            "━━━━━━━━━━━━━━\n"
            "✅ Tap *Done* when completed."
        ),
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
        "hi": "💰 {order_number} par ₹{amount} ({method}) record karun? Abhi baaki: ₹{due}\nUpar menu se: Payment Record Karo ya Cancel.",
        "en": "💰 Record ₹{amount} ({method}) on {order_number}? Currently due: ₹{due}\nUse the menu above: Record Payment or Cancel.",
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
    "order_confirmed_bill", "order_ready",
    "order_out_for_delivery", "delay_notice", "payment_reminder",
    "payment_reminder_firm", "ack_received", "complaint_ack",
    "escalated_ack",
    "stop_confirmed", "start_confirmed",
    "payment_thanks", "service_thanks", "partial_delivery",
    "delivery_update_full", "reward_earned",
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
    """Install/remove an owner override in the live cache (hot reload).

    Review solicitation copy is deliberately locked: owner overrides must not
    selectively solicit positive reviews or introduce incentives.
    """
    if key not in EDITABLE_KEYS:
        _OVERRIDES.pop(key, None)
        return
    if text:
        _OVERRIDES[key] = text
    else:
        _OVERRIDES.pop(key, None)


def load_overrides(data: dict[str, str]) -> None:
    _OVERRIDES.clear()
    _OVERRIDES.update({k: v for k, v in (data or {}).items() if k in EDITABLE_KEYS and v})
    if _OVERRIDES:
        log.info("message_overrides_loaded", count=len(_OVERRIDES))


def get_override(key: str) -> str | None:
    return _OVERRIDES.get(key)


# Jo keys GRAHAK ko jaati hain. Nayi customer-facing key banao to yahan jodo,
# warna wo staff ki bhasha (Hinglish) mein chali jayegi.
CUSTOMER_KEYS = frozenset({
    "ack_received", "error_fallback", "ai_failover_ack",
    "order_confirmed_no_price",
    "order_confirmed_bill", "bill_requested", "lead_followup_2h", "lead_day1", "lead_day3", "lead_day7",
    "pickup_done", "pickup_confirmed_customer", "thankyou_rating",
    "rate_good_reply", "rate_mid_reply", "rate_bad_reply",
    "order_ready", "order_out_for_delivery", "order_delivered", "delay_notice",
    "status_reply", "status_reply_with_date", "orders_list_header",
    "order_not_found", "complaint_ack", "escalated_ack",
    "payment_reminder", "payment_reminder_firm",
    "payment_thanks", "service_thanks", "review_request", "review_invitation_line", "partial_delivery",
    "delivery_update_full", "reward_earned", "not_home",
    "rating_staff_line", "rating_staff_line_one", "rating_points_note", "rating_reward_note", "rating_points_thanks",
    "start_confirmed", "stop_confirmed", "relay_message_customer",
})


def lang_for(key: str) -> str:
    return _customer_lang if key in CUSTOMER_KEYS else DEFAULT_LANG


def get_message(key: str, lang: str | None = None, **fmt: str) -> str:
    """Return the string for `key`, formatted.

    lang na diya ho to key se tay hota hai: grahak wali keys CUSTOMER_LANG,
    baaki (staff/owner) DEFAULT_LANG. Owner overrides (any language) win
    over the built-in copy. {shop} is always available. Raises KeyError for
    an unknown key — that is a programming error we want to hear about loudly.
    """
    try:
        by_lang = MESSAGES[key]
    except KeyError:
        log.error("unknown_message_key", key=key)
        raise
    lang = lang or lang_for(key)
    text = _OVERRIDES.get(key) or by_lang.get(lang) or by_lang.get(DEFAULT_LANG) or by_lang["en"]
    fmt.setdefault("shop", settings.SHOP_NAME)
    # Bill links are optional; older callers may not provide one.
    fmt.setdefault("bill_line", "")
    fmt.setdefault("staff_line", "")
    return text.format(**fmt)


def status_label(status: OrderStatus, lang: str = DEFAULT_LANG) -> str:
    """Human wording for a status, for use after 'Aapka order X ...'."""
    by_lang = STATUS_LABELS.get(lang) or STATUS_LABELS[DEFAULT_LANG]
    return by_lang[status]
