# PRD — Kwik Klin CRM: apni dukaan se SaaS tak

**Version:** 2.0 (final) · 2026-08-06
**Author:** Suyash ke liye, faisle lene ke liye — feature wish-list nahi
**Base:** `ARCHITECTURE.md`, `ROADMAP.md`, `PROJECT_SPEC.md`
**Tay ho chuka:** target price **₹999/mahina**, volume play, mohalla laundry ICP

> v1 ki 3 galtiyan is version mein theek ki gayi hain — multi-tenancy ka
> timing, pricing ka ganit, aur WABA ownership ka risk. Research ke saath.

---

## 0. Ek page mein poora faisla

**₹999 ka price sirf ek soorat mein chalta hai: jab WhatsApp ka kharcha aap
nahi uthate.**

Research se nikla sach: Indian BSPs (AiSensy, Wati, Interakt) **khud
₹999–₹2,199/mahina** lete hain — sirf WhatsApp layer ke liye. Agar har client
ka BSP subscription aapke sar par hua, to aapka poora ₹999 wahin khatam.
Business shuru hone se pehle mar jayega.

**Iska hal Meta ke Tech Provider program mein hai:** Tech Provider ban jaane
par client **Meta ko seedha** pay karta hai, published rate par, bina kisi
markup ke — aap sirf apne software ka paisa lete ho. Requirement: **10 client
+ 2,500 daily conversations**, approval 1–4 hafte.

Isse poora rasta ye ban jaata hai:

```
Client 1–10   → BSP ke ISV banke (co-branded onboarding)  → margin patla, chalega
Client 10+    → Tech Provider apply karo                   → client Meta ko seedha pay kare
Client 10–500 → ₹999 par 70–80% gross margin               → asli business
```

**Baaki 5 faisle:**

| # | Faisla | Final |
|---|---|---|
| 1 | Price | **₹999/mo** (+ ₹1,999 setup). Free tier nahi — competitor free deta hai, hum "chalu karke dete hain" bechte hain |
| 2 | Multi-tenancy | **`tenant_id` abhi daalo** (1 tenant par 2 din ka kaam). Deploy alag-alag rahe. 50 par migrate karna suicide hai |
| 3 | Onboarding | ₹999 par done-for-you nahi chal sakta. **Self-serve wizard P0 hai** |
| 4 | Hosting | Windows + tunnel khatam. Ek cloud VM + domain + TLS |
| 5 | Auth | `X-API-Key` khatam. User + password + roles, client #1 se pehle |

**Deewar kahan hai:** technology mein nahi — **aap akele 30–40 shop se aage
nahi ja sakte**. 30 se pehle onboarding self-serve karo ya banda rakho.

---

## 1. Aaj kahan hain (imaandar inventory)

Ek asli dukaan par, roz chal raha hai:

- **Orders** — state machine + history, auto number, SLA (pickup se 4/7 din), priority, staff assignment
- **Billing** — rate card (service × garment), discount presets, GST, coupons, payments ledger (append-only)
- **WhatsApp 2-tarfa** — har message type (text/photo/voice/doc/location/button/reaction), 24h window ka hisaab, template fallback, delivery ticks
- **AI agent** — customer ke sawaal ka jawab (FAQ + corrections + docs), escalation, bill-photo se order, voice note transcribe
- **Owner ka WhatsApp assistant** — business sawaal + tools (kharcha likhna, customer save, kaam dena)
- **Staff layer** — roles, standup, task follow-up ladder, pickup + delivery ka poora samvaad
- **Growth** — campaigns + segments + freq cap, leads ladder, daily social poster, review links
- **Dashboard** — 14 sections
- **Durability** — webhook journal + replay, outbound queue + dead-letter, idempotency, nightly `pg_dump`, keepalive

**Naapa hua:** ~14,000 lines Python + 3,700 frontend · 26 tables · **306 tests green** · 1 instance = **111 MB RAM** · ek shop ka DB = 13 MB

**Nahi hai:** login/multi-user, onboarding, billing, cloud hosting,
per-client provisioning, support tooling.

> Product technically **80% ban chuka hai**. Bacha hua 20% wahi hai jo "mera
> software" aur "bikne wala software" mein farak karta hai. **M1→M2→M3 se
> pehle koi naya laundry feature mat banao.**

---

## 2. Market aur ICP

**Market:** Indian laundry industry ₹2.5 lakh crore+, jiska **95–96%
unorganized** hai. Ye TAM problem nahi hai — ye distribution problem hai.

**ICP (tay):** mohalla-level laundry / dry cleaner
- 1 outlet, 2–5 log, mahine ke 150–800 order
- Sab register + WhatsApp par; software kabhi nahi kharida
- Malik counter par khada, mobile-first, English kam

**Unka dard, isi order mein:**
1. "Mera order kahan hai?" — din mein 20 phone
2. "Kaun sa kapda kiska" — parcha kho jaata hai
3. "Kisne paisa diya, kitna udhaar" — copy mein hai, milta nahi
4. "Staff ne kaam kiya ya nahi"
5. "Purane customer wapas nahi aate"

**Pitch (ek line):** *"Aapki dukaan ka WhatsApp khud jawab dega, bill
banayega, staff se kaam karvayega — aur har mahine purane customer wapas
layega."* Orders mein pitch karo, features mein nahi.

**Competitor benchmark:** Quick Dry Cleaning (segment ka leader) **Forever
Free** plan deta hai — 1,200 order/saal tak — par **₹5,000 mandatory
onboarding fee** leta hai. Iska matlab do baatein: (a) ye segment software ke
liye paisa dene mein bahut sensitive hai, (b) **paisa onboarding par milta
hai**, feature par nahi. Hamara ₹1,999 setup fee isi truth par baitha hai.

**Hamara farak:** WhatsApp-first + Hinglish AI + dukaandaar ke liye bana —
angrezi enterprise software nahi jiski training chahiye.

---

## 3. Unit economics — ₹999 ka poora ganit

### 3.1 WhatsApp ka asli kharcha (verified)

| Message type | India rate (2026) |
|---|---|
| Marketing | **₹0.8631**/message (Jan 2026 mein 10% badha) |
| Utility | **₹0.115**/message |
| Authentication | ₹0.115/message |
| Service (window ke andar) | **abhi free** |

Upar se **18% GST** (Meta ki charge OIDAR import maani jaati hai) aur BSP ka
platform fee + **10–45% markup**.

> ⚠️ **1 October 2026 se service/utility messages window ke andar bhi
> chargeable ho jayenge (₹0.115)** — yaani aaj ke free replies do mahine baad
> paise wale ho jayenge. Pricing isi ko maan ke banao, aaj ke free par nahi.

### 3.2 Kyun BSP subscription model se ₹999 nahi chalta

| BSP | Retail plan |
|---|---|
| Interakt | ~₹999–₹2,499/mo |
| AiSensy | ₹1,500 (Basic) → ₹2,199 (Growth) → ₹4,899 (Pro) |
| Wati | ~₹2,499–₹6,999/mo |

**Har client ka apna BSP subscription = aapka poora ₹999 khatam.** Isliye:

### 3.3 Tech Provider ka rasta (business ka core)

Tech Provider ban jaane par **client Meta ko seedha pay karta hai**, Meta ke
published rate par — aap sirf platform ka paisa lete ho.

- **Requirement:** ~10 client onboard + 2,500 daily conversations
- **Approval:** 1–4 hafte, technical + business review
- **Milta hai:** white-label signup (aapki branding, BSP ki nahi), managerial
  independence, aur **zero message-cost liability**

**Rasta:**
1. **Client 1–10:** kisi BSP ka **ISV/reseller** banke — co-branded embedded
   signup. Yahan margin patla rahega, ise **customer acquisition cost** maano
2. **10 par:** Tech Provider apply karo
3. **Approval ke baad:** WhatsApp ka kharcha aapki balance sheet se poori
   tarah bahar

### 3.4 Per-shop P&L (Tech Provider ke baad)

| Line | ₹/mahina |
|---|---|
| Revenue | **999** |
| Hosting (shared multi-tenant) | −60 |
| LLM (Gemini Flash free tier / Haiku with cap) | −100 se −250 |
| Payment gateway (~2%) | −20 |
| WhatsApp messages | **0** (client Meta ko seedha) |
| **Gross profit** | **~₹670–₹820 (67–82%)** |

**Shart:** LLM ka kharcha per-tenant cap hona chahiye. Ek baatuni dukaan poora
margin kha sakti hai. `llm_monthly_budget_usd` setting pehle se hai — usse
**hard limit** banao, sirf dikhawa nahi.

### 3.5 Revenue ladder

| Milestone | Shops | MRR | Gross profit |
|---|---|---|---|
| Pilot | 3 (free) | ₹0 | case study banana hai |
| Tech Provider unlock | 10 | ₹9,990 | ~₹7,000 |
| Break-even (founder salary) | 40–50 | ~₹45,000 | ~₹35,000 |
| Asli business | 100 | ₹99,900 | ~₹75,000 |
| Team ban sakti hai | 500 | ₹5 lakh | ~₹3.75 lakh |

**Doosri kamai:** setup fee ₹1,999 × har client · marketing message add-on
(₹0.8631 pass-through + margin) · thermal printer + QR tags par hardware
margin · "hum aapke liye campaign chalayenge" service.

---

## 4. Technical scale — naapa hua sach

### 4.1 Per shop: system bahut zyada bada hai

500 order/mahina ≈ 150–300 message/din. Ek instance isse **50 guna** jhel
lega. Yahan koi kaam nahi hai.

### 4.2 Fleet: aaj ke model par 50–100 tenant NAHI chalenge

| Deewar | Kyun | Kahan tootegi |
|---|---|---|
| **DB connections** | 10 steady/instance × N vs `max_connections=100` | **~10 tenant** |
| **RAM** | 111 MB/instance idle, ~200 MB peak | ~50 tenant / 16GB VM |
| **Schedulers** | N schedulers sab :00 par, sab 21:30 par `pg_dump` | ~20 tenant (disk) |
| **Migrations** | har release = N × alembic + N restart | ~15 tenant (manual) |
| **Threads endpoint** | har 12s **saare** conversation-wale customers uthata hai, Python mein sort | ~2,000 customer/shop |

**Aaj ka imaandar ceiling: 10–15 tenant.** Pool tune + pgbouncer + scheduler
jitter se 25–40.

### 4.3 Faisla: `tenant_id` ABHI daalo

v1 ne kaha tha "50+ par shared multi-tenant sochna". **Wo galat tha.** 50 alag
databases ko baad mein ek schema mein merge karna mahinon ka khatarnak project
hai.

**Sahi rasta:**
- **Abhi** (1 tenant par): har core table par `tenant_id`, request/job scope
  mein tenant context, unique constraints per-tenant (`(tenant_id,
  order_number)`, `(tenant_id, phone)`). **2 din ka kaam.** 30 shops par yahi
  2 mahine ka hoga
- **Deploy phir bhi alag-alag** — isolation ka fayda banaa rahe
- **Baad mein** consolidate karna sirf config badalna reh jayega, data
  migration nahi

### 4.4 ₹999 kya majboor karta hai

Ye price do luxuries afford nahi kar sakta:

| Luxury | ₹999 par kyun nahi | Kya karna hai |
|---|---|---|
| Done-for-you onboarding (3 ghante/client) | 100 shops = 300 ghante | **Self-serve wizard** (P0) |
| Instance-per-tenant hamesha | 100 process/DB/scheduler | Shared multi-tenant (tenant_id abhi) |

### 4.5 Fix list (scale ke liye)

1. `pool_size` 10 → **3**, `max_overflow` 20 → 5, aage pgbouncer
2. Scheduler jobs par **random jitter** (0–15 min), backup window stagger
3. **Threads endpoint SQL-level paginate** karo — Python sort hatao (ye maine
   khud chhoda hai: output paged hai, query nahi)
4. `webhook_events` + `conversations` par retention/archive job
5. `llm_usage` par **hard per-tenant cap** + degrade (smart → cheap model)

---

## 5. Product gaps — client #1 (bahar wala) se pehle

### P0 — inke bina bech nahi sakte

**5.1 Asli authentication + users**
Aaj: ek `X-API-Key` browser ke localStorage mein.
- Email/phone + password, session, logout, reset
- **Roles:** Owner · Manager (settings nahi) · Staff (sirf apna kaam) ·
  Accountant (read-only)
- Audit log mein asli user id (aaj "dashboard" likha jaata hai)

**5.2 Self-serve onboarding wizard (target: 20 minute)**
1. Shop profile — naam, address, timing, logo, GST
2. **WhatsApp connect** — BSP embedded signup (ISV) → baad mein white-label
3. Rate card — 60-item laundry template ya **Excel import** *(ban chuka hai)*
4. Staff + roles
5. Templates apne aap Meta ko submit
6. Pehla test bill → "Aap live hain"

**Activation metric:** signup → pehla asli bill **< 24 ghanta** (target 80%)

**5.3 Billing & subscription**
- Razorpay subscription, 14-din trial (card ke bina)
- Plan limits code mein lagu — orders/mahina, staff seats, campaigns, AI budget
- GST invoice, dunning (3 reminder → **read-only mode**, delete nahi)

**5.4 Support & diagnostics**
- In-app "problem batao" → screenshot + last 50 log lines
- Owner-side impersonation (audit ke saath)
- Status page + release notes

**5.5 Data ownership + exit**
- Poora export (customers, orders, payments)
- **WABA ownership contract mein** — 6.3 dekho
- Account + data delete request

### P1 — dukaan ka daily kaam (churn rokta hai)

| Feature | Kyun |
|---|---|
| **Counter POS + thermal print (58/80mm)** | Parcha aaj bhi haath se. Sabse zyada maanga jaane wala |
| **Garment QR/barcode tagging** | "Kaun sa kapda kiska" — sabse bada operational dard |
| **Customer self-serve link** (app nahi) | `/o/KK-...` — status, bill, UPI pay, re-order |
| **UPI payment link + auto-reconcile** | Udhaar ka poora hisaab |
| **Udhaar ledger + reminder ladder** | Har dukaan ka sabse bada leak |
| **Multi-branch** | 2+ outlet wale hi sabse zyada paisa dete hain (upsell) |

### P2 — profit aur growth
Delivery route planning · loyalty/wallet/referral · reviews automation
(aadha ban chuka) · attendance + payroll · inventory · expense P&L

### P3 — moat
**Cross-shop benchmark** ("aapka average bill area ke 12 shops se 18% kam
hai") — ye sirf SaaS wale ke paas ho sakta hai · owner AI copilot dashboard
mein · franchise/HQ mode

### Explicitly NAHI
Mobile app (WhatsApp hi app hai) · multi-language UI · aggregator banna ·
shared multi-tenant *deployment* 40 client se pehle (schema abhi, deployment baad mein)

---

## 6. Risk register

| Risk | Asar | Kya karein |
|---|---|---|
| **WhatsApp number ban** | Client ka business ruk gaya, wo aapko blame karega | Freq cap + opt-out (pehle se hai) · template quality rating monitor · per-tenant alag WABA (blast radius ek shop) |
| **Oct 2026: service messages chargeable** | Per-shop cost badhega | Pricing abhi se isi par banao · utility message optimize karo |
| **BSP subscription model** | ₹999 model mar jayega | **Tech Provider** — 3.3 |
| **WABA ownership** | Pehle churned client se ladai, community mein badnami | **Contract mein din 1 se: number client ka, export unka haq** |
| LLM cost blowout | Margin gaya | Hard per-tenant cap + cheap-model fallback |
| **Founder capacity** | 30–40 shop par deewar | Self-serve onboarding 30 se pehle, ya banda rakho |
| Data loss | Client chala gaya | **Restore drill mahine mein 1** — bina test kiya backup sirf kaagaz hai |
| Churn (use hi nahi kiya) | MRR gaya | Activation metric par nazar, pehle 30 din handhold |

---

## 7. Go-to-market

**Sabse bada asset: aapki apni dukaan ka asli data.** Feature list nahi —
"mere yahan pichhle mahine 62 purane customer wapas aaye, ye screenshot."

**Phase 1 — 3 pilot (0–2 mahina)**
Varanasi ke 3 non-competing dukaan, free 3 mahina. Aap khud onboard karo aur
**setup ka time naapo** — wahi asli product metric hai. Nikaalna hai: 2 case
study + 1 testimonial video.

**Phase 2 — 10 paid (2–5 mahina)** → **Tech Provider unlock**
Dry cleaners association, local WhatsApp groups. Referral: laane wale ko 1
mahina free. 10 client + 2,500 daily conversations par Tech Provider apply.

**Phase 3 — 50 (5–12 mahina)**
Self-serve trial + onboarding wizard. Doosre shehar mein local partner
(commission). Content: "laundry business kaise badhaye" — Hindi YouTube/IG.

**Phase 4 — 100+ (12–24 mahina)**
Shared multi-tenant deployment · pehla support hire · P1 features par upsell
(POS, tagging, multi-branch).

---

## 8. Milestones

| M | Kya | Kyun |
|---|---|---|
| **M0** | `tenant_id` schema mein (2 din) | Aaj sabse sasta, kal sabse mehnga |
| **M1** | Auth + roles · cloud hosting + domain · self-serve onboarding | Iske bina bech nahi sakte |
| **M2** | Control plane — provisioning, central rollout, monitoring | 15 client bina iske nahi sambhalenge |
| **M3** | BSP/ISV integration · Razorpay · plan limits · trial | Ab paise lene layak |
| **M4** | 3 pilot live + case study | Asli feedback |
| **M5** | **Tech Provider approval** (10 client par) | Yahan business profitable banta hai |
| **M6** | POS + thermal print + QR tagging | Sabse zyada maanga jaane wala |
| **M7** | Shared multi-tenant deployment · self-serve signup | 100+ |

**Order matter karta hai. M0→M1→M3 se pehle koi naya laundry feature nahi.**

---

## 9. Success metrics

**Product** — Activation: signup → pehla bill < 24h (80%) · Owner weekly
active 3+ din · WhatsApp delivery > 95%, quality GREEN · **AI containment**
(kitne message bina insaan ke handle hue — aaj measure nahi hota, `escalations`
vs total inbound se nikalo)

**Business** — MRR · net revenue retention · logo churn < 3%/mo · CAC payback
< 3 mahina · support tickets/shop/mahina (ghatna chahiye) · **onboarding time**
(3 ghante → 20 minute)

**Reliability** — uptime > 99.5%/tenant · `dead` rows = 0 · restore drill
mahine mein 1, pass

---

## 10. Abhi ke agle 6 kaam

1. **`tenant_id` schema mein daalo** — 2 din, aaj; 30 shops par 2 mahina
2. **Hosting cloud par** — tunnel hatao, domain + TLS. Sab isi par khada hai
3. **Login + roles** — `X-API-Key` hatao (`ROADMAP.md` mein pehle se likha)
4. **BSP chuno aur ISV banno** — provider switch pehle se hai, teesra adapter
5. **Pool 10 → 3** + scheduler jitter + threads endpoint SQL paginate
6. **Restore drill chalao** — aaj ke backup se test DB khada karke dekho

---

## Appendix A — jo pehle se hai (dobara mat banao)

26 tables: `customers, orders, order_status_history, payments, rate_card,
coupons, coupon_redemptions, staff, tasks, conversations, escalations,
open_questions, faq_entries, corrections, doc_chunks, campaigns,
campaign_recipients, leads, expenses, llm_usage, audit_log, settings_kv,
sent_events, webhook_events, outbound_queue, alembic_version`

Provider abstraction (Meta/DotPe), LLM abstraction (Anthropic/Gemini),
settings hot-reload, template studio, backup + keepalive — sab
tenant-agnostic. **Code fork karne ki zaroorat kabhi nahi padegi.**

## Appendix B — sources (verify before quoting)

- WhatsApp India rates: [MyOperator](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026) · [Whautomate rate card](https://whautomate.com/whatsapp-business-api-pricing-india) · [ChatMaxima](https://chatmaxima.com/whatsapp-api-pricing/india/)
- BSP pricing: [AiSensy](https://aisensy.com/pricing) · [Codingclave comparison](https://codingclave.com/blog/wati-vs-interakt-vs-aisensy-2026)
- Tech Provider program: [Whatsboost guide](https://whatsboost.in/blog/how-to-become-a-whatsapp-partner-meta-isv-step-by-step-guide-for-agencies-tech-providers-saas-builders) · [Whautomate: Tech Provider vs BSP](https://whautomate.com/whatsapp-tech-provider-vs-bsp) · [Interakt](https://www.interakt.shop/blog/become-a-whatsapp-business-partner/)
- Competitor: [Quick Dry Cleaning single-store pricing](https://www.quickdrycleaning.com/single-store-pricing/) · [Techjockey](https://www.techjockey.com/detail/quick-dry-cleaning)
- Market size: [IMARC India laundry](https://www.imarcgroup.com/india-laundry-service-market) · [Laundrywala](https://www.laundrywala.in/laundry-and-dry-cleaning-franchise-in-india/)

> Rate cards badalte rehte hain aur BSP markup alag-alag hai. Price final
> karne se pehle aaj ka Meta rate card + apne BSP ka quote dono check karo.
