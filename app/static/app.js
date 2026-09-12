/* Kwik Klin — Laundry Pro dashboard logic.
   All UI strings live in T (single place to change wording). */

"use strict";

if (new URLSearchParams(location.search).get("probe")) {
  document.title = "PROBE-BOOT";
  window.addEventListener("error", (e) => {
    document.title = "JSERR: " + e.message + " @line " + e.lineno;
  });
}

/* ============================= strings ============================= */
const T = {
  saved: "Saved", deleted: "Deleted", sent: "Sent", created: "Created",
  errGeneric: "Something went wrong", tryAgain: "Try again",
  confirmDelete: "Delete this? This cannot be undone.",
  noData: "Nothing here yet",
  loading: "Loading…",
  billCreated: "Bill created",
  paymentSaved: "Payment recorded",
  statusUpdated: "Status updated",
  dateUpdated: "Delivery date updated",
  reminderSent: "Payment reminder sent",
  windowClosed: "24h window is closed — the customer must message first",
};

const STATUS_LABEL = {
  RECEIVED: "New", PICKUP_ASSIGNED: "Pickup assigned", PICKED_UP: "Picked up",
  IN_WASH: "Washing", IN_DRY: "Drying", IN_IRON: "Ironing",
  READY: "Ready", OUT_FOR_DELIVERY: "Out for delivery", DELIVERED: "Delivered",
  ON_HOLD: "On hold", CANCELLED: "Cancelled",
};
// a status we forgot to name must still read as itself, never "undefined"
const statusName = (s) => STATUS_LABEL[s] || String(s || "").replace(/_/g, " ").toLowerCase();
const STATUS_SEQ = ["RECEIVED", "IN_WASH", "IN_DRY", "IN_IRON", "READY", "OUT_FOR_DELIVERY", "DELIVERED"];
const SEGMENT_LABEL = {
  new: "New customers", active_regular: "Active regulars", at_risk: "At risk",
  lapsed: "Lapsed (60–120d)", lost: "Lost (120d+)", high_value: "High value",
  outstanding_dues: "Has dues",
};

/* ============================= core ============================= */
let KEY = localStorage.getItem("kk_admin_key") || "";
const qs = new URLSearchParams(location.search);
if (qs.get("key")) {
  KEY = qs.get("key");
  localStorage.setItem("kk_admin_key", KEY);
  history.replaceState({}, "", location.pathname + location.hash);
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-API-Key": KEY }, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { showLogin(); throw new Error("Please sign in"); }
  if (!r.ok) {
    let d = T.errGeneric;
    try { d = (await r.json()).detail || d; } catch (e) {}
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* helpers */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/* Junk customer records exist ("." / "—" / blank names). Anywhere a name is
   shown, fall back to the phone number instead of a dot or a blank. */
const JUNK_NAME = /^[\s.\-—–_,'"]*$/;
const displayName = (name, phone) => {
  const n = String(name || "").trim();
  return JUNK_NAME.test(n) ? (phone || "—") : n;
};
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const money = (v) => "₹" + inr.format(Number(v || 0));
const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
};
/* Clock only. A chat bubble under a "05 Aug" day header that also says
   "05 Aug" tells you nothing — you want the time. */
const fmtClock = (iso) =>
  new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
const dayName = (iso) => {
  const d = new Date(iso), now = new Date();
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return "";
};
const fmtWhen = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  const named = dayName(iso);
  if (named === "Today") return fmtClock(iso);
  if (named === "Yesterday") return "Yesterday";
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
};
function toast(msg, err = false, ms = 0) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms || (err ? 5000 : 2600));
}
const skeleton = (n = 4) => Array.from({ length: n }, () => '<div class="skel skelrow"></div>').join("");
const emptyBox = (msg, ico = "🧺") => `<div class="empty"><div class="ico">${ico}</div>${esc(msg)}</div>`;
const errBox = (msg, retry) => `<div class="errbox">⚠️ ${esc(msg)}<br><br><button class="btn ghost" onclick="${retry}()">${T.tryAgain}</button></div>`;
function openModal(html) {
  $("modal-body").innerHTML = html;
  $("modal-ov").classList.add("open");
  const first = $("modal-body").querySelector("input, select, textarea, button");
  if (first) first.focus();
}
function closeModal() { $("modal-ov").classList.remove("open"); }
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("modal-ov") && $("modal-ov").classList.contains("open")) closeModal();
});
function confirmDialog(text, onYes) {
  openModal(`<h3>Confirm</h3><p>${esc(text)}</p>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn danger" id="cf-yes">Yes, continue</button></div>`);
  $("cf-yes").onclick = () => { closeModal(); onYes(); };
}
async function busy(btn, fn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>';
  try { await fn(); } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.innerHTML = old;
}
function dlCsvClient(filename, header, rows) {
  const csv = [header, ...rows].map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv" }));
  a.download = filename; a.click();
}

/* ---------------------------------------------------------- live updates
 *
 * Ajit ne phone se bill banaya aur owner ke khule hue dashboard par wo
 * dikha hi nahi — kyunki page ne dobara poocha hi nahi tha. Ab server
 * bata deta hai, aur page sirf WAHI hissa taaza karta hai jo abhi screen
 * par hai.
 *
 * Teen jaan-boojh kar liye gaye faisle:
 *
 * 1. **Server sirf ISHARA bhejta hai** ("order badla"), poora data nahi.
 *    Page apne hisaab se maangta hai — isliye kisi ka data galat
 *    connection par ja hi nahi sakta.
 * 2. **Refresh debounced hai.** Ek saath dus ishare aayen (bulk kaam) to
 *    ek hi refresh chalta hai, dus nahi.
 * 3. **SSE na chale to bhi kaam chalta rahe.** Purana browser, koi proxy
 *    jo stream kaat de — tab har 45 second par chup-chaap refresh. Dheema
 *    sahi, par owner ko kabhi purana data nahi dikhega.
 */
let LIVE = null, LIVE_TIMER = null, LIVE_FALLBACK = null, LIVE_SEEN = 0;

/* Stream par AAKHRI baar kab kuch aaya — event ho ya dhadkan (ping).
   Server har 20 second par dhadkan bhejta hai, isliye 60 second ki chuppi
   ka matlab hai stream sach mein mar chuki hai. */
const LIVE_SILENCE_MS = 60000;
function liveIsProven() { return LIVE_SEEN > 0 && Date.now() - LIVE_SEEN < LIVE_SILENCE_MS; }
function liveSeen() { LIVE_SEEN = Date.now(); }

function refreshCurrentSection() {
  // Sirf khuli hui screen — background mein baaki sab maangna bekaar hai
  if (CURRENT === "dashboard") loadDashboard();
  else if (CURRENT === "tasks") loadTasks();
  else if (CURRENT === "bills") loadBills();
  else if (CURRENT === "inbox") loadThreads();
}

function liveRefreshSoon() {
  clearTimeout(LIVE_TIMER);
  LIVE_TIMER = setTimeout(refreshCurrentSection, 400);
}

/* Poochhna HAMESHA chalu rehta hai — bas jab tak stream khud ko sabit kar
   rahi hai tab tak chup baitha rehta hai.
 *
 * Pehle ye ulta tha aur wahi ek line owner ke "phone se bana bill dashboard
 * par aata hi nahi" wali shikayat ki jad thi: fallback tabhi chalu hota tha
 * jab `onerror` TEEN baar aa jaye. Par EventSource ka niyam ye hai ki
 * 401/403 jaise jawab par browser connection band karke DOBARA JUDTA HI
 * NAHI — yani `onerror` sirf EK baar aata hai. Ginti kabhi teen tak
 * pahunchti hi nahi thi, isliye na stream chalti thi na polling: khula hua
 * dashboard hamesha ke liye purana ho jaata tha. Aur ye rozmarra ki baat
 * thi — tunnel ka URL badalte hi session cookie chali jaati hai aur page
 * purani admin key par chalta rehta hai, jise EventSource bhej hi nahi
 * sakta. */
function startPollingFallback() {
  if (LIVE_FALLBACK) return;
  LIVE_FALLBACK = setInterval(() => {
    if (document.hidden) return;      // chhupi tab ke liye data maangna bekaar
    if (liveIsProven()) return;       // stream zinda hai — usi se aa jayega
    refreshCurrentSection();
  }, 30000);
}

function startLiveUpdates() {
  startPollingFallback();             // pehle jaal, phir chhalaang
  if (!("EventSource" in window) || LIVE) return;
  try {
    LIVE = new EventSource("/admin/api/events", { withCredentials: true });
  } catch (e) {
    return;
  }
  // `onopen` sirf itna kehta hai ki header aa gaye. Asli saboot ye hai ki
  // stream par kuch GUZRA — "ready", dhadkan, ya koi khabar. Beech ka koi
  // proxy stream ko buffer kar de to headers aa jaate hain par kuch aata
  // nahi; us haalat mein bhi polling chalti rehni chahiye.
  LIVE.addEventListener("ready", liveSeen);
  LIVE.addEventListener("ping", liveSeen);
  LIVE.onmessage = () => { liveSeen(); liveRefreshSoon(); };
  LIVE.onerror = (e) => {
    // CLOSED = browser ne haar maan li (401/403 par wo dobara judta hi
    // nahi). Us waqt intezaar karne ka koi matlab nahi — polling hi ab
    // ekmatra rasta hai. Baaki haalat mein browser khud dobara judta hai.
    // Source event se lete hain, `LIVE` se nahi: neeche wo null ho jaata hai.
    const src = e && e.target;
    if (src && src.readyState === EventSource.CLOSED) {
      LIVE_SEEN = 0;
      LIVE = null;
      refreshCurrentSection();        // jitna peeche reh gaye, abhi pura karo
    }
  };
  // Tab wapas dikhi — jitni der chhupi thi utni der ka farak ek baar mein
  // poora kar lo (mobile browser chhupe tab ka stream rok dete hain).
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) liveRefreshSoon();
  });
}

async function kkLogout() {
  // dono cheezein hatao: purani admin key AUR asli session
  localStorage.removeItem("kk_admin_key");
  KEY = "";
  try {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  } catch (e) { /* offline — cookie waise bhi expire ho jayegi */ }
  location.href = "/#login";
}

/* login */
/* Session pehle, key baad mein. Dono na hon to login page par bhej do.
 *
 * Ek farak jo bahut maayne rakhta hai: "server ne kaha tum logged in nahi
 * ho" aur "server tak baat hi nahi pahunchi" — ye do alag baatein hain.
 * Pehle dono ka ek hi ilaaj tha, seedha /#login. Isliye phone par har
 * refresh par, jab pehli request signal ki wajah se gir jaati thi, login
 * page ek pal ko jhalak kar chala jaata tha — jabki session bilkul theek
 * tha. Ab network ki hichki par ek baar aur poochha jaata hai, aur phir
 * bhi baat na bane to aadmi jahan hai wahin rehta hai. */
async function ensureSignedIn() {
  let me = null, serverAnswered = false;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetch("/api/me", { credentials: "same-origin" });
      serverAnswered = true;
      me = await res.json().catch(() => null);
      break;
    } catch (e) {
      if (attempt === 0) await new Promise((r) => setTimeout(r, 800));
    }
  }
  if (me && me.user) {
    SIGNED_IN_AS = me;
    const btn = document.querySelector(".logout");
    if (btn) btn.textContent = "⏋ Log out " + (me.user.name || me.user.role);
    renderBillingBanner(me.subscription);
    renewCard(me.subscription);
    applyFeatureLocks(me.features || []);
    return true;                       // session kaafi hai, key ki zaroorat nahi
  }
  if (KEY) return true;                  // purani admin key se chal jayega
  if (!serverAnswered) {
    // Signal gaya hai, session nahi. Login par bhejna yahan galat jawab hai.
    toast("No signal — trying again…", true, 4000);
    setTimeout(() => ensureSignedIn().then((ok) => { if (ok) startLiveUpdates(); }), 5000);
    return false;
  }
  location.href = "/#login";             // server ne saaf kaha: session nahi
  return false;
}
let SIGNED_IN_AS = null;
/* Kya dukaan se WhatsApp bhej sakte hain? /api/me → tenant.wa_connected.
 * Pata na ho (purani admin-key login, session nahi) to haan maan lo —
 * tab bhejne ki koshish hoti hai aur fail par share modal khul jaata hai. */
function waConnected() {
  const t = SIGNED_IN_AS && SIGNED_IN_AS.tenant;
  return !t || t.wa_connected !== false;
}

/* Feature gating — /api/me ke `features` array se. Jo tab plan mein nahi
   hai wo 🔒 ke saath dikhta hai; click par Upgrade prompt. Naya gated tab
   banao to bas FEATURE_TABS mein entry daalo (backend plans.py ke saath). */
const FEATURE_TABS = {
  campaigns: "campaigns",
  reports: "reports",
  training: "service_agent",
  agents: "service_agent",
  usage: "reports",
};
const FEATURE_LABELS = {
  campaigns: "Campaigns", reports: "Reports",
  service_agent: "AI Service Agent", marketing_agent: "Marketing Agent",
};
let LOCKED_TABS = {};   // tab -> missing feature

function applyFeatureLocks(features) {
  const have = new Set(features);
  LOCKED_TABS = {};
  for (const [tab, feat] of Object.entries(FEATURE_TABS)) {
    const els = document.querySelectorAll(`[data-s="${tab}"]`);
    if (have.has(feat)) {
      els.forEach((el) => { el.classList.remove("locked"); });
      continue;
    }
    LOCKED_TABS[tab] = feat;
    els.forEach((el) => {
      el.classList.add("locked");
      if (!el.querySelector(".lock-ico")) {
        const ico = document.createElement("span");
        ico.className = "lock-ico";
        ico.textContent = " 🔒";
        el.appendChild(ico);
      }
    });
  }
}

function showUpgrade(feature) {
  const label = FEATURE_LABELS[feature] || feature;
  openModal(`<h3>🔒 ${esc(label)}</h3>
    <p>Ye feature aapke plan mein nahi hai. Upgrade karne par turant khul jayega —
    aapka data waise hi safe rahta hai.</p>
    <div style="margin-top:12px;display:flex;gap:8px">
      <a class="btn" href="/join#pricing" target="_blank">Plans dekhein</a>
      <button class="btn ghost" onclick="closeModal()">Baad mein</button>
    </div>`);
}


/* 7 din pehle ka renew card — banner se alag, kyunki ise dabana padta hai:
   "Pay now" billing page kholta hai, "Remind me later" 24 ghante chup.
   Ek hi din mein baar-baar chipakne se log ise andekha karne lagte hain. */
function renewCard(sub) {
  if (!sub) return;
  const days = sub.status === "trial" ? sub.days_left : null;
  const grace = sub.read_only ? (sub.grace_days_left ?? 0) : null;
  const due = (days != null && days <= 7) || sub.read_only || sub.locked;
  if (!due) return;
  try {
    if (Number(localStorage.getItem("kk_renew_snooze") || 0) > Date.now()) return;
  } catch (e) {}
  const title = sub.locked ? "Account locked"
    : sub.read_only ? "Your plan has ended"
    : days === 0 ? "Your trial ends today"
    : `Your trial ends in ${days} day${days === 1 ? "" : "s"}`;
  const line = sub.locked
    ? "Your data is safe. Renew to switch everything back on."
    : sub.read_only
      ? `You can still see everything; adding new bills is paused.${grace ? ` ${grace} days before the account locks.` : ""}`
      : "Renew now and nothing stops — orders, WhatsApp and the AI agent keep running.";
  const wrap = document.createElement("div");
  wrap.className = "renew-card";
  wrap.innerHTML = `
    <div class="rc-body">
      <div class="rc-ico">${sub.locked || sub.read_only ? "🔒" : "⏳"}</div>
      <div class="rc-text"><b>${esc(title)}</b><div class="muted">${esc(line)}</div></div>
    </div>
    <div class="rc-actions">
      <a class="btn" href="/billing">Pay / Recharge</a>
      <button class="btn ghost" id="rc-later">Remind me later</button>
    </div>`;
  document.body.appendChild(wrap);
  wrap.querySelector("#rc-later").onclick = () => {
    try { localStorage.setItem("kk_renew_snooze", String(Date.now() + 24 * 3600 * 1000)); } catch (e) {}
    wrap.remove();
  };
}

/* Trial/subscription banner — har page/tab par sabse upar (SPA hai, to ek
   hi banner sab jagah dikhta hai). /api/me ka `subscription` object yahi
   padhta hai; active par kuch nahi dikhta. */
function renderBillingBanner(sub) {
  const old = document.getElementById("billing-banner");
  if (old) old.remove();
  if (!sub || sub.status === "active") return;
  let cls = "warn", text = "";
  if (sub.status === "trial") {
    const d = sub.days_left == null ? "?" : sub.days_left;
    text = "🕒 Trial: " + d + " day" + (d === 1 ? "" : "s") + " left";
    if (sub.days_left != null && sub.days_left <= 2) cls = "danger";
  } else if (sub.locked) {
    cls = "danger";
    text = "🔒 Account locked hai — subscription renew karein. Aapka poora data safe hai.";
  } else if (sub.read_only) {
    cls = "danger";
    const g = sub.grace_days_left == null ? "" :
      " (" + sub.grace_days_left + " din mein account lock ho jayega)";
    text = "⚠️ Trial/subscription khatam — account READ-ONLY hai" + g + ". Data poora safe hai.";
  } else {
    return;
  }
  const div = document.createElement("div");
  div.id = "billing-banner";
  div.className = "billing-banner " + cls;
  div.textContent = text;
  document.body.prepend(div);
}

function showLogin() {
  openModal(`<h3>Sign in</h3><p class="muted">Enter your admin key to continue.</p>
    <div class="frm" style="margin-top:10px"><input id="login-key" type="password" placeholder="Admin key" autofocus></div>
    <div class="btnrow"><button class="btn" id="login-go">Sign in</button></div>`);
  $("login-go").onclick = async () => {
    KEY = $("login-key").value.trim();
    try {
      await api("/admin/api/staff");
      localStorage.setItem("kk_admin_key", KEY);
      closeModal(); toast("Welcome back!"); go(CURRENT);
    } catch (e) { toast("That key is not correct", true); }
  };
}

/* Debounce — search/filter typing must not re-render (or hit the API) per
   keystroke; 300ms after the last key is when the work happens. */
function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const renderOrdersDeb = debounce(() => renderOrders(), 300);
// Search box: har akshar par server nahi jaate — 300ms chup rahe to ek call
const renderBillsDeb = debounce(() => reloadBills(), 300);
const custSearchDeb = debounce(() => { CUST_SHOWN = 30; renderCustomers(); }, 300);

/* ============================= router ============================= */
const SECTIONS = ["dashboard", "inbox", "newbill", "bills", "customers", "expenses", "reports", "campaigns", "tasks", "agents", "usage", "training", "activity", "settings"];
const TITLES = {
  dashboard: ["Dashboard", "Today at a glance"],
  inbox: ["Inbox", "WhatsApp — see and reply yourself"],
  newbill: ["New bill", "Fast entry at the counter"],
  bills: ["Bill history", "Every order, filterable"],
  customers: ["Customers", "Ledger — business, paid and outstanding"],
  expenses: ["Expenses", "Daily spend and categories"],
  reports: ["Reports", "Revenue, expenses and profit"],
  campaigns: ["Campaigns", "Segments, offers and results"],
  tasks: ["Tasks", "Who was given what — pending, done, and replies"],
  agents: ["Agents", "Your AI employees — health and controls"],
  usage: ["AI usage", "How much AI was used and what it cost"],
  training: ["AI training", "Teach the agent your business"],
  activity: ["Activity", "Everything the agent did, and why"],
  settings: ["Settings", "Rates, staff, shop and agent"],
};
let CURRENT = "dashboard", NAVIGATING = false;
function go(sec, push = true) {
  if (!SECTIONS.includes(sec)) sec = "dashboard";
  if (LOCKED_TABS[sec]) { showUpgrade(LOCKED_TABS[sec]); return; }
  CURRENT = sec;
  closeSheet();
  // leaving a full-screen mobile chat closes it
  if (sec !== "inbox") closeThreadMobile(false);
  SECTIONS.forEach((s) => { const el = $("sec-" + s); if (el) el.style.display = s === sec ? "" : "none"; });
  // the Inbox is a full-height app pane, not a page that scrolls: it must
  // reach the bottom of the screen instead of leaving dead space under it
  document.body.classList.toggle("inbox-mode", sec === "inbox");
  document.querySelectorAll(".nav div[data-s]").forEach((el) => el.classList.toggle("on", el.dataset.s === sec));
  document.querySelectorAll(".tabbar div[data-s]").forEach((el) => el.classList.toggle("on", el.dataset.s === sec));
  $("mob-title").textContent = TITLES[sec][0];
  closeSidebar();
  if (push && location.hash !== "#" + sec) {
    NAVIGATING = true;
    location.hash = sec;  // creates a history entry -> Android back works
    setTimeout(() => (NAVIGATING = false), 0);
  }
  ({ dashboard: loadDashboard, inbox: loadThreads, newbill: initNewBill, bills: loadBills,
     customers: loadCustomers, expenses: loadExpenses, reports: loadReports,
     campaigns: loadCampaigns, tasks: loadTasks, agents: loadAgents, usage: loadUsage, training: loadTraining,
     activity: loadActivity, settings: loadSettings }[sec] || (() => {}))();
}

// Browser/Android back button: '#inbox/<phone>' = open thread, '#sec' = section
window.addEventListener("hashchange", () => {
  if (NAVIGATING) return;
  // Back/forward chalne par koi bhi khula overlay band — pehle modal ya
  // More-sheet naye section ke UPAR latka reh jaata tha aur lagta tha
  // "navigation atak gayi"
  closeModal(); closeSheet(); closeSidebar(); closeDrawer();
  const h = (location.hash || "#dashboard").slice(1);
  if (h.startsWith("inbox/")) {
    if (CURRENT !== "inbox") go("inbox", false);
    openThread(decodeURIComponent(h.slice(6)), false, false);
    return;
  }
  if (h.startsWith("settings/")) {  // deep link to one settings tab
    if (CURRENT !== "settings") go("settings", false);
    stTab(h.slice(9));
    return;
  }
  closeThreadMobile(false);
  go(h, false);
});

/* Sidebar drawer (phone/tablet) — scrim taps and navigation both close it */
function toggleSidebar() {
  const open = $("sidebar").classList.toggle("open");
  $("side-ov")?.classList.toggle("open", open);
}
function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("side-ov")?.classList.remove("open");
}

/* More sheet */
function openSheet() { $("sheet-ov").classList.add("open"); $("more-sheet").classList.add("open"); }
function closeSheet() { $("sheet-ov")?.classList.remove("open"); $("more-sheet")?.classList.remove("open"); }
function sheetGo(sec) { closeSheet(); go(sec); }

/* ============================= dashboard ============================= */
let DASH = null, SUMMARY = null, dashFilter = { status: "", pay: "", q: "", page: 1 };
const PAGE = 25;

async function loadDashboard() {
  $("kpis").innerHTML = skeleton(1) ;
  $("dash-orders").innerHTML = skeleton(5);
  try {
    [DASH, SUMMARY] = await Promise.all([api("/admin/api/dashboard"), api("/admin/api/reports/summary")]);
  } catch (e) {
    $("dash-orders").innerHTML = errBox(e.message, "loadDashboard");
    return;
  }
  renderKpis(); renderChips(); renderOrders();
  loadWaStats();
}
async function loadWaStats() {
  try {
    const s = await api("/admin/api/whatsapp/stats");
    const t = s.templates, q = s.quality;
    const qpill = q ? `<span class="pill ${q === "GREEN" ? "PAID" : "PARTIAL"}">quality ${q.toLowerCase()}</span>` : "";
    $("wa-stats").innerHTML = `
      <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center">
        <b style="margin:0">📱 WhatsApp aaj</b>
        <span>➡️ Bheje: <b>${s.today.sent}</b></span>
        <span>⬅️ Aaye: <b>${s.today.received}</b></span>
        <span>👥 Baat hui: <b>${s.today.customers_talked}</b> customers se</span>
        <span>📑 Templates: <b style="color:var(--ok)">${t.approved} ✓</b> · <b style="color:var(--warn)">${t.pending} pending</b>${t.rejected ? ` · <b style="color:var(--danger)">${t.rejected} ✗</b>` : ""}</span>
        ${qpill}
        ${s.meta_ok ? "" : '<span class="pill UNPAID">Meta API unreachable</span>'}
      </div>`;
  } catch (e) {
    $("wa-stats").innerHTML = `<span class="muted">📱 Could not load WhatsApp stats: ${esc(e.message)}</span>`;
  }
}

function renderKpis() {
  const c = DASH.counts, m = SUMMARY.month || {}, t = SUMMARY.today || {};
  const outstanding = CUSTOMERS_CACHE
    ? CUSTOMERS_CACHE.reduce((a, x) => a + Number(x.outstanding || 0), 0) : null;
  $("kpis").innerHTML = `
    ${kpi("New orders today", c.today_new, "", "go('bills')", "🧺", "orange")}
    ${kpi("Today's collection", money(t.revenue || 0), "", "go('reports')", "₹", "green")}
    ${kpi("Revenue this month", money(m.revenue || 0), "", "go('reports')", "📈", "blue")}
    ${kpi("Expenses this month", money(m.expenses || 0), "", "go('expenses')", "💸", "amber")}
    ${kpi("Profit this month", money(m.profit || 0), "revenue − expenses", "go('reports')", "💰", "teal")}
    ${kpi("Total outstanding", outstanding === null ? "…" : money(outstanding), "tap for the list", "go('customers')", "🏦", "red")}
  `;
  if (outstanding === null) loadCustomers(true).then(renderKpis);
}
const kpi = (lbl, val, sub, click, icon = "📊", tint = "blue") =>
  `<div class="card kpi" onclick="${click}"><span class="kico ${tint}">${icon}</span><div class="lbl">${lbl}</div><div class="val">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;

function renderChips() {
  const by = DASH.counts.by_status || {};
  const chips = [["", `All active <b>${DASH.counts.active_total}</b>`]]
    .concat(STATUS_SEQ.filter((s) => s !== "DELIVERED").map((s) => [s, `${statusName(s)} <b>${by[s] || 0}</b>`]));
  $("dash-chips").innerHTML = chips
    .map(([v, h]) => `<span class="chip ${dashFilter.status === v ? "on" : ""}" onclick="dashFilter.status='${v}';dashFilter.page=1;renderChips();renderOrders()">${h}</span>`)
    .join("");
}

function orderMatches(o) {
  if (dashFilter.status && o.status !== dashFilter.status) return false;
  if (dashFilter.pay && o.payment_status !== dashFilter.pay) return false;
  const q = dashFilter.q.toLowerCase();
  if (q && !(o.order_number.toLowerCase().includes(q) || (o.customer || "").toLowerCase().includes(q) || o.phone.includes(q))) return false;
  return true;
}
const isOverdue = (o) => o.expected_delivery && o.expected_delivery < new Date().toISOString().slice(0, 10) && !["DELIVERED", "CANCELLED"].includes(o.status);
const itemsText = (items) => (items || []).map((i) => `${i.qty} × ${i.type || i.garment || i.service}`).join(", ");

function renderOrders() {
  const all = (DASH.active_orders || []).filter(orderMatches);
  const pages = Math.max(1, Math.ceil(all.length / PAGE));
  dashFilter.page = Math.min(dashFilter.page, pages);
  const rows = all.slice((dashFilter.page - 1) * PAGE, dashFilter.page * PAGE);
  if (!rows.length) { $("dash-orders").innerHTML = emptyBox("No orders match — new bills appear here."); $("dash-pager").innerHTML = ""; return; }

  $("dash-orders").innerHTML = `
    <table class="tbl"><thead><tr><th>Order</th><th>Customer</th><th>Items</th><th>Status</th><th>Payment</th><th>Delivery</th><th>Actions</th></tr></thead>
    <tbody>${rows.map((o) => `
      <tr class="${isOverdue(o) ? "overdue" : ""}">
        <td><b>${o.order_number}</b><div class="muted">${fmtDate(o.created_at)}</div></td>
        <td>${esc(displayName(o.customer, o.phone))}<div class="muted">${esc(o.phone)}</div></td>
        <td style="max-width:140px" title="${esc(itemsText(o.items))}"><div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(itemsText(o.items))}</div></td>
        <td><span class="pill ${o.status}">${statusName(o.status)}</span>${isOverdue(o) ? ' <span class="pill UNPAID">Overdue</span>' : ""}</td>
        <td><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span><div class="muted">${money(o.amount_paid)} / ${o.total_amount ? money(o.total_amount) : "—"}</div></td>
        <td>${fmtDate(o.expected_delivery)}</td>
        <td><div class="act">
          <button class="btn sm ghost" title="Update status" aria-label="Update status" onclick="statusModal('${o.order_number}','${o.status}')">🔄</button>
          <button class="btn sm ghost" title="Collect payment" aria-label="Collect payment" onclick="paymentModal('${o.order_number}')">₹</button>
          <button class="btn sm ghost" title="More actions" aria-label="More actions" onclick="orderMenu('${o.order_number}')">⋯</button>
        </div></td>
      </tr>`).join("")}
    </tbody></table>
    <div class="rowcards">${rows.map((o) => `
      <div class="rowcard ${isOverdue(o) ? "overdue" : ""}">
        <div class="r1"><b>${o.order_number}</b><span class="pill ${o.status}">${statusName(o.status)}</span></div>
        <div class="kv"><span>${esc(displayName(o.customer, o.phone))}</span><span>${esc(o.phone)}</span></div>
        <div class="kv"><span class="muted">${esc(itemsText(o.items))}</span></div>
        <div class="kv"><span>Paid ${money(o.amount_paid)} of ${o.total_amount ? money(o.total_amount) : "—"}</span><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></div>
        <div class="kv"><span>Delivery</span><span>${fmtDate(o.expected_delivery)}${isOverdue(o) ? " ⚠️" : ""}</span></div>
        <div class="act">
          <button class="btn sm" onclick="statusModal('${o.order_number}','${o.status}')">Status</button>
          <button class="btn sm ghost" onclick="paymentModal('${o.order_number}')">Payment</button>
          <button class="btn sm ghost" onclick="orderDetail('${o.order_number}')">Details</button>
          <button class="btn sm ghost" onclick="jumpChat('${o.phone}')">Chat</button>
        </div>
      </div>`).join("")}
    </div>`;
  $("dash-pager").innerHTML = pages > 1
    ? `<button class="btn sm ghost" ${dashFilter.page <= 1 ? "disabled" : ""} onclick="dashFilter.page--;renderOrders()">‹ Prev</button>
       <span class="muted">Page ${dashFilter.page} of ${pages}</span>
       <button class="btn sm ghost" ${dashFilter.page >= pages ? "disabled" : ""} onclick="dashFilter.page++;renderOrders()">Next ›</button>`
    : "";
}

/* "⋯" overflow menus — 5 icons per row made the Actions column wider than
   the card at common laptop widths, pushing it behind a horizontal scroll
   nobody discovers. Two primary actions stay inline; the rest live here. */
function orderMenu(num) {
  const o = ((DASH && DASH.active_orders) || []).find((x) => x.order_number === num);
  openModal(`<h3>${num}</h3>
    <div class="frm">
      <button class="btn ghost" onclick="closeModal();dateModal('${num}')">📅 Delivery date</button>
      <button class="btn ghost" onclick="closeModal();orderDetail('${num}')">👁 Details</button>
      ${o ? `<button class="btn ghost" onclick="closeModal();jumpChat('${o.phone}')">💬 Open chat</button>` : ""}
    </div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
}
function billMenu(num) {
  openModal(`<h3>${num}</h3>
    <div class="frm">
      <button class="btn ghost" onclick="closeModal();printReceiptFromOrder('${num}')">🖨 Print receipt</button>
      <button class="btn ghost" onclick="closeModal();shareBillFromOrder('${num}')">📲 Share on WhatsApp</button>
      <button class="btn ghost" onclick="closeModal();editBillModal('${num}')">✏️ Edit bill</button>
      <button class="btn ghost danger-ic" onclick="closeModal();deleteBillModal('${num}')">🗑 Delete bill</button>
    </div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
}

function statusModal(number, current) {
  const nexts = STATUS_SEQ.slice(STATUS_SEQ.indexOf(current) + 1).concat(["ON_HOLD", "CANCELLED"]);
  openModal(`<h3>Update status — ${number}</h3>
    <p class="muted">Current: ${statusName(current)}. Customer is notified automatically on Ready / Out for delivery / Delivered.</p>
    <div class="frm" style="margin-top:10px">
      <select id="st-new">${nexts.map((s) => `<option value="${s}">${statusName(s)}</option>`).join("")}</select>
    </div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn" id="st-go">Update</button></div>`);
  $("st-go").onclick = (e) => busy(e.target, async () => {
    await api(`/orders/${number}/status`, { method: "POST", body: { status: $("st-new").value, changed_by: "dashboard" } });
    closeModal(); toast(T.statusUpdated); loadDashboard();
    if (typeof loadBills === "function" && $("bills-list")) loadBills();
  });
}

function paymentModal(number) {
  // prefill with the outstanding amount — one click for the common case
  const o = ((DASH && DASH.active_orders) || []).find((x) => x.order_number === number)
         || BILLS.find((x) => x.order_number === number);
  const due = o && o.total_amount
    ? Math.max(0, Number(o.total_amount) - Number(o.amount_paid || 0)) : "";
  const hint = o && o.total_amount
    ? `<div class="muted">Due: ${money(due)} (bill ${money(o.total_amount)}, received ${money(o.amount_paid || 0)}) — advance/extra is fine too</div>` : "";
  openModal(`<h3>Collect payment — ${number}</h3>
    <div class="frm">
      <div><label>Amount (₹)</label><input id="pm-amt" type="number" min="1" step="0.01" value="${due || ""}" autofocus></div>
      <div><label>Mode</label><select id="pm-mode"><option value="CASH">Cash</option><option value="UPI">UPI</option><option value="OTHER">Other</option></select></div>
      ${hint}
    </div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn ok" id="pm-go">Record payment</button></div>`);
  $("pm-go").onclick = (e) => busy(e.target, async () => {
    const amt = parseFloat($("pm-amt").value);
    if (!(amt > 0)) throw new Error("Amount must be greater than 0");
    await api(`/orders/${number}/payment`, { method: "POST", body: { amount: amt, method: $("pm-mode").value } });
    closeModal(); toast(T.paymentSaved); loadDashboard(); loadCustomers(true);
    // Bill history page has its own cache — refresh it too, else the
    // payment looks "not saved" when the modal was opened from there.
    if (typeof loadBills === "function" && $("bills-list")) loadBills();
  });
}

function dateModal(number) {
  openModal(`<h3>Delivery date — ${number}</h3>
    <div class="frm">
      <div><label>New date</label><input id="dt-new" type="date" value="${new Date(Date.now() + 864e5).toISOString().slice(0, 10)}"></div>
      <div><label>Internal reason (never sent to the customer)</label><input id="dt-why" placeholder="e.g. machine under repair"></div>
    </div>
    <p class="muted">The customer gets a polite notice with the new date only.</p>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn" id="dt-go">Update date</button></div>`);
  $("dt-go").onclick = (e) => busy(e.target, async () => {
    await api(`/orders/${number}/delivery-date`, { method: "POST", body: { expected_delivery: $("dt-new").value, changed_by: "dashboard", internal_reason: $("dt-why").value || null } });
    closeModal(); toast(T.dateUpdated); loadDashboard();
    if (typeof loadBills === "function" && $("bills-list")) loadBills();
  });
}

async function orderDetail(number) {
  $("drawer").classList.add("open");
  $("drawer-body").innerHTML = skeleton(4);
  try {
    const d = await api(`/orders/${number}`);
    const o = d.order;
    $("drawer-body").innerHTML = `
      <h3>${o.order_number} <span class="pill ${o.status}">${statusName(o.status)}</span></h3>
      <p class="muted">${esc(displayName(o.customer_name, o.customer_phone))} · ${esc(o.customer_phone)}</p><hr class="hr">
      <b>Items</b>
      ${(o.items || []).map((i) => `<div class="sumrow"><span>${i.qty} × ${esc(i.type || i.garment || i.service || "?")}</span><span>${i.amount != null ? money(i.amount) : ""}</span></div>`).join("")}
      <div class="sumrow"><span>Discount</span><span>${money(o.discount_amount || 0)}</span></div>
      <div class="sumrow"><span>GST</span><span>${money(o.gst_amount || 0)}</span></div>
      <div class="sumrow total"><span>Total</span><span>${o.total_amount ? money(o.total_amount) : "—"}</span></div>
      <div class="sumrow"><span>Paid</span><span>${money(o.amount_paid)} <span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></span></div>
      <hr class="hr"><b>Timeline</b>
      ${(d.history || []).map((h) => `<div class="sumrow"><span>${statusName(h.new_status) || h.new_status}</span><span class="muted">${fmtWhen(h.changed_at)} · ${esc(h.changed_by)}</span></div>`).join("")}
      ${o.notes ? `<hr class="hr"><b>Internal notes</b><p class="muted" style="white-space:pre-wrap">${esc(o.notes)}</p>` : ""}
      <div class="btnrow" style="margin-top:14px">
        <button class="btn ghost" onclick="printReceiptFromOrder('${o.order_number}')">🖨 Print</button>
        <button class="btn" onclick="jumpChat('${o.customer_phone}')">💬 Chat</button>
      </div>`;
  } catch (e) { $("drawer-body").innerHTML = errBox(e.message, "closeDrawer"); }
}
function closeDrawer() { $("drawer").classList.remove("open"); }
function jumpChat(phone) { go("inbox"); setTimeout(() => openThread(phone), 250); }

/* ============================= new bill ============================= */
let RATES = [], CUSTOMERS_CACHE = null, LINES = [];

async function initNewBill() {
  try {
    [RATES, SETTINGS_CACHE] = await Promise.all([api("/admin/api/rates"), api("/admin/api/settings")]);
  } catch (e) { toast(e.message, true); }
  if (!CUSTOMERS_CACHE) loadCustomers(true);
  if ($("nb-wa-notice")) $("nb-wa-notice").style.display = waConnected() ? "none" : "";
  if (!LINES.length) addLine();
  renderLines();
  const days = parseInt(SETTINGS_CACHE.turnaround_days) || 2;
  $("nb-date").value = new Date(Date.now() + days * 864e5).toISOString().slice(0, 10);
  $("nb-gst").checked = !!SETTINGS_CACHE.gst_default_on;
  $("nb-preset").innerHTML = '<option value="">No discount</option>' +
    (SETTINGS_CACHE.discount_presets || []).map((p, i) =>
      `<option value="${i}">${esc(p.name)} (${p.type === "percent" ? p.value + "%" : "₹" + p.value})</option>`).join("");
  calcBill();
}
/* Discount rules, made predictable:
   - a PRESET owns the discount: it fills the ₹ field, keeps it in sync as
     items change (a 10% preset stays 10%), and locks manual entry
   - "No discount" unlocks the ₹ field for manual entry
   - the note under the field says which one is actually applied
   - a coupon is validated at save by the server (noted in the UI) */
function activePreset() {
  const i = $("nb-preset").value;
  if (i === "") return null;
  return (SETTINGS_CACHE.discount_presets || [])[parseInt(i)] || null;
}
function applyPreset() {
  if (!activePreset()) $("nb-disc").value = "";
  calcBill();
}
function addLine() { LINES.push({ service: "", garment: "", qty: 1, rate: "", amount: 0 }); renderLines(); }
function delLine(i) { LINES.splice(i, 1); if (!LINES.length) addLine(); renderLines(); }
const services = () => [...new Set(RATES.filter((r) => r.is_active).map((r) => r.service))];
const garmentsFor = (svc) => RATES.filter((r) => r.is_active && r.service === svc);

function renderLines() {
  $("nb-lines").innerHTML = LINES.map((l, i) => `
    <div class="lineitem">
      <div class="lf lf-service"><span class="ll">Service</span>
        <select aria-label="Service" onchange="LINES[${i}].service=this.value;LINES[${i}].garment='';lineRate(${i})">
          <option value="">Service…</option>
          ${services().map((s) => `<option ${l.service === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select></div>
      <div class="lf lf-item"><span class="ll">Item</span>
        <select aria-label="Item" onchange="LINES[${i}].garment=this.value;lineRate(${i})">
          <option value="">Item…</option>
          ${garmentsFor(l.service).map((r) => `<option value="${esc(r.garment)}" ${l.garment === r.garment ? "selected" : ""}>${esc(r.garment || "(per kg)")} — ₹${r.rate}/${r.unit}</option>`).join("")}
        </select></div>
      <div class="lf lf-qty"><span class="ll">Qty / kg</span>
        <input type="number" min="0.1" step="0.1" value="${l.qty}" aria-label="Quantity" title="Qty / kg"
          onchange="LINES[${i}].qty=parseFloat(this.value)||1;calcBill()"></div>
      <div class="lf lf-rate"><span class="ll">Rate ₹</span>
        <input type="number" min="0" step="0.01" value="${l.rate}" placeholder="Rate" aria-label="Rate in rupees"
          onchange="LINES[${i}].rate=parseFloat(this.value)||0;calcBill()"></div>
      <div class="lf lf-amt"><span class="ll">Amount</span>
        <div class="money" id="nb-amt-${i}">${money(l.amount)}</div></div>
      <button class="btn sm danger del" aria-label="Remove item" title="Remove item" onclick="delLine(${i})">✕</button>
    </div>`).join("");
  calcBill();
}
function lineRate(i) {
  const l = LINES[i];
  const r = RATES.find((x) => x.service === l.service && x.garment === l.garment);
  if (r) l.rate = parseFloat(r.rate);
  renderLines();
}
function calcBill() {
  let sub = 0;
  LINES.forEach((l, i) => {
    l.amount = (parseFloat(l.rate) || 0) * (parseFloat(l.qty) || 0);
    sub += l.amount;
    const el = $("nb-amt-" + i); if (el) el.textContent = money(l.amount);
  });
  // preset owns the discount and tracks the subtotal live; manual otherwise
  const p = activePreset();
  const discEl = $("nb-disc");
  let disc;
  if (p) {
    disc = p.type === "percent" ? Math.round(sub * p.value) / 100 : Math.min(p.value, sub);
    discEl.value = disc || "";
    discEl.disabled = true;
  } else {
    discEl.disabled = false;
    disc = parseFloat(discEl.value) || 0;
  }
  const dnote = $("nb-disc-note");
  if (dnote) dnote.textContent = p
    ? `${p.name} applied${p.type === "percent" ? ` (${p.value}% of subtotal)` : ""} — manual entry is off`
    : (disc ? "Manual discount applied" : "");
  const pct = (parseFloat(SETTINGS_CACHE.gst_percent) || 18) / 100;
  const gst = $("nb-gst").checked ? Math.round((sub - disc) * pct * 100) / 100 : 0;
  const total = Math.max(0, sub - disc + gst);
  const adv = parseFloat($("nb-adv").value) || 0;
  $("nb-sub").textContent = money(sub);
  $("nb-gstamt").textContent = money(gst);
  $("nb-total").textContent = money(total);
  $("nb-due").textContent = money(Math.max(0, total - adv));
  return { sub, disc, gst, total, adv };
}
/* SECURITY: the customer's NAME comes from their WhatsApp profile — it is
   attacker-controlled text. It must never travel through an inline
   onclick="...'${name}'" JS string (a single quote breaks out = XSS).
   Rows are picked by INDEX into this array instead. */
let AC_HITS = [];
let AC_TIMER = null;      // debounce: har akshar par server nahi jaate
let AC_SEQ = 0;           // dher saare jawab aayen to sirf AAKHRI wala lagta hai

/* Naam ya number likhte hi purane customer ka sujhaav.
 *
 * Pehle ye chunaav BROWSER karta tha, `CUSTOMERS_CACHE` par — yani sirf un
 * teen sau customers par jo page ne utaare the. Jiska naam us list mein
 * nahi tha wo mila hi nahi karta tha, aur staff naya customer bana deta
 * tha; wahi ek grahak do baar dukaan mein baith jaata tha.
 *
 * Ab chunaav DB karta hai (indexed), aur network par sirf aath row aati
 * hain. Do cheezein zaroori thin:
 *   - **Debounce**: har keystroke par request nahi. 200ms chup rahe to ek
 *     request. "Anmol" par ek call, paanch nahi.
 *   - **Sequence**: dheema jawab tez jawab ke baad aa kar purane sujhaav
 *     na dikha de. Har request ka number hota hai; purana aaye to phenk
 *     diya jaata hai.
 *
 * SECURITY: customer ka naam unke WhatsApp profile se aata hai — yani
 * unka likha hua text. Wo kabhi inline onclick="...'${name}'" ke andar
 * nahi jaata (ek quote = XSS). Row INDEX se chuni jaati hai. */
function custAc(fieldId) {
  const isName = fieldId === "nb-name";
  const box = $(isName ? "nb-ac-name" : "nb-ac");
  const other = $(isName ? "nb-ac" : "nb-ac-name");
  if (other) other.innerHTML = "";
  if (!isName) { const err = $("nb-phone-err"); if (err) err.textContent = ""; }
  const q = $(fieldId).value.trim();
  clearTimeout(AC_TIMER);
  if (q.length < 2) { box.innerHTML = ""; return; }
  const mine = ++AC_SEQ;
  AC_TIMER = setTimeout(async () => {
    let hits = [];
    try {
      hits = await api(`/admin/api/customers/search?q=${encodeURIComponent(q)}`);
    } catch (e) {
      box.innerHTML = "";       // sujhaav ek suvidha hai — fail ho to chup
      return;
    }
    if (mine !== AC_SEQ) return;            // beech mein aur likh diya gaya
    AC_HITS = hits;
    box.innerHTML = hits.length
      ? hits.map((c, i) =>
          `<div onclick="pickCust(${i})">${esc(displayName(c.name, c.phone))} · ${esc(c.phone)}</div>`).join("")
      : `<div class="muted" style="cursor:default">No match — this will be a new customer</div>`;
  }, 200);
}

function pickCust(i) {
  const c = AC_HITS[i];
  if (!c) return;
  $("nb-phone").value = c.phone;
  $("nb-name").value = c.name || "";
  $("nb-ac").innerHTML = "";
  const nameBox = $("nb-ac-name");
  if (nameBox) nameBox.innerHTML = "";
}

/* Bahar tap karte hi sujhaav band — warna wo doosre field ke upar chipka
   reh jaata hai. */
document.addEventListener("click", (e) => {
  if (e.target.closest(".autocomplete")) return;
  ["nb-ac", "nb-ac-name"].forEach((id) => { const b = $(id); if (b) b.innerHTML = ""; });
});

async function saveBill(btn) {
  await busy(btn, async () => {
    // inline validation: the mistake is shown AT the field, not only a toast
    $("nb-phone-err").textContent = ""; $("nb-items-err").textContent = "";
    const t = calcBill();
    const phone = $("nb-phone").value.trim();
    if (!phone) {
      $("nb-phone-err").textContent = "Customer mobile number is required.";
      $("nb-phone").focus();
      return;
    }
    if (phone.replace(/\D/g, "").length < 10) {
      $("nb-phone-err").textContent = "That number looks too short — 10 digits needed.";
      $("nb-phone").focus();
      return;
    }
    const items = LINES.filter((l) => l.service && (l.garment || l.service.toLowerCase().includes("kg"))).map((l) => {
      const r = RATES.find((x) => x.service === l.service && x.garment === l.garment) || {};
      return { type: l.garment || l.service, service: l.service, qty: l.qty, rate: l.rate, amount: l.amount, unit: r.unit || "pc" };
    });
    if (!items.length) {
      $("nb-items-err").textContent = "Add at least one item — pick a service and an item.";
      return;
    }
    const body = {
      customer_phone: phone, customer_name: $("nb-name").value.trim() || null, items,
      total_amount: t.total, discount_amount: t.disc || null, gst_amount: t.gst || null,
      expected_delivery: $("nb-date").value || null, notes: $("nb-notes").value.trim() || null,
      advance_amount: t.adv || null, advance_method: t.adv ? $("nb-advmode").value : null,
      coupon_code: $("nb-coupon").value.trim() || null,
    };
    const out = await api("/orders", { method: "POST", body });
    toast(T.billCreated + " — " + out.order_number);
    showBillSuccess(out);
    LINES = []; addLine();
    ["nb-phone", "nb-name", "nb-disc", "nb-adv", "nb-notes", "nb-coupon", "nb-preset"].forEach((id) => ($(id).value = ""));
    calcBill();   // re-enables the manual discount field after a preset
    loadDashboard();
  });
}
function showBillSuccess(o) {
  const j = esc(JSON.stringify(o)).replace(/"/g, "&quot;");
  const wa = waConnected();
  // Jhooth mat bolo: API juda nahi to customer ko kuch nahi gaya.
  const line = wa
    ? "Customer notified on WhatsApp; staff got the work order."
    : "WhatsApp not connected — customer has <b>not</b> been messaged. Share the bill from your phone:";
  openModal(`<h3>✅ ${o.order_number} created</h3>
    <p class="muted">Total ${o.total_amount ? money(o.total_amount) : "—"} · ${esc(o.customer_name || o.customer_phone)}. ${line}</p>
    <div class="btnrow" style="margin-top:12px">
      <button class="btn ghost" onclick="printReceipt(${j})">🖨 Print receipt</button>
      ${wa ? `<button class="btn ghost" onclick="waBill(${j})">📲 Send from shop number</button>` : ""}
      <button class="btn ${wa ? "ghost" : ""}" onclick="closeModal();shareBillModal(${j})">💬 Share on WhatsApp</button>
      <button class="btn ${wa ? "" : "ghost"}" onclick="closeModal()">Done</button>
    </div>`);
}
function receiptText(o) {
  const s = SETTINGS_CACHE || {};
  const lines = (o.items || []).map((i) => ` ${i.qty} x ${i.type}  ${i.amount != null ? money(i.amount) : ""}`);
  let out = `KWIK KLIN — Laundry Pro`;
  if (s.shop_address) out += `\n${s.shop_address}`;
  if (s.shop_contact_phone) out += `\nPh: ${s.shop_contact_phone}`;
  if (s.shop_gstin) out += `\nGSTIN: ${s.shop_gstin}`;
  out += `\n------------------------------\nBill: ${o.order_number}\nCustomer: ${o.customer_name || o.customer_phone}\nDate: ${fmtDate(o.created_at || new Date().toISOString())}\n------------------------------\n${lines.join("\n")}\n------------------------------\nTotal: ${o.total_amount ? money(o.total_amount) : "—"}\nPaid: ${money(o.amount_paid || 0)}\nDue: ${o.total_amount ? money(o.total_amount - (o.amount_paid || 0)) : "—"}\nDelivery: ${fmtDate(o.expected_delivery)}\n------------------------------`;
  if (s.upi_vpa) out += `\nPay via UPI: ${s.upi_vpa}${s.upi_payee ? " (" + s.upi_payee + ")" : ""}`;
  out += `\n${s.invoice_footer || "Thank you! 🙏"}`;
  return out;
}
function printReceipt(o) { $("receipt").textContent = receiptText(o); window.print(); }
async function printReceiptFromOrder(number) {
  try { const d = await api(`/orders/${number}`); printReceipt(d.order); } catch (e) { toast(e.message, true); }
}
/* ---- bill ko WhatsApp par bhejna ----
 *
 * Do raaste hain, aur dono chahiye:
 *
 *   1. Dukaan ke apne WhatsApp number se (Cloud API). Sabse achha — customer
 *      ko shop ka number dikhta hai, thread inbox mein rehta hai. Par ye tabhi
 *      chalta hai jab tenant ne WhatsApp connect kiya ho AUR 24h window khuli ho.
 *
 *   2. wa.me deep link. Owner ke apne phone/WhatsApp Web se khulta hai, bill
 *      text pehle se bhara hua. Isme koi API, token ya setup nahi chahiye —
 *      isliye ye kabhi gayab nahi hota.
 *
 * Pehle sirf (1) tha, to jis dukaan ne API connect nahi kiya uske liye bill
 * bhejne ka koi rasta hi nahi bachta tha — button dabta tha aur error aata tha.
 * Ab (1) fail hone par seedha (2) khul jaata hai, aur (2) har bill par apne
 * aap bhi maujood hai.
 *
 * window.open() ko `await` ke baad bulana popup blocker khaa jaata hai (user
 * gesture khatam ho chuka hota hai), isliye fallback ek modal deta hai jisme
 * asli <a> link hota hai — us par click khud user ka gesture hai. */

/* wa.me sirf digits leta hai, "+" ya space ke bina. 10-ank ke local number
 * par default country code (91) lagta hai — baaki jaisa hai waisa. */
function waDigits(phone) {
  const d = String(phone || "").replace(/\D/g, "").replace(/^0+/, "");
  if (!d) return "";
  return d.length === 10 ? "91" + d : d;
}
function waShareUrl(phone, text) {
  const n = waDigits(phone);
  return `https://wa.me/${n}?text=${encodeURIComponent(text)}`;
}

let SHARE_TEXT = "";

/* Deep-link wala share — bina kisi API ke, hamesha kaam karta hai. */
function shareBillModal(o, note) {
  SHARE_TEXT = receiptText(o);
  const who = displayName(o.customer_name, o.customer_phone);
  openModal(`<h3>Share bill ${esc(o.order_number)}</h3>
    <p class="muted">${note ? esc(note) : `WhatsApp khulega, bill pehle se likha hua — bas Send dabana hai. To: ${esc(who)}`}</p>
    <pre class="sharetext">${esc(SHARE_TEXT)}</pre>
    <div class="btnrow" style="margin-top:12px">
      <a class="btn" href="${esc(waShareUrl(o.customer_phone, SHARE_TEXT))}" target="_blank" rel="noopener"
         onclick="closeModal()">📲 Open WhatsApp</a>
      <button class="btn ghost" onclick="copyShareText()">📋 Copy text</button>
      <button class="btn ghost" onclick="closeModal()">Close</button>
    </div>`);
}
async function copyShareText() {
  try { await navigator.clipboard.writeText(SHARE_TEXT); toast("Copied — paste it in WhatsApp"); }
  catch (e) { toast("Could not copy — select the text above and copy manually", true); }
}

/* Bill history se: order pehle server se lao, phir share modal. */
async function shareBillFromOrder(number) {
  try { const d = await api(`/orders/${number}`); shareBillModal(d.order); }
  catch (e) { toast(e.message, true); }
}

async function waBill(o) {
  if (!waConnected()) { shareBillModal(o); return; }   // API hai hi nahi — seedha share
  try {
    await api("/admin/api/inbox/send", { method: "POST", body: { phone: o.customer_phone, text: receiptText(o) } });
    toast(T.sent);
  } catch (e) {
    // API connect nahi hai / window band hai / send fail — kaam ruke nahi.
    shareBillModal(o, `Shop ke number se nahi bheja ja saka (${e.message}). Apne WhatsApp se bhej dijiye:`);
  }
}

/* ============================= bills ============================= */
let BILLS = [], billFilter = { q: "", status: "", pay: "", from: "", to: "", page: 1 };
/* Bills ki list ab SERVER se ek page aati hai.
 *
 * Pehle ye sabse naye 200 bill utaar kar browser mein chhaanti thi. Us
 * soch mein do khaamiyan hain jo dukaan badhte hi dikhti hain: 201-wa
 * bill kisi bhi tarah nahi milta — na search se, na page badalne se — aur
 * har baar poora 200 ka bojh phone par utarta hai.
 *
 * Ab jo dikhana hai wahi maanga jaata hai (25), aur `total` alag se aata
 * hai taaki "Page 3 of 47" sach bole. Search/filter DB tak jaate hain,
 * isliye pichhle saal ka bill bhi utni hi aasani se milta hai. */
let BILLS_TOTAL = 0, BILLS_SEQ = 0;

async function loadBills() {
  const f = billFilter;
  $("bills-list").innerHTML = skeleton(6);
  const p = new URLSearchParams({
    limit: PAGE, offset: (f.page - 1) * PAGE,
  });
  if (f.q) p.set("q", f.q);
  if (f.status) p.set("status", f.status);
  if (f.pay) p.set("payment", f.pay);
  if (f.from) p.set("date_from", f.from);
  if (f.to) p.set("date_to", f.to);
  const mine = ++BILLS_SEQ;
  let out;
  try {
    out = await api("/admin/api/bills?" + p.toString());
  } catch (e) {
    $("bills-list").innerHTML = errBox(e.message, "loadBills");
    return;
  }
  // Jaldi-jaldi type karne par purana jawab baad mein aakar nayi list na
  // mita de — sirf aakhri request ka natija lagta hai.
  if (mine !== BILLS_SEQ) return;
  BILLS = out.items;
  BILLS_TOTAL = out.total;
  renderBills();
}

/* Filter badla -> pehle page par wapas aur server se dobara maango.
   (Pehle ye sirf browser mein chhaanta tha, isliye alag function tha.) */
function reloadBills() { billFilter.page = 1; loadBills(); }

function renderBills() {
  const f = billFilter;
  const pages = Math.max(1, Math.ceil(BILLS_TOTAL / PAGE));
  if (!BILLS.length) {
    $("bills-list").innerHTML = emptyBox("No bills match these filters.", "🧾");
    $("bills-pager").innerHTML = "";
    return;
  }
  $("bills-list").innerHTML = `
    <table class="tbl"><thead><tr><th>Invoice</th><th>Customer</th><th>Items</th><th>Total / due</th><th>Status</th><th>Actions</th></tr></thead>
    <tbody>${BILLS.map((o) => billRowHtml(o, "tr")).join("")}</tbody></table>
    <div class="rowcards">${BILLS.map((o) => billRowHtml(o, "card")).join("")}</div>`;
  $("bills-pager").innerHTML = pages > 1
    ? `<button class="btn sm ghost" ${f.page <= 1 ? "disabled" : ""} onclick="billFilter.page--;loadBills()">‹ Prev</button>
       <span class="muted">Page ${f.page} of ${pages} · ${BILLS_TOTAL} bills</span>
       <button class="btn sm ghost" ${f.page >= pages ? "disabled" : ""} onclick="billFilter.page++;loadBills()">Next ›</button>` : "";
}
function billRowHtml(o, kind) {
  const due = o.total_amount ? Number(o.total_amount) - Number(o.amount_paid) : null;
  if (kind === "tr") return `<tr>
    <td><b>${o.order_number}</b><div class="muted">${fmtDate(o.created_at)}</div></td>
    <td>${esc(displayName(o.customer_name, o.customer_phone))}<div class="muted">${esc(o.customer_phone)}</div></td>
    <td style="max-width:150px" title="${esc(itemsText(o.items))}"><div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(itemsText(o.items))}</div></td>
    <td class="money">${o.total_amount ? money(o.total_amount) : "—"}${due > 0 ? `<div class="muted">due ${money(due)}</div>` : ""}</td>
    <td><div class="pillrow"><span class="pill ${o.status}">${statusName(o.status)}</span><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></div></td>
    <td><div class="act">
      <button class="btn sm ghost" title="Details" aria-label="Details" onclick="orderDetail('${o.order_number}')">👁</button>
      <button class="btn sm ghost" title="Collect payment" aria-label="Collect payment" onclick="paymentModal('${o.order_number}')">₹</button>
      <button class="btn sm ghost" title="More actions" aria-label="More actions" onclick="billMenu('${o.order_number}')">⋯</button>
    </div></td></tr>`;
  return `<div class="rowcard">
    <div class="r1"><b>${o.order_number}</b><span class="pill ${o.status}">${statusName(o.status)}</span></div>
    <div class="kv"><span>${esc(displayName(o.customer_name, o.customer_phone))}</span><span>${fmtDate(o.created_at)}</span></div>
    <div class="kv"><span>${o.total_amount ? money(o.total_amount) : "—"}</span><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></div>
    <div class="act"><button class="btn sm ghost" onclick="orderDetail('${o.order_number}')">Details</button>
    <button class="btn sm ghost" onclick="printReceiptFromOrder('${o.order_number}')">Print</button>
    <button class="btn sm ghost" onclick="shareBillFromOrder('${o.order_number}')">Share</button>
    <button class="btn sm ghost" onclick="paymentModal('${o.order_number}')">Payment</button>
    <button class="btn sm ghost" onclick="editBillModal('${o.order_number}')">Edit</button>
    <button class="btn sm danger" onclick="deleteBillModal('${o.order_number}')">Delete</button></div></div>`;
}

/* ---- bill edit / delete ---- */
function billByNumber(num) { return BILLS.find((b) => b.order_number === num); }

/* Edit bill opens as a BILL: one row per garment with qty and rate, the
   total adding itself up — not a box you retype text into. */
let EB = { num: "", lines: [] };

async function editBillModal(num) {
  const o = billByNumber(num);
  if (!o) return;
  await ensureRates();
  EB = {
    num,
    lines: (o.items || []).map((i) => ({
      item: i.type || i.garment || i.service || "",
      service: i.service || "",
      unit: i.unit || "pc",
      qty: Number(i.qty) || 1,
      rate: i.rate != null ? Number(i.rate) : "",
    })),
    paid: Number(o.amount_paid || 0),
    total: o.total_amount != null ? Number(o.total_amount) : null,
  };
  if (!EB.lines.length) EB.lines.push(ebBlank());

  openModal(`<h3>Bill — ${num}</h3>
    <p class="muted">${esc(o.customer_name || o.customer_phone)} · ${fmtDate(o.created_at)}</p>
    <div class="billedit">
      <div class="bl head"><span>Item</span><span>Qty</span><span>Rate</span><span>Amount</span><span></span></div>
      <div id="eb-lines"></div>
      <button class="btn ghost sm" onclick="ebAdd()" style="margin-top:8px">+ Add item</button>
      <div class="billtot">
        <div class="sumrow"><span>Total</span><span class="money" id="eb-sum">—</span></div>
        <div class="sumrow"><span>Received</span><span class="money">${money(EB.paid)}</span></div>
        <div class="sumrow total"><span>Due</span><span class="money" id="eb-due">—</span></div>
      </div>
    </div>
    <div class="frm" style="margin-top:14px">
      <div class="split2">
        <div class="setfield"><label for="eb-total">Total (₹) — adds up automatically</label>
          <input id="eb-total" type="number" min="0" step="0.01" value="${o.total_amount ?? ""}"
                 oninput="EB.manual=true;ebCalc()">
          <small>No rates entered? You can type the total here yourself.</small></div>
        <div class="setfield"><label for="eb-date">Delivery date</label>
          <input id="eb-date" type="date" value="${o.expected_delivery || ""}"></div>
      </div>
      <div class="setfield"><label for="eb-notes">Internal note (never sent to the customer)</label>
        <input id="eb-notes" value="${esc(o.notes || "")}"></div>
      <small class="fielderr" id="eb-err"></small>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="eb-save">Save changes</button>
    </div>`);
  ebRender();

  $("eb-save").onclick = (e) => busy(e.target, async () => {
    $("eb-err").textContent = "";
    const items = EB.lines
      .filter((l) => (l.item || "").trim())
      .map((l) => {
        const row = { type: l.item.trim(), qty: Number(l.qty) || 1 };
        if (l.rate !== "" && l.rate != null) row.rate = Number(l.rate);
        // keep what the rate card told us, so the receipt and the next
        // edit still know which service this garment was billed under
        if (l.service) row.service = l.service;
        if (l.unit) row.unit = l.unit;
        return row;
      });
    if (!items.length) { $("eb-err").textContent = "Add at least one item."; return; }
    const total = $("eb-total").value === "" ? null : parseFloat($("eb-total").value);
    if (total !== null && !(total >= 0)) { $("eb-err").textContent = "That total doesn't look right."; return; }
    await api(`/orders/${EB.num}`, { method: "PUT", body: {
      items, total_amount: total, expected_delivery: $("eb-date").value || null,
      notes: $("eb-notes").value, edited_by: "dashboard",
    }});
    closeModal(); toast("Bill updated"); loadBills(); loadDashboard();
  });
}

/* The kapda field is the rate card, not a typing box: pick the garment and
   its price comes along. Two things must never be lost — an item that is
   NOT on the card (old bill, one-off) stays selected as-is, and the last
   option drops the row back to free text so a new kapda is always billable. */
const ebBlank = () => ({ item: "", service: "", unit: "pc", qty: 1, rate: "" });
const kapdaList = () => RATES.filter((r) => r.is_active && (r.garment || "").trim());

async function ensureRates() {
  // Bill history can be opened without ever visiting New bill
  if (RATES.length) return;
  try { RATES = await api("/admin/api/rates"); } catch (e) { RATES = []; }
}

function ebKapda(l, i) {
  const list = kapdaList();
  if (l.custom || !list.length) {
    return `<div class="kapda kapdanew">
      <input id="eb-item-${i}" value="${esc(l.item)}" placeholder="garment name"
             oninput="EB.lines[${i}].item=this.value">
      ${list.length ? `<button class="btn sm ghost" title="Pick from list" onclick="ebFromList(${i})">☰</button>` : ""}
    </div>`;
  }
  const cur = (l.item || "").trim();
  const hit = list.findIndex((r) =>
    r.garment.toLowerCase() === cur.toLowerCase() && (!l.service || r.service === l.service));
  let opts = `<option value=""${cur ? "" : " selected"}>Pick a garment…</option>`;
  if (cur && hit < 0) opts += `<option value="keep" selected>${esc(cur)}</option>`;
  let svc = null;
  list.forEach((r, n) => {
    if (r.service !== svc) {
      if (svc !== null) opts += "</optgroup>";
      svc = r.service;
      opts += `<optgroup label="${esc(svc)}">`;
    }
    opts += `<option value="${n}"${n === hit ? " selected" : ""}>${esc(r.garment)} — ₹${r.rate}${r.unit === "kg" ? "/kg" : ""}</option>`;
  });
  if (svc !== null) opts += "</optgroup>";
  opts += `<option value="new">➕ New garment — not on the list</option>`;
  return `<select class="kapda" onchange="ebPick(${i},this.value)">${opts}</select>`;
}

function ebPick(i, val) {
  const l = EB.lines[i];
  if (val === "keep") return;
  if (val === "new") {
    l.custom = true; l.item = ""; l.service = "";
    // a price that came with the old garment is meaningless for a new one
    if (l.fromCard) { l.rate = ""; l.fromCard = false; }
    ebRender();
    const box = $("eb-item-" + i); if (box) box.focus();
    return;
  }
  if (val === "") { l.item = ""; l.service = ""; ebRender(); return; }
  const r = kapdaList()[parseInt(val)];
  if (!r) return;
  l.item = r.garment; l.service = r.service; l.unit = r.unit;
  l.rate = parseFloat(r.rate);   // card price, still editable in the Rate box
  l.fromCard = true;
  ebRender();
}
function ebFromList(i) { EB.lines[i].custom = false; ebRender(); }

function ebRender() {
  $("eb-lines").innerHTML = EB.lines.map((l, i) => `
    <div class="bl">
      ${ebKapda(l, i)}
      <input class="k-qty" type="number" min="0.1" step="0.5" value="${l.qty}"
             oninput="EB.lines[${i}].qty=parseFloat(this.value)||0;ebCalc()">
      <input class="k-rate" type="number" min="0" step="1" value="${l.rate}" placeholder="—"
             oninput="EB.lines[${i}].rate=this.value===''?'':parseFloat(this.value)||0;ebCalc()">
      <span class="money" id="eb-amt-${i}">—</span>
      <button class="btn sm ghost" title="Hatao" onclick="ebDel(${i})">✕</button>
    </div>`).join("");
  ebCalc();
}
function ebAdd() { EB.lines.push(ebBlank()); ebRender(); }
function ebDel(i) { EB.lines.splice(i, 1); if (!EB.lines.length) ebAdd(); else ebRender(); }

function ebCalc() {
  let sum = 0, anyRate = false;
  EB.lines.forEach((l, i) => {
    const amt = (Number(l.rate) || 0) * (Number(l.qty) || 0);
    if (l.rate !== "" && l.rate != null) anyRate = true;
    sum += amt;
    const cell = $(`eb-amt-${i}`);
    if (cell) cell.textContent = l.rate === "" || l.rate == null ? "—" : money(amt);
  });
  // rows win unless the owner typed a total himself
  if (anyRate && !EB.manual) $("eb-total").value = sum ? sum.toFixed(2) : "";
  const total = parseFloat($("eb-total").value);
  $("eb-sum").textContent = isFinite(total) ? money(total) : "—";
  $("eb-due").textContent = isFinite(total) ? money(Math.max(0, total - EB.paid)) : "—";
}

function deleteBillModal(num) {
  const o = billByNumber(num);
  if (!o) return;
  const paid = Number(o.amount_paid || 0);
  openModal(`<h3>Delete bill ${num}?</h3>
    <p class="muted">${esc(o.customer_name || o.customer_phone)} · ${o.total_amount ? money(o.total_amount) : "—"}</p>
    <p class="muted">The bill, its status history${paid > 0 ? ` and the ${money(paid)} payment record` : ""} — all gone, and there is no undo.</p>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn danger" id="db-go">Yes, delete it</button>
    </div>`);
  $("db-go").onclick = (e) => busy(e.target, async () => {
    await api(`/orders/${num}?deleted_by=dashboard`, { method: "DELETE" });
    closeModal(); toast(`${num} deleted`); loadBills(); loadDashboard();
  });
}
function billsCsv() {
  dlCsvClient("bills.csv",
    ["order", "date", "customer", "phone", "total", "paid", "status", "payment"],
    BILLS.map((o) => [o.order_number, o.created_at.slice(0, 10), o.customer_name || "", o.customer_phone, o.total_amount || "", o.amount_paid, o.status, o.payment_status]));
}

/* ============================= customers ============================= */
/* The cache stays at the server's default 300 (same as before) because the
   "Total outstanding" KPI and the reports top-list sum over it. The LIST
   renders a window of 30 and grows via "Aur dikhao" — past the cache it
   pages the API with limit/offset, so every customer stays reachable. */
let CUST_SHOWN = 30, CUST_MAYBE_MORE = false;
const CUST_CHUNK = 100;
async function loadCustomers(quiet = false) {
  if (!quiet) $("cust-list").innerHTML = skeleton(6);
  CUST_SHOWN = 30;
  try { CUSTOMERS_CACHE = await api("/admin/api/customers"); } catch (e) { if (!quiet) $("cust-list").innerHTML = errBox(e.message, "loadCustomers"); return; }
  CUST_MAYBE_MORE = CUSTOMERS_CACHE.length >= 300;
  if (!quiet || CURRENT === "customers") renderCustomers();
}
function visibleCustomers() {
  const q = (($("cust-search") && $("cust-search").value) || "").toLowerCase();
  return (CUSTOMERS_CACHE || [])
    .filter((c) => !q || (c.name || "").toLowerCase().includes(q) || c.phone.includes(q))
    .sort((a, b) => Number(b.outstanding) - Number(a.outstanding));
}
async function custMore(btn) {
  const all = visibleCustomers();
  if (CUST_SHOWN < all.length) { CUST_SHOWN += 50; renderCustomers(); return; }
  if (!CUST_MAYBE_MORE) return;
  btn.disabled = true;
  try {
    const r = await api(`/admin/api/customers?limit=${CUST_CHUNK}&offset=${CUSTOMERS_CACHE.length}`);
    CUSTOMERS_CACHE = CUSTOMERS_CACHE.concat(r);
    CUST_MAYBE_MORE = r.length >= CUST_CHUNK;
  } catch (e) { toast(e.message, true); btn.disabled = false; return; }
  CUST_SHOWN += 50;
  renderCustomers();
}
function renderCustomers() {
  if (!$("cust-list")) return;
  const all = visibleCustomers();
  if (!all.length) { $("cust-list").innerHTML = emptyBox("No customers yet — they appear after their first bill or message.", "👥"); return; }
  const rows = all.slice(0, CUST_SHOWN);
  const more = all.length > rows.length || CUST_MAYBE_MORE;
  const moreBtn = more
    ? `<div style="padding:12px;text-align:center"><button class="btn ghost sm" onclick="custMore(this)">⬇ Show more (${rows.length}${CUST_MAYBE_MORE ? "+" : " / " + all.length})</button></div>`
    : "";
  $("cust-list").innerHTML = `
    <table class="tbl"><thead><tr><th>Customer</th><th>Orders</th><th>Business</th><th>Paid</th><th>Outstanding</th><th>Last seen</th><th>Actions</th></tr></thead>
    <tbody>${rows.map((c) => `
      <tr><td>${esc(displayName(c.name, c.phone))}${c.opted_out ? ' <span class="tag">opted out</span>' : ""}<div class="muted">${c.phone}</div></td>
      <td>${c.total_orders} <span class="muted">(${c.active_orders} active)</span></td>
      <td class="money">${money(c.business)}</td><td class="money">${money(c.paid)}</td>
      <td class="money" style="color:${Number(c.outstanding) > 0 ? "var(--danger)" : "var(--ok)"}">${money(c.outstanding)}</td>
      <td class="muted">${c.last_message_at ? fmtWhen(c.last_message_at) : "—"}</td>
      <td><div class="act">
        ${Number(c.outstanding) > 0 ? `<button class="btn sm" onclick="sendReminder('${c.phone}','${c.outstanding}')">Remind</button>` : ""}
        <button class="btn sm ghost" onclick="jumpChat('${c.phone}')">💬</button>
        <button class="btn sm ghost" onclick="editCustomerModal('${c.phone}')">Edit</button>
        <button class="btn sm danger" onclick="deleteCustomerModal('${c.phone}')">Delete</button>
      </div></td></tr>`).join("")}
    </tbody></table>
    <div class="rowcards">${rows.map((c) => `
      <div class="rowcard"><div class="r1"><b>${esc(displayName(c.name, c.phone))}</b><span class="money" style="color:${Number(c.outstanding) > 0 ? "var(--danger)" : "var(--ok)"}">${money(c.outstanding)}</span></div>
      <div class="kv"><span>${c.phone}</span><span>${c.total_orders} orders</span></div>
      <div class="kv"><span>Business ${money(c.business)}</span><span>Paid ${money(c.paid)}</span></div>
      <div class="act">${Number(c.outstanding) > 0 ? `<button class="btn sm" onclick="sendReminder('${c.phone}','${c.outstanding}')">Remind</button>` : ""}
      <button class="btn sm ghost" onclick="jumpChat('${c.phone}')">Chat</button>
      <button class="btn sm ghost" onclick="editCustomerModal('${c.phone}')">Edit</button>
      <button class="btn sm danger" onclick="deleteCustomerModal('${c.phone}')">Delete</button></div></div>`).join("")}</div>${moreBtn}`;
}

/* ---- customer edit / delete ---- */
function customerByPhone(p) { return (CUSTOMERS_CACHE || []).find((c) => c.phone === p); }

function editCustomerModal(phone) {
  const c = customerByPhone(phone);
  if (!c) return;
  const digits = String(phone).replace(/^\+91/, "");
  openModal(`<h3>Edit customer</h3>
    <div class="frm">
      <div class="setfield"><label for="ec-name">Name</label>
        <input id="ec-name" value="${esc(c.name || "")}"></div>
      <div class="setfield"><label for="ec-phone">Phone</label>
        <input id="ec-phone" type="tel" inputmode="numeric" value="${esc(digits)}">
        <small>Changing the number moves their whole chat and all bills to the new number.</small>
        <small class="fielderr" id="ec-phone-err"></small></div>
      <div class="setfield"><label for="ec-addr">Address</label>
        <input id="ec-addr" value="${esc(c.address || "")}"></div>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="ec-save">Save changes</button>
    </div>`);
  $("ec-save").onclick = (e) => busy(e.target, async () => {
    $("ec-phone-err").textContent = "";
    const ph = $("ec-phone").value.replace(/\D/g, "");
    if (ph.length !== 10) { $("ec-phone-err").textContent = "Phone must be 10 digits."; return; }
    await api(`/admin/api/customers/${encodeURIComponent(phone)}`, { method: "PUT", body: {
      name: $("ec-name").value, phone: ph, address: $("ec-addr").value,
    }});
    closeModal(); toast("Customer updated"); loadCustomers();
  });
}

function deleteCustomerModal(phone) {
  const c = customerByPhone(phone);
  if (!c) return;
  const label = displayName(c.name, c.phone);
  const n = Number(c.total_orders || 0);
  openModal(`<h3>Delete ${esc(label)}?</h3>
    <p class="muted">${c.phone}</p>
    <p class="muted">${n
      ? `Their <b>${n} bill(s)</b>, payments and full chat history will be deleted too. Report numbers will change.`
      : "They have no bills. Their chat history will be deleted."}</p>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn danger" id="dc-go">Yes, delete them</button>
    </div>`);
  $("dc-go").onclick = (e) => busy(e.target, async () => {
    await api(`/admin/api/customers/${encodeURIComponent(phone)}?force=true`, { method: "DELETE" });
    closeModal(); toast(`${label} deleted`); loadCustomers(); loadDashboard();
  });
}
async function sendReminder(phone, amt) {
  try {
    await api("/admin/api/inbox/send", { method: "POST", body: { phone, text: `Namaste! Aapka ₹${amt} baaki hai. Jab suvidha ho, de dijiyega 🙏 — Kwik Klin` } });
    toast(T.reminderSent);
  } catch (e) { toast(e.message, true); }
}

/* Media ka URL. Login session ho to cookie hi kaafi hai; sirf purane
   admin-key wale rasta ke liye ?key= lagta hai. Khali key jodne se server
   401 deta tha aur Inbox mein toota hua dabba dikhta tha. */
const mediaUrl = (path) => KEY ? `${path}?key=${encodeURIComponent(KEY)}` : path;

/* ---- chat text + ticks, WhatsApp style ---- */
/* Trailing newlines in a stored/approved body rendered as a big empty hole
   under the message (white-space: pre-wrap). Trim, and never allow more
   than one blank line in a row. */
const tidy = (s) => String(s || "").replace(/[ \t]+$/gm, "").replace(/\n{3,}/g, "\n\n").trim();
const oneLine = (s) => tidy(s).replace(/\s+/g, " ");

/* ✓ sent · ✓✓ delivered · blue ✓✓ read · ⚠ failed — Meta's status, not a
   decoration. The old UI painted a blue ✓✓ on everything, so a message
   sitting undelivered looked read. */
function ticks(status) {
  if (status === "read") return ` <span class="tick read">✓✓</span>`;
  if (status === "delivered") return ` <span class="tick">✓✓</span>`;
  if (status === "failed") return ` <span class="tick fail">⚠</span>`;
  return ` <span class="tick">✓</span>`;   // sent, or an older row
}
let BY_WAMID = {};

/* Thread list previews: a file must read like WhatsApp's own list —
   "🎤 Voice note", not "[audio:/admin/media/in-f7ed7c2...]". */
const PREVIEW_ICON = {
  image: "📷 Photo", audio: "🎤 Voice note", voice: "🎤 Voice note",
  video: "🎥 Video", document: "📄 File", location: "📍 Location",
  contact: "📇 Contact", reaction: "", button: "", template: "📑 Template",
};
function previewText(raw) {
  const m = String(raw || "").match(/^\[(\w+)[:\]]([^\]]*)\]?\s*(.*)$/s);
  if (!m) return raw || "";
  const label = PREVIEW_ICON[m[1]];
  if (label === undefined) return raw;          // unknown marker: show as-is
  const rest = (m[3] || "").trim();
  // a transcribed voice note shows the words, like WhatsApp shows a caption
  return rest ? (label ? `${label}: ${rest}` : rest) : label;
}

/* Template bodies for the Inbox, so a sent template reads as the message
   the customer actually got instead of "[template:kk_thankyou_rating]".
   Meta is the source of truth; if it is unreachable the name chip still
   renders, so the inbox never regresses to raw brackets. */
let TPL_PREVIEW = {};
async function loadTplPreview() {
  if (Object.keys(TPL_PREVIEW).length) return;
  try {
    (await api("/admin/api/templates")).forEach((t) => {
      TPL_PREVIEW[t.name] = {
        body: t.body || "",
        buttons: (t.buttons || []).map((b) => b.text || b.type || ""),
      };
    });
  } catch (e) { /* Meta down — names still show */ }
}

/* ============================= AI usage ============================= */
const usd = (n) => "$" + Number(n || 0).toFixed(Number(n) >= 1 ? 2 : 4);
const kTok = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n || 0));

async function loadUsage() {
  $("usage-kpis").innerHTML = skeleton(1);
  $("usage-models").innerHTML = skeleton(3);
  let u;
  try { u = await api("/admin/api/usage"); }
  catch (e) { $("usage-models").innerHTML = errBox(e.message, "loadUsage"); return; }

  const capNote = u.daily_request_cap
    ? `${u.calls_left_today} left today`
    : "no limit set";
  const budgetNote = u.monthly_budget_usd
    ? `budget ${usd(u.monthly_budget_usd)}` : "no budget set";

  $("usage-kpis").innerHTML =
    kpi("AI calls today", u.today.calls, capNote, "", "⚡", "orange") +
    kpi("Tokens today", kTok(u.today.input_tokens + u.today.output_tokens),
        `in ${kTok(u.today.input_tokens)} · out ${kTok(u.today.output_tokens)}`, "", "🔤", "blue") +
    kpi("Spend this month", u.all_free ? "₹0 (free)" : usd(u.month.cost_usd),
        u.all_free ? "on the free tier" : budgetNote, "", "💰", "green") +
    kpi("Monthly at this rate", u.all_free ? "₹0" : usd(u.projected_month_usd),
        `${u.month.calls} calls so far`, "", "📈", "purple");

  // simple bar chart — no library, scales to the busiest day
  const s = u.series || [];
  const max = Math.max(1, ...s.map((d) => d.calls));
  $("usage-chart").innerHTML = s.length
    ? `<div style="display:flex;align-items:flex-end;gap:3px;height:130px">` +
      // tap = toast with the numbers; title alone is invisible on touch
      s.map((d) => `<div title="${d.date}: ${d.calls} calls, ${kTok(d.tokens)} tokens"
        onclick="toast('${d.date}: ${d.calls} calls, ${kTok(d.tokens)} tokens')"
        style="flex:1;min-width:0;background:var(--g-blue);border-radius:3px 3px 0 0;cursor:pointer;
        height:${Math.max(3, (d.calls / max) * 100)}%"></div>`).join("") + `</div>
      <div class="muted" style="display:flex;justify-content:space-between;margin-top:6px">
        <span>${s[0].date.slice(5)}</span><span>today</span></div>`
    : emptyBox("No AI calls recorded yet.", "📊");

  const rows = u.by_purpose || [];
  const totTok = rows.reduce((a, r) => a + r.tokens, 0) || 1;
  $("usage-purpose").innerHTML = rows.length
    ? rows.map((r) => `
      <div class="sumrow"><span>${esc(PURPOSE_LABEL[r.purpose] || r.purpose)}</span>
        <span>${r.calls} calls · ${kTok(r.tokens)}${u.all_free ? "" : " · " + usd(r.cost_usd)}</span></div>
      <div style="height:5px;background:var(--n100);border-radius:3px;margin-bottom:8px">
        <div style="height:5px;width:${Math.round((r.tokens / totTok) * 100)}%;background:var(--g-orange);border-radius:3px"></div>
      </div>`).join("")
    : `<p class="muted">Nothing this month yet.</p>`;

  // table on wide screens + stacked cards on phones — .tbl is hidden <768px
  const byModel = u.month.by_model || [];
  $("usage-models").innerHTML = `
    <p class="muted">Provider: <b>${esc(u.provider)}</b> · ${esc(u.models.smart)} / ${esc(u.models.cheap)}</p>
    <table class="tbl"><thead><tr><th>Model</th><th>Calls</th><th>Input</th><th>Output</th><th>Cost</th></tr></thead>
    <tbody>${byModel.map((m) => `<tr>
      <td><b>${esc(m.model)}</b></td><td>${m.calls}</td>
      <td>${kTok(m.input_tokens)}</td><td>${kTok(m.output_tokens)}</td>
      <td class="money">${m.priced ? usd(m.cost_usd) : '<span class="muted">free</span>'}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">No calls this month.</td></tr>`}</tbody></table>
    <div class="rowcards">${byModel.map((m) => `
      <div class="rowcard"><div class="r1"><b>${esc(m.model)}</b>
        <span class="money">${m.priced ? usd(m.cost_usd) : "free"}</span></div>
      <div class="kv"><span>${m.calls} calls</span><span>in ${kTok(m.input_tokens)} · out ${kTok(m.output_tokens)}</span></div>
      </div>`).join("") || `<p class="muted">No calls this month.</p>`}</div>
    <p class="muted" style="margin-top:10px">Rates and limits can be changed in Settings — past usage is re-priced with the new rates too.</p>`;
}

const PURPOSE_LABEL = {
  reply: "Customer replies", intent: "Understanding messages", extract: "Reading bills/commands",
  vision: "Bill from photo", query: "Your questions", marketing: "Writing campaigns",
  social: "Daily poster", other: "Other",
};

/* ============================= tasks ============================= */
let TASKS = [], taskFilter = "OPEN";

async function loadTasks() {
  $("task-list").innerHTML = skeleton(5);
  try {
    // STAFF is normally filled by the Settings page — the "naya kaam" form
    // needs it here too, so fetch it if we came straight to Tasks
    const [tasks, staff] = await Promise.all([
      api("/admin/api/tasks?status=ALL&limit=200"),
      STAFF.length ? Promise.resolve(STAFF) : api("/admin/api/staff"),
    ]);
    TASKS = tasks; STAFF = staff;
  } catch (e) { $("task-list").innerHTML = errBox(e.message, "loadTasks"); return; }
  renderTaskKpis(); renderTaskChips(); renderTasks();
}

function renderTaskKpis() {
  const open = TASKS.filter((t) => t.status === "OPEN");
  const stuck = open.filter((t) => t.escalated || t.age_hours >= 6);
  const doneToday = TASKS.filter((t) => t.status === "DONE"
    && t.completed_at && t.completed_at.slice(0, 10) === new Date().toISOString().slice(0, 10));
  $("task-kpis").innerHTML =
    kpi("Pending tasks", open.length, "", "", "📋", "orange") +
    kpi("Stuck", stuck.length, "6h+ old or escalated", "", "🚨", "red") +
    kpi("Done today", doneToday.length, "", "", "✅", "green");
}

function renderTaskChips() {
  const counts = {
    OPEN: TASKS.filter((t) => t.status === "OPEN").length,
    DONE: TASKS.filter((t) => t.status === "DONE").length,
    ALL: TASKS.length,
  };
  // Ginti label se chipki hui thi ("Pending1") — ab apne pill mein, saaf
  // padhne layak. Ye asli buttons hain, isliye keyboard/tab se bhi chalte
  // hain aur screen-reader ko pata hota hai kaunsa chuna hua hai.
  $("task-chips").innerHTML = [["OPEN", "Pending"], ["DONE", "Done"], ["ALL", "All"]]
    .map(([v, label]) => `<button type="button" class="chip ${taskFilter === v ? "on" : ""}"
      aria-pressed="${taskFilter === v}"
      onclick="taskFilter='${v}';renderTaskChips();renderTasks()">${label}<span class="cnt">${counts[v]}</span></button>`)
    .join("");
}

function renderTasks() {
  const rows = TASKS.filter((t) => taskFilter === "ALL" || t.status === taskFilter);
  if (!rows.length) {
    $("task-list").innerHTML = emptyBox(
      taskFilter === "OPEN" ? "No pending tasks 🎉" : "Nothing here.", "✅");
    return;
  }
  // Har kaam apna card — poora card khulta hai (click ya Enter), aur
  // buttons alag rehte hain taaki "Done" dabane par detail na khul jaye.
  $("task-list").innerHTML = `<div class="taskgrid">` + rows.map((t) => {
    const open = t.status === "OPEN";
    const late = open && (t.escalated || t.age_hours >= 6);
    return `
    <article class="taskcard ${open ? "" : "off"} ${late ? "late" : ""}"
      tabindex="0" role="button" aria-label="${esc(t.code)} details"
      onclick="taskDetail('${t.code}')"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();taskDetail('${t.code}')}">
      <div class="tc-top">
        <span class="badge">${t.code}</span>
        <span class="statuspill ${open ? "off" : "on"}">${
          t.status === "OPEN" ? `${t.age_hours}h pending` : t.status === "DONE" ? "Done" : "Cancelled"}</span>
      </div>
      <div class="tc-title">${t.urgent ? "🔴 " : ""}${esc(t.title)}</div>
      <div class="tc-meta">
        <span class="badge role">${t.staff ? esc(t.staff) : "unassigned"}</span>
        ${t.order_number ? `<span class="badge">${esc(t.order_number)}</span>` : ""}
        ${t.ping_count ? `<span class="badge">reminded ${t.ping_count}×</span>` : ""}
        ${t.escalated ? `<span class="badge warn">escalated to you</span>` : ""}
        ${t.awaiting_reply ? `<span class="badge warn">❓ sawaal — jawab baaki</span>` : ""}
      </div>
      ${t.awaiting_reply ? `<div class="tc-reply">❓ ${esc(t.staff || "they")}: ${esc(t.last_question)}</div>` : ""}
      ${t.reply ? `<div class="tc-reply">💬 ${esc(t.staff || "they")}: ${esc(t.reply)}</div>` : ""}
      ${open ? `
      <div class="tc-acts" onclick="event.stopPropagation()">
        <button class="btn sm ghost" onclick="pingTask('${t.code}', this)">Ask</button>
        <button class="btn sm" onclick="doneTask('${t.code}', this)">Done</button>
        <button class="btn sm ghost" onclick="cancelTask('${t.code}', this)">Cancel</button>
      </div>` : ""}
    </article>`;
  }).join("") + `</div>`;
}

/* Tareekh ka ek hi roop — pehle ye taskDetail ke andar band tha, isliye
   thread use nahi kar pata tha. */
const when = (s) => (s ? new Date(s).toLocaleString("en-IN", {
  day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
}) : "—");

/* Card par click -> poori kahani ek jagah: kise diya, kab, kitni baar
   yaad dilaya, usne kya kaha. Pehle ye sab kahin dikhta hi nahi tha. */
function taskDetail(code) {
  const t = TASKS.find((x) => x.code === code);
  if (!t) return;
  const open = t.status === "OPEN";
  const row = (k, v) => `<div class="dt-row"><span>${k}</span><b>${v}</b></div>`;
  openModal(`
    <h3>${t.urgent ? "🔴 " : ""}${esc(t.title)}</h3>
    <div class="tc-meta" style="margin:-4px 0 12px">
      <span class="badge">${t.code}</span>
      <span class="statuspill ${open ? "off" : "on"}">${
        t.status === "OPEN" ? `${t.age_hours}h pending` : t.status === "DONE" ? "Done" : "Cancelled"}</span>
      ${t.escalated ? `<span class="badge warn">escalated to you</span>` : ""}
    </div>
    <div class="dtl">
      ${row("Given to", t.staff ? esc(t.staff) : "unassigned")}
      ${t.order_number ? row("Order", esc(t.order_number)) : ""}
      ${row("Created", `${when(t.created_at)}${t.created_by ? ` · ${esc(t.created_by)}` : ""}`)}
      ${row("Last reminder", when(t.last_ping_at))}
      ${row("Times asked", t.ping_count || 0)}
      ${t.eta_text ? row("They said", esc(t.eta_text)) : ""}
      ${t.completed_at ? row("Closed", when(t.completed_at)) : ""}
    </div>
    ${t.reply ? `<div class="tc-reply" style="margin-top:12px">💬 ${esc(t.staff || "they")}: ${esc(t.reply)}</div>` : ""}
    <div id="tt-box"></div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Close</button>
      ${open ? `
        <button class="btn ghost" onclick="pingTask('${t.code}', this, true)">Ask again</button>
        <button class="btn ghost" onclick="cancelTask('${t.code}', this, true)">Cancel task</button>
        <button class="btn" onclick="doneTask('${t.code}', this, true)">Mark done</button>` : ""}
    </div>`);
  if (t.msg_count) loadTaskThread(t.code);
}

/* Staff ke sawaal aur unke jawab — wahi thread jo panel mein dikhta hai.
 *
 * Ye poora rasta backend mein pehle se bana pada tha (/tasks/{code}/messages
 * aur /reply), par dashboard mein use bulane wala kuch nahi tha. Yani staff
 * ka sawaal DB mein likha jata tha aur wahin mar jata tha: owner ko na
 * koi screen milti thi, na — WhatsApp API juda na ho to — koi khabar.
 *
 * Isliye thread task ke andar hai, alag page par nahi: sawaal hamesha kisi
 * ek kaam ke baare mein hota hai, aur uska jawab dene ke liye wahi kaam
 * saamne hona chahiye. */
async function loadTaskThread(code) {
  const box = document.getElementById("tt-box");
  if (!box) return;
  box.innerHTML = `<div class="muted" style="margin-top:12px">Loading…</div>`;
  let th;
  try { th = await api(`/admin/api/tasks/${code}/messages`); }
  catch (e) { box.innerHTML = `<div class="muted" style="margin-top:12px">${esc(e.message)}</div>`; return; }
  if (!document.getElementById("tt-box")) return;      // sheet band ho gayi
  box.innerHTML = `
    <div class="tt-thread">
      ${th.messages.map((m) => `
        <div class="tt-msg ${m.who === "staff" ? "them" : "us"}">
          ${esc(m.text)}
          <span class="tt-at">${esc(m.who === "staff" ? m.name : "Aapne")} · ${when(m.at)}</span>
        </div>`).join("")}
    </div>
    <textarea id="tt-text" rows="2" placeholder="Jawab likhein…"></textarea>
    <div class="btnrow" style="margin-top:8px">
      <button class="btn" onclick="sendTaskReply('${code}', this)">Send reply</button>
    </div>
    <div id="tt-wa"></div>`;
}

async function sendTaskReply(code, btn) {
  const el = document.getElementById("tt-text");
  const text = (el.value || "").trim();
  if (text.length < 2) { toast("Jawab likhein pehle", true); return; }
  await busy(btn, async () => {
    const r = await api(`/admin/api/tasks/${code}/reply`, { method: "POST", body: { text } });
    el.value = "";
    await loadTaskThread(code);
    loadTasks();
    if (r.whatsapp) { toast("Jawab bhej diya"); return; }
    // WhatsApp API se nahi gaya. Jawab panel mein to dikh hi raha hai, par
    // staff ko tab tak pata nahi chalega jab tak wo khud khole. Owner ke
    // apne phone ka WhatsApp hamesha hai — wahi rasta jo staff panel bill
    // share karne ke liye use karta hai: link par tap khud user ka gesture
    // hai, isliye popup blocker ise nahi rokta.
    toast("Panel mein chala gaya — WhatsApp se bhejna ho to neeche");
    const wa = document.getElementById("tt-wa");
    if (wa && r.staff_phone) {
      wa.innerHTML = `<div class="btnrow" style="margin-top:8px">
        <a class="btn" target="_blank" rel="noopener"
           href="${esc(waShareUrl(r.staff_phone, r.wa_text))}">📲 ${esc(r.staff_name || "Staff")} ko WhatsApp par bhejein</a>
      </div>`;
    }
  });
}


/* Task ke teen kaam: pooch lo, band karo, radd karo.
 *
 * Teenon ek hi raste se jate hain taaki teenon ek jaise BOLEN. Pehle button
 * dabate hi kuch nahi dikhta tha — network dheema ho to aadmi ko lagta tha
 * click laga hi nahi, aur wo dobara dabata tha. Ab: turant spinner, phir
 * toast, phir list taaza. Sheet se dabaya ho to sheet kaam POORA hone par
 * band hoti hai (pehle wo pehle band ho jati thi, isliye galti dikhti hi
 * nahi thi — jawab kahin kho jata tha). */
async function taskAction(code, path, btn, shut, okMsg) {
  const run = async () => {
    const r = await api(`/admin/api/tasks/${code}/${path}`, { method: "POST" });
    if (shut) closeModal();
    toast((r && r.detail) || okMsg);
    loadTasks();
  };
  if (!btn) { try { await run(); } catch (e) { toast(e.message, true); } return; }
  await busy(btn, run);           // busy() khud galti par toast kar deta hai
}

async function pingTask(code, btn, shut) {
  return taskAction(code, "ping", btn, shut, "Reminder sent");
}
async function doneTask(code, btn, shut) {
  return taskAction(code, "done", btn, shut, `${code} closed`);
}
function cancelTask(code, btn, shut) {
  confirmDialog(`Cancel ${code}? The staff member will get no more reminders.`, async () => {
    // confirm apni modal khud band karta hai; detail sheet bhi tabhi jaye
    await taskAction(code, "cancel", null, shut, `${code} cancelled`);
  });
}
async function pingAllTasks(btn) {
  await busy(btn, async () => {
    const r = await api("/admin/api/jobs/task-followups", { method: "POST" });
    toast(r.sent ? `Reminded ${r.sent} people` : "Nobody needed a reminder right now");
    loadTasks();
  });
}

function newTaskModal() {
  const opts = (STAFF || []).filter((s) => s.is_active)
    .map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
  openModal(`<h3>New task</h3>
    <div class="frm">
      <div class="setfield"><label for="nt-title">What needs doing</label>
        <input id="nt-title" placeholder="Deliver Sharma ji's order today itself" autofocus>
        <small>Write it as if you're talking to them — this exact message goes to their WhatsApp.</small>
        <small class="fielderr" id="nt-err"></small></div>
      <div class="split2">
        <div class="setfield"><label for="nt-staff">Assign to</label>
          <select id="nt-staff">${opts || '<option value="">no staff yet</option>'}</select></div>
        <div class="setfield"><label for="nt-order">Order (optional)</label>
          <input id="nt-order" placeholder="KK-20260805-01"></div>
      </div>
      <div class="setfield"><label for="nt-urgent">Urgent?</label>
        <select id="nt-urgent"><option value="false">Normal</option><option value="true">Urgent — follow up sooner</option></select></div>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="nt-go">Send and track</button>
    </div>`);
  $("nt-go").onclick = (e) => busy(e.target, async () => {
    const title = $("nt-title").value.trim();
    if (title.length < 2) { $("nt-err").textContent = "Please write the task."; return; }
    const r = await api("/admin/api/tasks", { method: "POST", body: {
      title, staff: $("nt-staff").value || null,
      order_number: $("nt-order").value.trim() || null,
      urgent: $("nt-urgent").value === "true",
    }});
    closeModal(); toast(`${r.code} sent`); loadTasks();
  });
}

/* ============================= expenses ============================= */
const EXP_CATS = ["Detergent", "Electricity", "Rent", "Salary", "Transport", "Maintenance", "Other"];
let EXPENSES = [];
let expSort = { key: "spent_on", dir: -1 }, expQuery = "";
async function loadExpenses() {
  $("exp-list").innerHTML = skeleton(4);
  $("exp-cat").innerHTML = '<option value="">Select category…</option>'
    + EXP_CATS.map((c) => `<option>${c}</option>`).join("");
  $("exp-date").value = new Date().toISOString().slice(0, 10);
  try {
    [EXPENSES, SUMMARY] = await Promise.all([api("/admin/api/expenses"), api("/admin/api/reports/summary")]);
  } catch (e) { $("exp-list").innerHTML = errBox(e.message, "loadExpenses"); return; }
  const t = SUMMARY.today || {}, m = SUMMARY.month || {}, lm = SUMMARY.last_month || {};
  const profit = Number(m.profit || 0);
  // "vs last month" — omit when there is no baseline to compare against
  const delta = (now, prev) => {
    const p = Number(prev || 0);
    if (!p) return "";
    const pct = Math.round(((Number(now || 0) - p) / Math.abs(p)) * 100);
    const cls = pct > 0 ? "down" : pct < 0 ? "up" : "";   // more spend = bad (red)
    return `<span class="sub ${cls}">${pct > 0 ? "▲" : pct < 0 ? "▼" : ""} ${Math.abs(pct)}% vs last month</span>`;
  };
  const profitDelta = (() => {
    const p = Number(lm.profit || 0);
    if (!p) return "revenue − expenses";
    const pct = Math.round(((profit - p) / Math.abs(p)) * 100);
    return `<span class="${pct >= 0 ? "up" : "down"}">${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs last month</span>`;
  })();
  $("exp-kpis").innerHTML =
    kpi("Expenses today", money(t.expenses || 0), fmtDate(new Date().toISOString()), "", "📅", "amber") +
    kpi("Expenses this month", money(m.expenses || 0), delta(m.expenses, lm.expenses), "", "🗓️", "pink") +
    kpi("Profit this month", money(profit), profitDelta, "go('reports')", profit < 0 ? "📉" : "💰", profit < 0 ? "red" : "green");
  // profit value takes the sign colour directly
  const pv = $("exp-kpis").querySelectorAll(".kpi")[2]?.querySelector(".val");
  if (pv) pv.style.color = profit < 0 ? "var(--danger)" : "var(--ok)";
  renderExpenses(); renderExpChart();
}
function expSortBy(key) {
  if (expSort.key === key) expSort.dir *= -1;
  else expSort = { key, dir: key === "spent_on" ? -1 : -1 };
  renderExpenses();
}
function expFilter(v) { expQuery = (v || "").toLowerCase(); renderExpenses(); }
function renderExpenses() {
  if (!EXPENSES.length) {
    $("exp-list").innerHTML = emptyBox("No expenses yet — add your first one above.", "💸");
    return;
  }
  const q = expQuery;
  const rows = EXPENSES
    .filter((e) => !q || e.category.toLowerCase().includes(q) || (e.description || "").toLowerCase().includes(q))
    .sort((a, b) => {
      const k = expSort.key;
      const av = k === "amount" ? Number(a.amount) : a.spent_on;
      const bv = k === "amount" ? Number(b.amount) : b.spent_on;
      return (av < bv ? -1 : av > bv ? 1 : 0) * expSort.dir;
    });
  const total = rows.reduce((s, e) => s + Number(e.amount || 0), 0);
  const arrow = (k) => expSort.key === k ? (expSort.dir < 0 ? " ▼" : " ▲") : "";
  if (!rows.length) {
    $("exp-list").innerHTML = `
      <div class="filters" style="padding:12px 12px 0"><input type="search" aria-label="Search expenses" value="${esc(expQuery)}" placeholder="Search category or description…" oninput="expFilter(this.value)"></div>
      ${emptyBox("No expenses match your search.", "🔍")}`;
    return;
  }
  $("exp-list").innerHTML = `
    <div class="filters" style="padding:12px 12px 0"><input type="search" aria-label="Search expenses" value="${esc(expQuery)}" placeholder="Search category or description…" oninput="expFilter(this.value)"></div>
    <table class="tbl zebra"><thead><tr>
      <th class="sortable" onclick="expSortBy('spent_on')">Date${arrow("spent_on")}</th>
      <th>Category</th>
      <th class="sortable num" onclick="expSortBy('amount')">Amount${arrow("amount")}</th>
      <th>Description</th><th></th></tr></thead>
    <tbody>${rows.map((e) => `
      <tr><td class="nowrap">${fmtDate(e.spent_on)}</td><td>${esc(e.category)}</td>
      <td class="money">${money(e.amount)}</td>
      <td class="muted" style="max-width:260px" title="${esc(e.description || "")}"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.description || "")}</div></td>
      <td><button class="btn sm ghost danger-ic" aria-label="Delete expense" title="Delete" onclick="delExpense('${e.id}')">🗑</button></td></tr>`).join("")}
    </tbody>
    <tfoot><tr class="totalrow"><td>Total</td><td class="muted">${rows.length} item${rows.length > 1 ? "s" : ""}</td>
      <td class="money">${money(total)}</td><td></td><td></td></tr></tfoot></table>
    <div class="rowcards">${rows.map((e) => `
      <div class="rowcard"><div class="r1"><b>${esc(e.category)}</b><span class="money">${money(e.amount)}</span></div>
      <div class="kv"><span>${fmtDate(e.spent_on)}</span><span>${esc(e.description || "")}</span></div>
      <div class="act"><button class="btn sm ghost danger-ic" onclick="delExpense('${e.id}')">🗑 Delete</button></div></div>`).join("")}
      <div class="rowcard" style="background:var(--n50)"><div class="r1"><b>Total (${rows.length})</b><span class="money">${money(total)}</span></div></div>
    </div>`;
}
async function saveExpense(btn) {
  $("exp-cat-err").textContent = ""; $("exp-amt-err").textContent = "";
  const cat = $("exp-cat").value;
  const amt = parseFloat($("exp-amt").value);
  if (!cat) { $("exp-cat-err").textContent = "Pick a category."; $("exp-cat").focus(); return; }
  if (!(amt > 0)) { $("exp-amt-err").textContent = "Enter an amount greater than 0."; $("exp-amt").focus(); return; }
  await busy(btn, async () => {
    await api("/admin/api/expenses", { method: "POST", body: { category: cat, amount: amt, spent_on: $("exp-date").value, description: $("exp-desc").value.trim() || null } });
    $("exp-cat").value = ""; $("exp-amt").value = ""; $("exp-desc").value = "";
    toast(`✓ ${money(amt)} expense saved`); loadExpenses();
  });
}
function delExpense(id) {
  const e = EXPENSES.find((x) => x.id === id);
  const label = e ? `${money(e.amount)} — ${e.category}` : "this expense";
  confirmDialog(`Delete ${label}? This cannot be undone.`, async () => {
    try { await api(`/admin/api/expenses/${id}`, { method: "DELETE" }); toast(T.deleted); loadExpenses(); }
    catch (e) { toast(e.message, true); }
  });
}
const PALETTE = ["#f97316", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0e7490", "#dc2626", "#78716c"];
/* money=true (Expenses/Reports): every legend value is a ₹ amount, and the
   donut shows the total in its middle. money=false keeps the old count look. */
function donutHtml(pairs, opts = {}) {
  const asMoney = opts.money !== false;
  const total = pairs.reduce((a, [, v]) => a + v, 0) || 1;
  let acc = 0;
  const stops = pairs.map(([, v], i) => {
    const from = (acc / total) * 360; acc += v;
    return `${PALETTE[i % PALETTE.length]} ${from}deg ${(acc / total) * 360}deg`;
  });
  const fmt = (v) => asMoney ? money(v) : (typeof v === "number" && v > 999 ? money(v) : v);
  const legend = pairs.map(([k, v], i) => {
    const pct = Math.round((v / total) * 100);
    return `<div><span class="sw" style="background:${PALETTE[i % PALETTE.length]}"></span>${esc(k)} — <b>${fmt(v)}</b>${asMoney ? ` <span class="muted">(${pct}%)</span>` : ""}</div>`;
  }).join("");
  const center = asMoney
    ? `<div class="donut-center"><span class="dc-lbl">Total</span><span class="dc-val">${money(total)}</span></div>`
    : "";
  return [`<div class="donut" style="background:conic-gradient(${stops.join(",")})">${center}</div>`, legend];
}
function renderExpChart() {
  const byCat = {};
  EXPENSES.forEach((e) => { byCat[e.category] = (byCat[e.category] || 0) + Number(e.amount); });
  const pairs = Object.entries(byCat).sort((a, b) => b[1] - a[1]);
  if (!pairs.length) { $("exp-chart").innerHTML = emptyBox("No spending to chart yet.", "📊"); return; }
  const [donut, legend] = donutHtml(pairs, { money: true });
  $("exp-chart").innerHTML = `<div class="split2" style="align-items:center">${donut}<div class="legend">${legend}</div></div>`;
}

/* ============================= reports ============================= */
async function loadReports() {
  $("rep-body").innerHTML = skeleton(5);
  try {
    const [sum, dash] = await Promise.all([api("/admin/api/reports/summary"), api("/admin/api/dashboard")]);
    SUMMARY = sum; DASH = dash;
    if (!CUSTOMERS_CACHE) await loadCustomers(true);
  } catch (e) { $("rep-body").innerHTML = errBox(e.message, "loadReports"); return; }
  const periods = [["Today", SUMMARY.today], ["This week", SUMMARY.week], ["This month", SUMMARY.month]];
  const maxV = Math.max(1, ...periods.flatMap(([, p]) => [Number(p.revenue || 0), Number(p.expenses || 0), Math.abs(Number(p.profit || 0))]));
  const bars = periods.map(([lbl, p]) => `
    <div class="bargrp">
      <div style="display:flex;gap:4px;align-items:flex-end;height:110px;width:100%;justify-content:center">
        <div class="bar rev" style="height:${(Number(p.revenue || 0) / maxV) * 100}%" title="Revenue ${money(p.revenue)}"></div>
        <div class="bar exp" style="height:${(Number(p.expenses || 0) / maxV) * 100}%" title="Expenses ${money(p.expenses)}"></div>
        <div class="bar pft" style="height:${(Math.max(0, Number(p.profit || 0)) / maxV) * 100}%" title="Profit ${money(p.profit)}"></div>
      </div>
      <div class="muted">${lbl}</div>
      <div style="font-size:11px" class="muted">R ${money(p.revenue)} · E ${money(p.expenses)} · P ${money(p.profit)}</div>
    </div>`).join("");
  const statusPairs = Object.entries(DASH.counts.by_status || {}).map(([k, v]) => [statusName(k) || k, v]);
  const [sd, sl] = statusPairs.length ? donutHtml(statusPairs, { money: false }) : ["", ""];
  const top = (CUSTOMERS_CACHE || []).slice().sort((a, b) => Number(b.business) - Number(a.business)).slice(0, 8);
  $("rep-body").innerHTML = `
    <div class="card"><b>Revenue vs expenses vs profit</b>
      <div class="muted" style="margin-bottom:6px">Revenue = payments received on orders created in the period</div>
      <div class="bars">${bars}</div>
      <div class="legend" style="flex-direction:row;gap:16px;margin-top:8px">
        <div><span class="sw" style="background:var(--brand)"></span>Revenue</div>
        <div><span class="sw" style="background:var(--n300)"></span>Expenses</div>
        <div><span class="sw" style="background:var(--ok)"></span>Profit</div></div></div>
    <div class="split2" style="margin-top:14px">
      <div class="card"><b>Active orders by status</b><div class="split2" style="align-items:center;margin-top:8px">${sd}<div class="legend">${sl}</div></div></div>
      <div class="card"><b>Top customers by business</b>
        ${top.map((c) => `<div class="sumrow"><span>${esc(displayName(c.name, c.phone))}</span><span class="money">${money(c.business)}</span></div>`).join("") || emptyBox(T.noData)}
      </div></div>
    <div class="btnrow" style="margin-top:12px;justify-content:flex-start">
      <button class="btn ghost" onclick="dlServer('/admin/api/export/orders.csv','orders.csv')">⬇ Orders CSV</button>
      <button class="btn ghost" onclick="dlServer('/admin/api/export/customers.csv','customers.csv')">⬇ Customers CSV</button>
    </div>`;
}
async function dlServer(path, name) {
  const r = await fetch(path, { headers: { "X-API-Key": KEY } });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob()); a.download = name; a.click();
}

/* ============================= campaigns ============================= */
async function loadCampaigns() {
  $("seg-cards").innerHTML = skeleton(2);
  $("camp-list").innerHTML = skeleton(3);
  try {
    const [segs, camps, coupons] = await Promise.all([
      api("/admin/api/segments"), api("/admin/api/campaigns"), api("/admin/api/coupons"),
    ]);
    renderSegments(segs); renderCampaigns(camps); renderCoupons(coupons);
  } catch (e) { $("camp-list").innerHTML = errBox(e.message, "loadCampaigns"); }
  tplLoad(); tplPreview();
}
function renderSegments(segs) {
  $("seg-cards").innerHTML = Object.entries(segs.counts)
    .map(([k, v]) => `<div class="card kpi seg-card" onclick="prefillCampaign('${k}')">
      <div class="lbl">${SEGMENT_LABEL[k] || k}</div><div class="val">${v}</div>
      <div class="sub">tap to create a campaign</div></div>`).join("");
  const sel = $("camp-seg");
  if (sel) sel.innerHTML = Object.keys(segs.counts).map((k) => `<option value="${k}">${SEGMENT_LABEL[k] || k}</option>`).join("");
}
function prefillCampaign(seg) {
  $("camp-seg").value = seg;
  $("camp-name").value = `${seg}-${new Date().toISOString().slice(0, 10)}`;
  $("camp-name").scrollIntoView({ behavior: "smooth", block: "center" });
}
function renderCampaigns(camps) {
  if (!camps.length) { $("camp-list").innerHTML = emptyBox("No campaigns yet. The agent also suggests one every Monday.", "📣"); return; }
  $("camp-list").innerHTML = camps.map((c) => {
    const s = c.stats || {};
    return `<div class="card" style="margin-bottom:10px">
      <div class="r1" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <b>${esc(c.name)}</b>
        <span><span class="tag">${SEGMENT_LABEL[c.segment] || c.segment}</span>
        <span class="pill ${c.status === "sent" ? "PAID" : c.status === "cancelled" ? "CANCELLED" : "PARTIAL"}">${c.status}</span></span>
      </div>
      ${c.rationale ? `<p class="muted" style="margin:6px 0">${esc(c.rationale)}</p>` : ""}
      <p style="background:var(--n50);border-radius:8px;padding:8px 10px;font-size:13px;margin:6px 0;white-space:pre-wrap">${esc(c.message_text)}</p>
      <div class="muted">Sent ${s.sent || 0} · Delivered ${s.delivered || 0} · Read ${s.read || 0} · Replied ${s.replied || 0} · Failed ${s.failed || 0} · Skipped ${s.skipped || 0}
      ${s.orders_attributed ? ` · <b style="color:var(--ok)">Orders ${s.orders_attributed} (${money(s.revenue_attributed)})</b>` : ""}</div>
      <div class="act" style="margin-top:8px">
        ${["draft", "suggested"].includes(c.status) ? `<button class="btn sm ok" onclick="approveCampaign('${c.id}')">Approve & send</button>
        <button class="btn sm ghost" onclick="cancelCampaign('${c.id}')">Skip</button>` : ""}
      </div></div>`;
  }).join("");
}
async function createCampaign(btn) {
  await busy(btn, async () => {
    await api("/admin/api/campaigns", { method: "POST", body: {
      name: $("camp-name").value.trim(), segment: $("camp-seg").value,
      message_text: $("camp-msg").value.trim(), coupon_code: $("camp-coupon").value.trim() || null,
    }});
    toast("Campaign saved as draft"); $("camp-msg").value = ""; loadCampaigns();
  });
}
function approveCampaign(id) {
  confirmDialog("Send this campaign now? Only opted-in customers are messaged; quiet hours are respected.", async () => {
    try { const r = await api(`/admin/api/campaigns/${id}/approve`, { method: "POST" }); toast(`Sending to ${r.queued} customers`); loadCampaigns(); }
    catch (e) { toast(e.message, true); }
  });
}
async function cancelCampaign(id) {
  try { await api(`/admin/api/campaigns/${id}/cancel`, { method: "POST" }); toast("Campaign skipped"); loadCampaigns(); }
  catch (e) { toast(e.message, true); }
}
function renderCoupons(coupons) {
  $("coupon-list").innerHTML = coupons.length ? coupons.map((c) => `
    <div class="sumrow"><span><b>${c.code}</b> — ${c.discount_type === "percent" ? c.value + "%" : money(c.value)} off
      ${c.min_order ? `(min ${money(c.min_order)})` : ""} <span class="tag">${c.used} used</span></span>
      <button class="btn sm ghost" onclick="toggleCoupon('${c.code}')">${c.active ? "Disable" : "Enable"}</button></div>`).join("")
    : `<p class="muted">No coupons yet.</p>`;
}
async function createCoupon(btn) {
  await busy(btn, async () => {
    await api("/admin/api/coupons", { method: "POST", body: {
      code: $("cp-code").value.trim(), discount_type: $("cp-type").value,
      value: parseFloat($("cp-val").value), min_order: parseFloat($("cp-min").value) || null,
      valid_to: $("cp-until").value || null,
    }});
    toast("Coupon created"); $("cp-code").value = ""; loadCampaigns();
  });
}
async function toggleCoupon(code) {
  try { await api(`/admin/api/coupons/${code}/toggle`, { method: "POST" }); loadCampaigns(); }
  catch (e) { toast(e.message, true); }
}

/* ============================= template studio ============================= */
let TPL_BTNS = [];
function tplAddBtn() {
  if (TPL_BTNS.length >= 3) { toast("Meta allows at most 3 buttons", true); return; }
  TPL_BTNS.push({ type: "QUICK_REPLY", text: "", url: "", phone_number: "" });
  tplRenderBtns();
}
function tplRenderBtns() {
  $("tpl-btns").innerHTML = TPL_BTNS.map((b, i) => `
    <div class="filters" style="margin-bottom:6px">
      <select style="min-width:120px" onchange="TPL_BTNS[${i}].type=this.value;tplRenderBtns()">
        <option value="QUICK_REPLY" ${b.type === "QUICK_REPLY" ? "selected" : ""}>Quick reply</option>
        <option value="URL" ${b.type === "URL" ? "selected" : ""}>Open link</option>
        <option value="PHONE_NUMBER" ${b.type === "PHONE_NUMBER" ? "selected" : ""}>Call</option>
      </select>
      <input placeholder="Button text (max 25)" maxlength="25" value="${esc(b.text)}" oninput="TPL_BTNS[${i}].text=this.value;tplPreview()" style="min-width:130px">
      ${b.type === "URL" ? `<input placeholder="https://wa.me/…" value="${esc(b.url)}" oninput="TPL_BTNS[${i}].url=this.value" style="min-width:150px">` : ""}
      ${b.type === "PHONE_NUMBER" ? `<input placeholder="+919696856069" value="${esc(b.phone_number)}" oninput="TPL_BTNS[${i}].phone_number=this.value" style="min-width:130px">` : ""}
      <button class="btn sm danger" onclick="TPL_BTNS.splice(${i},1);tplRenderBtns();tplPreview()">✕</button>
    </div>`).join("");
  tplPreview();
}
function tplPreview() {
  const body = $("tpl-body").value || "…";
  $("tpl-count").textContent = `${body.length}/1024`;
  const samples = $("tpl-samples").value.split(",").map((s) => s.trim());
  let rendered = body;
  (body.match(/\{\{(\d+)\}\}/g) || []).forEach((m) => {
    const n = parseInt(m.replace(/\D/g, ""));
    rendered = rendered.replace(m, samples[n - 1] || `[${n}]`);
  });
  $("tplp-body").textContent = rendered;
  $("tplp-footer").textContent = $("tpl-footer").value;
  $("tplp-btns").innerHTML = TPL_BTNS.filter((b) => b.text).map((b) =>
    `<div style="border-top:1px solid #eee;margin-top:8px;padding-top:8px;text-align:center;color:#00a5f4;font-weight:600">${b.type === "PHONE_NUMBER" ? "📞 " : b.type === "URL" ? "🔗 " : ""}${esc(b.text)}</div>`).join("");
}
async function tplSubmit(btn) {
  await busy(btn, async () => {
    const r = await api("/admin/api/templates", { method: "POST", body: {
      name: $("tpl-name").value.trim(), category: $("tpl-cat").value,
      body: $("tpl-body").value, footer: $("tpl-footer").value.trim() || null,
      buttons: TPL_BTNS.filter((b) => b.text),
      samples: $("tpl-samples").value.split(",").map((s) => s.trim()).filter(Boolean),
    }});
    toast(`Submitted — '${r.name}' is ${r.status} at Meta`);
    $("tpl-body").value = ""; TPL_BTNS = []; tplRenderBtns(); tplLoad();
  });
}
async function tplLoad() {
  try {
    const rows = await api("/admin/api/templates");
    $("tpl-list").innerHTML = rows.length ? rows.map((t) => `
      <div class="sumrow" style="align-items:flex-start">
        <span style="flex:1"><b>${esc(t.name)}</b> <span class="tag">${t.category}</span>
          <span class="pill ${t.status === "APPROVED" ? "PAID" : t.status === "REJECTED" ? "UNPAID" : "PARTIAL"}">${t.status}</span>
          ${t.rejected_reason ? `<div class="muted" style="color:var(--danger)">Reason: ${esc(t.rejected_reason)}</div>` : ""}
          <div class="muted" style="font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px">${esc(t.body)}</div></span>
        <button class="btn sm danger" onclick="tplDelete('${esc(t.name)}')">✕</button>
      </div>`).join("") : `<p class="muted">No templates yet.</p>`;
  } catch (e) { $("tpl-list").innerHTML = errBox(e.message, "tplLoad"); }
}
function tplDelete(name) {
  confirmDialog(`Delete template "${name}" from Meta? This cannot be undone.`, async () => {
    try { await api(`/admin/api/templates/${encodeURIComponent(name)}`, { method: "DELETE" }); toast(T.deleted); tplLoad(); }
    catch (e) { toast(e.message, true); }
  });
}

/* ============================= agents control room ============================= */
async function loadAgents() {
  $("agents-body").innerHTML = skeleton(5);
  let d;
  try { d = await api("/admin/api/agents/overview"); }
  catch (e) { $("agents-body").innerHTML = errBox(e.message, "loadAgents"); return; }
  const sv = d.service, mk = d.marketing;
  const segTop = Object.entries(mk.segments).filter(([, v]) => v > 0)
    .map(([k, v]) => `${SEGMENT_LABEL[k] || k}: <b>${v}</b>`).join(" · ") || "no segments yet";
  const budgetPct = Math.min(100, Math.round((mk.month.messages_used / Math.max(1, mk.month.budget)) * 100));
  $("agents-body").innerHTML = `
    <div class="split2">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <b style="font-size:16px">🛎️ Service agent</b>
          <label style="display:flex;align-items:center;gap:8px;margin:0;font-size:13px">
            <input type="checkbox" ${sv.enabled ? "checked" : ""} style="width:auto"
              onchange="quickSet('agent_enabled', this.checked, 'Service agent ' + (this.checked ? 'on' : 'off'))"> Active
          </label>
        </div>
        <div class="grid kpis" style="grid-template-columns:repeat(2,1fr);margin:12px 0">
          ${kpi("Replies today", sv.today.replies, "", "go('activity')")}
          ${kpi("Owner commands", sv.today.commands, "bills, status, relay…", "go('activity')")}
          ${kpi("Escalations", sv.today.escalations, "needed you", "go('activity')")}
          ${kpi("FYIs sent", sv.today.fyis, "handled + informed", "go('activity')")}
        </div>
        <div class="sumrow"><span>📚 Knowledge</span><span><b>${sv.knowledge.faqs}</b> FAQs · <b>${sv.knowledge.corrections}</b> corrections · <b>${sv.knowledge.docs}</b> documents</span></div>
        <div class="sumrow"><span>🙋 Teach-me queue</span><span style="color:${sv.teachme_open ? "var(--danger)" : "var(--ok)"}"><b>${sv.teachme_open}</b> pending</span></div>
        <div class="act" style="margin-top:10px">
          <button class="btn sm" onclick="go('training')">🎓 Train</button>
          <button class="btn sm ghost" onclick="go('activity')">🛰 Activity</button>
        </div>
        <p class="muted" style="margin-top:8px">Train from WhatsApp: <b>test customer</b> → ask a question → <b>sikhao: the right answer</b> → <b>test band</b></p>
      </div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <b style="font-size:16px">📣 Marketing agent</b>
          <select style="width:auto" onchange="quickSet('marketing_autonomy', this.value, 'Marketing: ' + this.value)">
            <option value="auto" ${mk.autonomy === "auto" ? "selected" : ""}>Full auto</option>
            <option value="suggest" ${mk.autonomy === "suggest" ? "selected" : ""}>Suggest only</option>
            <option value="off" ${mk.autonomy === "off" ? "selected" : ""}>Off</option>
          </select>
        </div>
        <div class="grid kpis" style="grid-template-columns:repeat(2,1fr);margin:12px 0">
          ${kpi("Campaigns this month", mk.month.campaigns_sent, "", "go('campaigns')")}
          ${kpi("Orders from campaigns", mk.month.orders_attributed, money(mk.month.revenue_attributed) + " revenue", "go('campaigns')")}
        </div>
        <div class="sumrow"><span>🎯 Segments</span><span style="text-align:right">${segTop}</span></div>
        <div class="sumrow"><span>💬 Message budget</span><span><b>${mk.month.messages_used}</b> / ${mk.month.budget} (${budgetPct}%)</span></div>
        <div class="sumrow"><span>📸 Daily social</span><span>${mk.social.enabled ? `On, ${mk.social.hour}:00 IST` : "Off"} · Instagram ${mk.social.instagram_linked ? "✅ linked" : "❌ not linked"}</span></div>
        <div class="act" style="margin-top:10px">
          <button class="btn sm" onclick="go('campaigns')">📣 Campaigns</button>
          <button class="btn sm ghost" onclick="go('settings')">⚙️ Limits</button>
        </div>
        <p class="muted" style="margin-top:8px">From WhatsApp: <b>test marketing</b> (preview) · <b>social bhejo</b> (today's poster) · <b>campaign nahi</b> (stop)</p>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <b>🩺 System health</b>
      <div class="filters" style="margin-top:8px">
        <span class="tag">LLM: ${esc(d.health.llm_provider)}</span>
        <span class="tag">Public URL: ${d.health.public_url_set ? "✅ set" : "❌ missing"}</span>
        <span class="tag">Standup: ${d.health.standup_hour}:00 IST</span>
        <span class="tag">Turnaround: ${d.health.turnaround_days} days</span>
      </div>
    </div>`;
}
async function quickSet(key, value, msg) {
  try { await api("/admin/api/settings", { method: "PUT", body: { key, value } }); toast(msg); }
  catch (e) { toast(e.message, true); }
}

/* ============================= training ============================= */
async function loadTraining() {
  $("faq-list").innerHTML = skeleton(3);
  try {
    const [settings, faqs, corr, teach, docs] = await Promise.all([
      api("/admin/api/settings"), api("/admin/api/training/faq"),
      api("/admin/api/training/corrections"), api("/admin/api/training/teachme"),
      api("/admin/api/training/docs"),
    ]);
    $("agent-toggle").checked = !!settings.agent_enabled;
    $("tr-cust-inst").value = settings.customer_instructions || "";
    $("tr-staff-inst").value = settings.staff_instructions || "";
    $("tr-mkt-inst").value = settings.marketing_instructions || "";
    renderFaqs(faqs); renderCorrections(corr); renderTeachme(teach); renderDocs(docs);
  } catch (e) { $("faq-list").innerHTML = errBox(e.message, "loadTraining"); }
}
let DOCS_CACHE = [];
function renderDocs(docs) {
  DOCS_CACHE = docs;
  $("doc-list").innerHTML = docs.length ? docs.map((d, i) => `
    <div class="sumrow"><span>📄 <b>${esc(d.document)}</b> <span class="tag">${d.chunks} parts</span>
      <span class="muted">${fmtWhen(d.uploaded_at)}</span></span>
      <button class="btn sm danger" aria-label="Delete document" onclick="delDocAt(${i})">✕</button></div>`).join("")
    : `<p class="muted">No documents yet.</p>`;
}
async function uploadDoc(input) {
  if (!input.files || !input.files[0]) return;
  const fd = new FormData();
  fd.append("file", input.files[0]);
  $("doc-status").textContent = "Reading " + input.files[0].name + "…";
  try {
    const r = await api("/admin/api/training/upload", { method: "POST", body: fd });
    toast(`${r.document} learned — ${r.chunks} parts, live now`);
    $("doc-status").textContent = "";
    loadTraining();
  } catch (e) { toast(e.message, true); $("doc-status").textContent = ""; }
  input.value = "";
}
function delDocAt(i) {
  const d = DOCS_CACHE[i];
  if (d) delDoc(d.document);
}
function delDoc(name) {
  confirmDialog(`Remove "${name}" from the agent's knowledge?`, async () => {
    try { await api(`/admin/api/training/docs/${encodeURIComponent(name)}`, { method: "DELETE" }); toast(T.deleted); loadTraining(); }
    catch (e) { toast(e.message, true); }
  });
}
/* The FAQ store can hold 1000+ rows (mostly duplicates). Rendering them all
   into #sec-training left thousands of DOM nodes alive app-wide — every
   layout/paint (composer, template modal) crawled. Fix: de-duplicate, then
   render a page at a time. */
let FAQ_ALL = [], FAQ_SHOWN = 0;
const FAQ_PAGE = 30;
function renderFaqs(faqs) {
  const seen = new Set();
  FAQ_ALL = (faqs || []).filter((f) => {
    const k = `${(f.question || "").trim().toLowerCase()}|${(f.answer || "").trim().toLowerCase()}|${f.audience}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  FAQ_SHOWN = FAQ_PAGE;
  paintFaqs();
}
function faqMore() { FAQ_SHOWN += 50; paintFaqs(); }
function paintFaqs() {
  if (!FAQ_ALL.length) {
    $("faq-list").innerHTML = `<p class="muted">No FAQ entries yet — add shop timings, prices policy, delivery areas…</p>`;
    return;
  }
  const rows = FAQ_ALL.slice(0, FAQ_SHOWN);
  const dupNote = "";
  $("faq-list").innerHTML = rows.map((f) => `
    <div class="sumrow" style="align-items:flex-start;gap:8px">
      <span style="flex:1"><b>Q:</b> ${esc(f.question)}<br><b>A:</b> ${esc(f.answer)} <span class="tag">${f.audience}</span></span>
      <button class="btn sm ghost danger-ic" aria-label="Delete FAQ" title="Delete" onclick="delFaq('${f.id}')">🗑</button></div>`).join("")
    + (FAQ_ALL.length > rows.length
        ? `<div style="text-align:center;padding:10px"><button class="btn ghost sm" onclick="faqMore()">Show more (${rows.length}/${FAQ_ALL.length})</button></div>`
        : `<div class="muted" style="text-align:center;padding:8px;font-size:11.5px">${FAQ_ALL.length} unique ${FAQ_ALL.length === 1 ? "entry" : "entries"}</div>`);
}
async function addFaq(btn) {
  await busy(btn, async () => {
    await api("/admin/api/training/faq", { method: "POST", body: { question: $("faq-q").value.trim(), answer: $("faq-a").value.trim(), audience: $("faq-aud").value } });
    $("faq-q").value = ""; $("faq-a").value = "";
    toast("Saved — the agent uses this immediately"); loadTraining();
  });
}
function delFaq(id) {
  confirmDialog(T.confirmDelete, async () => {
    await api(`/admin/api/training/faq/${id}`, { method: "DELETE" }); toast(T.deleted); loadTraining();
  });
}
function renderCorrections(corr) {
  $("corr-list").innerHTML = corr.length ? corr.map((c) => `
    <div class="sumrow" style="align-items:flex-start;gap:8px">
      <span style="flex:1"><b>When asked:</b> ${esc(c.question)}<br><b>Reply like:</b> ${esc(c.correct_reply)}</span>
      <button class="btn sm danger" onclick="delCorr('${c.id}')">✕</button></div>`).join("")
    : `<p class="muted">No corrections yet.</p>`;
}
async function addCorr(btn) {
  await busy(btn, async () => {
    await api("/admin/api/training/corrections", { method: "POST", body: { question: $("corr-q").value.trim(), correct_reply: $("corr-a").value.trim() } });
    $("corr-q").value = ""; $("corr-a").value = "";
    toast("Saved — used from the next reply"); loadTraining();
  });
}
function delCorr(id) {
  confirmDialog(T.confirmDelete, async () => {
    await api(`/admin/api/training/corrections/${id}`, { method: "DELETE" }); toast(T.deleted); loadTraining();
  });
}
function renderTeachme(teach) {
  $("teach-count").textContent = teach.length;
  $("teach-list").innerHTML = teach.length ? teach.map((q) => `
    <div class="card" style="margin-bottom:8px">
      <div class="muted">${esc(q.customer)} · ${fmtWhen(q.asked_at)}</div>
      <p style="margin:5px 0"><b>${esc(q.question)}</b></p>
      <div class="frm"><textarea id="ta-${q.id}" rows="2" placeholder="Write the answer once — it becomes permanent knowledge"></textarea></div>
      <div class="act" style="margin-top:6px">
        <button class="btn sm" onclick="answerTeach('${q.id}', true)">Answer + send to customer</button>
        <button class="btn sm ghost" onclick="answerTeach('${q.id}', false)">Save answer only</button>
      </div></div>`).join("")
    : `<p class="muted">Queue is empty — the agent knew everything it was asked. 🎉</p>`;
}
async function answerTeach(id, send) {
  const ans = $("ta-" + id).value.trim();
  if (!ans) { toast("Write an answer first", true); return; }
  try {
    const r = await api(`/admin/api/training/teachme/${id}/answer`, { method: "POST", body: { answer: ans, save_as_faq: true, send_to_customer: send } });
    toast(r.sent_to_customer ? "Answered + sent to the customer" : "Answered — saved to knowledge");
    loadTraining();
  } catch (e) { toast(e.message, true); }
}
async function saveAgentSettings(btn) {
  await busy(btn, async () => {
    await api("/admin/api/settings", { method: "PUT", body: { key: "agent_enabled", value: $("agent-toggle").checked } });
    await api("/admin/api/settings", { method: "PUT", body: { key: "customer_instructions", value: $("tr-cust-inst").value } });
    await api("/admin/api/settings", { method: "PUT", body: { key: "staff_instructions", value: $("tr-staff-inst").value } });
    await api("/admin/api/settings", { method: "PUT", body: { key: "marketing_instructions", value: $("tr-mkt-inst").value } });
    toast("Saved — live immediately, no restart");
  });
}

/* ============================= activity =============================
   One event = one block. The log used to print raw JSON next to a raw
   action name ({"code":"T-3","staff":"Taskram"}), which is a developer's
   view of the shop. Same rows, told as sentences. */
const ACT_META = {
  new_bill: ["🧾", "Bill drafted"],
  create_bill: ["🧾", "Bill created"],
  order_edited: ["✏️", "Bill edited"],
  order_deleted: ["🗑", "Bill deleted"],
  customer_edited: ["✏️", "Customer edited"],
  customer_deleted: ["🗑", "Customer deleted"],
  relay: ["📨", "Message relayed"],
  task_created: ["📋", "Task assigned"],
  task_completed: ["✅", "Task completed"],
  pickup_task_created: ["🛺", "Pickup scheduled"],
  status_update: ["🔄", "Status changed"],
  delay_update: ["⏳", "Delivery pushed back"],
  assign_staff: ["👷", "Assigned to staff"],
  done_command: ["✅", "Staff said done"],
  standup: ["📣", "Morning standup"],
  payment_reminders: ["💰", "Payment reminder"],
  delivery_nudges: ["🔔", "Delivery nudge"],
  ai_reply: ["🤖", "Replied to customer"],
  escalated: ["🔔", "Escalated to you"],
  complaint_escalated: ["😞", "Complaint received"],
  admin_fyi: ["ℹ️", "FYI to you"],
  rating: ["⭐", "Rating received"],
  lead_created: ["🌱", "New inquiry"],
  hot_lead_digest: ["🌱", "Lead digest"],
  campaign_sent: ["📢", "Campaign sent"],
  campaign_approved: ["👍", "Campaign approved"],
  daily_social: ["📸", "Daily post"],
  taught_via_whatsapp: ["🎓", "Taught via WhatsApp"],
  training_doc_uploaded: ["📄", "Training file uploaded"],
  message_format_edited: ["💬", "Message format edited"],
  message_format_reset: ["↩️", "Message format reset"],
  template_submitted: ["📑", "Template sent to Meta"],
  agent_paused: ["⏸", "Agent paused"],
  agent_resumed: ["▶️", "Agent resumed"],
  tunnel_heal: ["🔧", "Tunnel repaired"],
};
// args worth showing as chips, in the order they read best
const ACT_CHIPS = ["order", "order_number", "code", "staff", "staff_name", "relay_to",
  "customer", "customer_name", "new_status", "new_date", "total", "rating",
  "campaign", "theme", "key", "document", "name", "date", "open", "orders", "items"];

function actMeta(action) {
  return ACT_META[action] || ["🔹", action.replace(/_/g, " ")];
}
function actChips(args) {
  if (!args || typeof args !== "object") return "";
  const out = [];
  for (const k of ACT_CHIPS) {
    const v = args[k];
    if (v === undefined || v === null || v === "" || typeof v === "object") continue;
    out.push(`<span class="actchip"><i>${esc(k.replace(/_/g, " "))}</i>${esc(String(v)).slice(0, 40)}</span>`);
  }
  if (args.urgent === true) out.push('<span class="actchip urgent">urgent</span>');
  return out.length ? `<div class="actchips">${out.join("")}</div>` : "";
}

/* First screen = 60 events (was the server default 100 in one shot);
   "Aur dikhao" grows the window via the API's ?limit= up to its 500 cap. */
let ACT_LIMIT = 60;
const ACT_MAX = 500;
function actMore(btn) { btn.disabled = true; ACT_LIMIT = Math.min(ACT_LIMIT + 100, ACT_MAX); loadActivity(false); }
async function loadActivity(reset = true) {
  if (reset) { ACT_LIMIT = 60; $("act-list").innerHTML = skeleton(6); }
  try {
    const role = $("act-role").value;
    const p = new URLSearchParams({ limit: ACT_LIMIT });
    if (role) p.set("role", role);
    const rows = await api("/admin/api/activity?" + p);
    if (!rows.length) { $("act-list").innerHTML = emptyBox("No agent activity yet.", "🤖"); return; }
    const moreBtn = rows.length >= ACT_LIMIT && ACT_LIMIT < ACT_MAX
      ? `<div style="padding:12px;text-align:center"><button class="btn ghost sm" onclick="actMore(this)">⬇ Show more (${rows.length} shown)</button></div>`
      : "";
    let lastDay = "";
    $("act-list").innerHTML = rows.map((r) => {
      const [icon, label] = actMeta(r.action);
      const a = r.args || {};
      // the customer's own words matter more than any label we invent
      const quote = a.text || a.question || a.relay_message || "";
      let head = "";
      const day = new Date(r.at).toDateString();
      if (day !== lastDay) {
        lastDay = day;
        head = `<div class="actday">${dayName(r.at) || fmtDate(r.at)}</div>`;
      }
      return `${head}
        <div class="actrow${r.ok === false ? " bad" : ""}">
          <div class="actico">${icon}</div>
          <div class="actbody">
            <div class="acthead">
              <span class="actwho"><b>${esc(label)}</b>
                <span class="tag">${esc(r.role || "")}</span>
                ${r.actor ? `<span class="muted">${esc(r.actor)}</span>` : ""}</span>
              <span class="acttime">${fmtClock(r.at)}</span></div>
            ${r.result ? `<div class="actres">${esc(String(r.result)).slice(0, 400)}</div>` : ""}
            ${quote ? `<div class="actquote">${esc(String(quote)).slice(0, 220)}</div>` : ""}
            ${actChips(a)}
          </div>
        </div>`;
    }).join("") + moreBtn;
  } catch (e) { $("act-list").innerHTML = errBox(e.message, "loadActivity"); }
}

/* ============================= settings ============================= */
let STAFF = [], SETTINGS_CACHE = {}, RM_RATES = [], RM_EXTRA_G = [], RM_EXTRA_S = [];

function stTab(t) {
  ["profile", "pricing", "messages", "ops"].forEach((x) => {
    const el = $("st-" + x); if (el) el.style.display = x === t ? "" : "none";
  });
  document.querySelectorAll("[data-st]").forEach((el) => el.classList.toggle("on", el.dataset.st === t));
}

async function loadSettings() {
  mfLoad();
  try {
    const [rates, staff, s] = await Promise.all([
      api("/admin/api/rates"), api("/admin/api/staff"), api("/admin/api/settings"),
    ]);
    STAFF = staff; SETTINGS_CACHE = s; RM_RATES = rates;
    renderRateMatrix(); renderStaff(); renderPresets();
    $("set-standup").value = s.standup_hour;
    $("set-turnaround").value = s.turnaround_days;
    $("set-freqcap").value = s.marketing_freq_cap_per_month;
    $("set-budget").value = s.marketing_monthly_msg_budget;
    $("set-autonomy").value = s.marketing_autonomy;
    $("set-social").value = String(!!s.social_daily_enabled);
    $("set-socialhour").value = s.social_post_hour;
    $("set-igid").value = s.ig_user_id || "";
    // the API returns a mask for a saved token — the real one never reaches
    // the browser; saving the mask back is a no-op server-side
    $("set-igtoken").value = s.ig_access_token || "";
    $("set-igtoken").placeholder = s.ig_access_token ? "" : "Paste token";
    igState();
    const staffOpts = (sel) => '<option value="">— none —</option>' +
      staff.filter((x) => x.is_active).map((x) => `<option value="${x.phone}" ${sel === x.phone ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    $("set-washer").innerHTML = staffOpts(s.default_washer_phone);
    $("set-delivery").innerHTML = staffOpts(s.default_delivery_phone);
    // business profile
    $("bp-name").value = "Kwik Klin";
    $("bp-gstin").value = s.shop_gstin || "";
    $("bp-phone").value = s.shop_contact_phone || "";
    $("bp-hours").value = s.shop_hours || "";
    $("bp-address").value = s.shop_address || "";
    $("bp-upi").value = s.upi_vpa || "";
    $("bp-payee").value = s.upi_payee || "";
    $("bp-footer").value = s.invoice_footer || "";
    $("bp-gstpct").value = s.gst_percent;
    $("bp-gstdef").checked = !!s.gst_default_on;
  } catch (e) { toast(e.message, true); }
}

async function saveProfile(btn) {
  await busy(btn, async () => {
    const pairs = [
      ["shop_gstin", $("bp-gstin").value.trim()],
      ["shop_contact_phone", $("bp-phone").value.trim()],
      ["shop_hours", $("bp-hours").value.trim()],
      ["shop_address", $("bp-address").value.trim()],
      ["upi_vpa", $("bp-upi").value.trim()],
      ["upi_payee", $("bp-payee").value.trim()],
      ["invoice_footer", $("bp-footer").value.trim()],
      ["gst_percent", parseFloat($("bp-gstpct").value) || 18],
      ["gst_default_on", $("bp-gstdef").checked],
      ["default_delivery_phone", $("set-delivery").value],
    ];
    for (const [key, value] of pairs) await api("/admin/api/settings", { method: "PUT", body: { key, value } });
    toast("Profile saved — receipts & bills use it now");
    loadSettings();
  });
}

/* discount presets */
function renderPresets() {
  const list = SETTINGS_CACHE.discount_presets || [];
  $("dp-list").innerHTML = list.length ? list.map((p, i) => `
    <div class="sumrow"><span><b>${esc(p.name)}</b> — ${p.type === "percent" ? p.value + "%" : money(p.value)} off</span>
      <button class="btn sm danger" onclick="delPreset(${i})">✕</button></div>`).join("")
    : `<p class="muted">No offers yet — they appear in New Bill's discount dropdown.</p>`;
}
async function addPreset(btn) {
  await busy(btn, async () => {
    const name = $("dp-name").value.trim(), val = parseFloat($("dp-val").value);
    if (!name || !(val > 0)) throw new Error("Offer name and value required");
    const list = [...(SETTINGS_CACHE.discount_presets || []), { name, type: $("dp-type").value, value: val }];
    await api("/admin/api/settings", { method: "PUT", body: { key: "discount_presets", value: list } });
    $("dp-name").value = ""; $("dp-val").value = "";
    toast("Offer added"); loadSettings();
  });
}
async function delPreset(i) {
  const list = [...(SETTINGS_CACHE.discount_presets || [])];
  list.splice(i, 1);
  await api("/admin/api/settings", { method: "PUT", body: { key: "discount_presets", value: list } });
  loadSettings();
}

/* pricing matrix (legacy-style: garment rows x service columns) */
function renderRateMatrix() {
  const pc = RM_RATES.filter((r) => r.unit === "pc");
  const kg = RM_RATES.filter((r) => r.unit === "kg");
  const services = [...new Set([...pc.map((r) => r.service), ...RM_EXTRA_S])];
  const garments = [...new Set([...pc.map((r) => r.garment), ...RM_EXTRA_G])].filter(Boolean).sort();
  const cell = (g, s) => {
    const r = pc.find((x) => x.garment === g && x.service === s);
    return `<td><input type="number" step="0.5" style="max-width:86px" value="${r ? r.rate : ""}"
      placeholder="—" onchange="rmCell('${esc(g)}','${esc(s)}',this.value,'${r ? r.id : ""}')"></td>`;
  };
  $("rate-matrix").innerHTML = `
    <table class="tbl keep" style="min-width:${180 + services.length * 110}px"><thead><tr>
      <th>Laundry garment name</th>${services.map((s) => `<th>${esc(s)} (₹)</th>`).join("")}
    </tr></thead><tbody>
      ${garments.map((g) => `<tr><td><b style="font-size:13px;text-transform:none;letter-spacing:0">${esc(g)}</b></td>${services.map((s) => cell(g, s)).join("")}</tr>`).join("")}
    </tbody></table>`;
  $("rate-kg").innerHTML = kg.map((r) => `
    <div class="sumrow"><span>${esc(r.service)}</span>
      <span style="max-width:110px"><input type="number" step="0.5" value="${r.rate}" onchange="updRate('${r.id}', this.value, null)"></span></div>`).join("")
    || `<p class="muted">No per-kg services yet.</p>`;
}
async function rmCell(garment, service, value, id) {
  const v = parseFloat(value);
  try {
    if (id) {
      if (!(v > 0)) { await api(`/admin/api/rates/${id}`, { method: "PUT", body: { is_active: false } }); toast("Rate disabled"); }
      else { await api(`/admin/api/rates/${id}`, { method: "PUT", body: { rate: v, is_active: true } }); toast("Rate saved — new bills only"); }
    } else if (v > 0) {
      await api("/admin/api/rates", { method: "POST", body: { service, garment, unit: "pc", rate: v } });
      toast("Rate added");
    }
    RM_RATES = await api("/admin/api/rates");
    renderRateMatrix();
  } catch (e) { toast(e.message, true); }
}
function rmAddGarment() {
  const g = $("rm-garment").value.trim();
  if (!g) return;
  RM_EXTRA_G.push(g); $("rm-garment").value = "";
  renderRateMatrix();
  toast("Now type a rate in any service cell — it saves automatically");
}
function rmAddService() {
  const s = $("rm-service").value.trim();
  if (!s) return;
  RM_EXTRA_S.push(s); $("rm-service").value = "";
  renderRateMatrix();
}
let rateTimer = {};
function updRate(id, rate, active) {
  clearTimeout(rateTimer[id]);
  rateTimer[id] = setTimeout(async () => {
    try {
      const body = {};
      if (rate !== null) body.rate = parseFloat(rate);
      if (active !== null) body.is_active = active;
      await api(`/admin/api/rates/${id}`, { method: "PUT", body });
      toast("Rate saved — applies to new bills only");
    } catch (e) { toast(e.message, true); }
  }, 500);
}
async function addRate(btn) {
  await busy(btn, async () => {
    await api("/admin/api/rates", { method: "POST", body: {
      service: $("rt-svc").value.trim(), garment: $("rt-item").value.trim(),
      unit: $("rt-unit").value, rate: parseFloat($("rt-rate").value),
    }});
    toast("Rate added"); $("rt-item").value = ""; $("rt-rate").value = ""; loadSettings();
  });
}
const ROLE_LABEL = {
  WASHER: "Washer", DELIVERY: "Delivery",
  // Senior aadmi ko sirf "Washer" likhna uske kaam ko chhota dikhata hai
  SUPERVISOR: "Washerman / Manager", MANAGER: "Manager", ADMIN: "Owner",
};

/** +918707093136 -> +91 87070 93136 (never wraps mid-number, see .ph) */
function fmtPhone(p) {
  const m = String(p || "").match(/^\+91(\d{5})(\d{5})$/);
  return m ? `+91 ${m[1]} ${m[2]}` : p || "";
}

function renderStaff() {
  if (!STAFF.length) {
    $("staff-list").innerHTML =
      `<div class="emptystate">No staff added yet. Add your first team member above.</div>`;
    return;
  }
  $("staff-list").innerHTML = `<div class="stafflist">` + STAFF.map((s) => `
    <div class="staffrow ${s.is_active ? "" : "off"}">
      <div class="who">
        <div class="nm">${esc(s.name)}</div>
        <div class="meta">
          <span class="ph">${fmtPhone(s.phone)}</span>
          <span class="badge role">${ROLE_LABEL[s.role] || esc(s.role)}</span>
          <span class="statuspill ${s.is_active ? "on" : "off"}">${s.is_active ? "Active" : "Inactive"}</span>
          ${s.is_default ? `<span class="badge">Default</span>` : ""}
          ${s.also_customer ? `<span class="badge warn" title="Yeh number customer list mein bhi hai. Is number se aane wale message STAFF ke maane jayenge — customer wala AI jawab nahi milega.">Customer bhi</span>` : ""}
          ${s.active_orders ? `<span class="badge">${s.active_orders} active order${s.active_orders > 1 ? "s" : ""}</span>` : ""}
        </div>
      </div>
      <div class="acts">
        <button class="btn sm ghost" onclick="panelAccess('${s.id}')">${
          s.has_login ? "🔑 New password" : "🔑 Panel login"}</button>
        <button class="btn sm ghost" onclick="editStaffModal('${s.id}')">Edit</button>
        ${s.is_active
          ? `<button class="btn sm ghost" onclick="deactivateStaff('${s.id}')">Deactivate</button>`
          : `<button class="btn sm ghost" onclick="updStaff('${s.id}', {is_active: true})">Reactivate</button>`}
        <button class="btn sm danger" onclick="deleteStaffModal('${s.id}')">Delete</button>
      </div>
    </div>`).join("") + `</div>`;
}

function staffById(id) { return STAFF.find((s) => s.id === id); }

/* Staff ko /staff panel ka login dena.
 *
 * Password sirf EK BAAR dikhta hai — DB mein uska hash hi jata hai, isliye
 * baad mein "wo password kya tha" kahin se nikala nahi ja sakta. Bhool jaye
 * to yahi button dobara dabaiye, naya ban jayega (purane phone ke session
 * apne aap kat jate hain). */
function panelAccess(id) {
  const s = staffById(id);
  if (!s) return;
  // Owner ka apna row: koi role dropdown nahi. Wo koi "role" nahi hai, wo
  // maalik hai — aur is dropdown mein ADMIN hai hi nahi, isliye dikhane par
  // owner khud ko chup-chaap MANAGER bana baithta tha.
  const roleSel = s.role === "ADMIN" ? `
    <p class="muted" style="font-size:13px;margin:0 0 10px">
      This is the owner's own login. Their access stays as it is.</p>` : `
    <label>What will they do</label>
    <select id="pa-role">
      <option value="WASHER"${s.role === "WASHER" ? " selected" : ""}>Washerman — washing/pressing</option>
      <option value="DELIVERY"${s.role === "DELIVERY" ? " selected" : ""}>Delivery — pickup and delivery</option>
      <option value="SUPERVISOR"${s.role === "SUPERVISOR" ? " selected" : ""}>Washerman / Manager — works, and oversees everyone</option>
      <option value="MANAGER"${s.role === "MANAGER" ? " selected" : ""}>Manager — oversees only, does not work orders</option>
    </select>`;
  openModal(`
    <h3>Give ${esc(s.name)} a panel login</h3>
    <p class="muted" style="font-size:13px;margin:0 0 10px">
      They sign in at <b>${location.origin}/staff</b> with their own number and this
      password. It is shown once, and you can send it to them on WhatsApp.</p>
    ${roleSel}
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      ${s.has_login ? `<button class="btn danger" id="pa-revoke">Remove access</button>` : ""}
      <button class="btn" id="pa-go">${s.has_login ? "New password" : "Create login"}</button>
    </div>`);
  $("pa-go").onclick = (e) => busy(e.target, async () => {
    try {
      const sel = $("pa-role");
      const r = await api(`/admin/api/staff/${id}/access`, {
        method: "POST", body: sel ? { role: sel.value } : {},
      });
      // Local list ko TURANT sach bana do. Sirf loadSettings() par chhodne
      // se, agar refetch dhima ho, dobara khulne par wahi purana "Login
      // banayein" dikh jata tha — jaise login bana hi na ho.
      s.has_login = true;
      s.role = r.role;
      showPanelPassword(s, r);
      loadSettings();
    } catch (err) { toast(err.message, true, 9000); }
  });
  if (s.has_login) {
    $("pa-revoke").onclick = () => confirmDialog(
      `Remove ${s.name}'s panel access? All their logins stop immediately.`,
      async () => {
        try {
          const r = await api(`/admin/api/staff/${id}/access/revoke`, { method: "POST" });
          closeModal(); toast(`Access removed (${r.sessions_killed} login${r.sessions_killed === 1 ? "" : "s"} closed)`); loadSettings();
        } catch (err) { toast(err.message, true); }
      });
  }
}

/* Password ek hi baar dikhta hai, isliye yahin se bhej bhi dijiye.
 *
 * "Send on WhatsApp" server se jata hai — owner ka message app kholna,
 * password type karna, galat number chun lena, sab hat gaya. Password
 * server ko wapas jata hai kyunki DB mein sirf hash hai; server use hash
 * se milata hai, isliye ye endpoint "staff ko kuch bhi bhej do" nahi ban
 * sakta. Hamare apne message log mein password nahi likha jata.
 * Copy button rehta hai — kabhi WhatsApp na jaye to haath ka rasta. */
function showPanelPassword(s, r) {
  const name = s.name;
  const url = `${location.origin}/staff`;
  const msg = `${name}, your work panel: ${url}\nNumber: ${s.phone || "your WhatsApp number"}\nPassword: ${r.temp_password}\n(Log in and change the password the first time)`;
  openModal(`
    <h3>✅ ${esc(name)}'s login is ready</h3>
    <div class="dtl">
      <div class="dt-row"><span>Panel</span><b>${esc(url)}</b></div>
      <div class="dt-row"><span>Role</span><b>${esc(r.role)}</b></div>
      <div class="dt-row"><span>Password</span><b style="font-size:1.1rem">${esc(r.temp_password)}</b></div>
    </div>
    <p class="muted" style="font-size:12.5px;margin:10px 0 0">
      This password is shown only once — send it now. They will set their own
      on first login.</p>
    <div class="btnrow">
      <button class="btn ghost" id="pw-copy">📋 Copy</button>
      <button class="btn" id="pw-share">📤 Send on WhatsApp</button>
    </div>
    <div class="btnrow" style="margin-top:6px">
      <button class="btn ghost" onclick="closeModal()">Done</button>
    </div>`);
  $("pw-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(msg); toast("Copied — paste it in WhatsApp"); }
    catch (e) { toast("Could not copy — write the password down", true); }
  };
  // busy() apna purana label wapas laga deta hai, isliye yahan haath se —
  // bhej dene ke baad button ko jawab hi bane rehna chahiye.
  $("pw-share").onclick = async (e) => {
    const btn = e.currentTarget;
    const old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>';
    try {
      const out = await api(`/admin/api/staff/${s.id}/access/share`, {
        method: "POST", body: { password: r.temp_password },
      });
      btn.innerHTML = `✅ Sent to ${esc(fmtPhone(out.to))}`;
      toast(`Login sent to ${s.name} on WhatsApp`);
    } catch (err) {
      btn.disabled = false;
      btn.innerHTML = old;
      toast(err.message, true, 9000);
    }
  };
}

async function updStaff(id, body) {
  try { await api(`/admin/api/staff/${id}`, { method: "PUT", body }); toast(T.saved); loadSettings(); }
  catch (e) { toast(e.message, true); }
}

function editStaffModal(id) {
  const s = staffById(id);
  if (!s) return;
  const digits = String(s.phone || "").replace(/^\+91/, "");
  openModal(`<h3>Edit ${esc(s.name)}</h3>
    <div class="frm">
      <div class="setfield"><label for="es-name">Name</label>
        <input id="es-name" value="${esc(s.name)}">
        <small class="fielderr" id="es-name-err"></small></div>
      <div class="setfield"><label for="es-phone">Phone</label>
        <input id="es-phone" inputmode="numeric" value="${esc(digits)}">
        <small class="fielderr" id="es-phone-err"></small></div>
      <div class="setfield"><label for="es-role">Role</label>
        <select id="es-role">
          <option value="WASHER" ${s.role === "WASHER" ? "selected" : ""}>Washer (washing &amp; ironing)</option>
          <option value="DELIVERY" ${s.role === "DELIVERY" ? "selected" : ""}>Delivery</option>
        </select></div>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="es-save">Save changes</button>
    </div>`);
  $("es-save").onclick = (e) => busy(e.target, async () => {
    const name = $("es-name").value.trim();
    const phone = $("es-phone").value.replace(/\D/g, "");
    $("es-name-err").textContent = ""; $("es-phone-err").textContent = "";
    let bad = false;
    if (name.length < 2) { $("es-name-err").textContent = "Name must be at least 2 characters."; bad = true; }
    if (phone.length !== 10) { $("es-phone-err").textContent = "Phone must be exactly 10 digits."; bad = true; }
    if (bad) return;
    const res = await api(`/admin/api/staff/${id}`, { method: "PUT", body: { name, phone, role: $("es-role").value } });
    closeModal();
    // save ke turant baad batao ki agent ab kya karega — aur agar wo number
    // customer ka bhi hai to chetavni, jo chhupani nahi chahiye
    toast((res && res.ready) || "Staff updated", false, 7000);
    if (res && res.warning) toast(res.warning, true, 12000);
    loadSettings();
  });
}

function deactivateStaff(id) {
  const s = staffById(id);
  if (!s) return;
  confirmDialog(
    `Deactivate ${s.name}? They'll stop receiving order assignments. You can reactivate them later.`,
    async () => {
      try { await api(`/admin/api/staff/${id}`, { method: "DELETE" }); toast(`${s.name} deactivated`); loadSettings(); }
      catch (e) { toast(e.message, true); }
    });
}

function deleteStaffModal(id) {
  const s = staffById(id);
  if (!s) return;
  // Blocked cases are explained up front — deactivate stays available.
  const blocker = s.active_orders
    ? `${esc(s.name)} is assigned to ${s.active_orders} active order${s.active_orders > 1 ? "s" : ""}. Reassign or complete those orders first.`
    : s.is_default
      ? `${esc(s.name)} is set as the default washer or delivery person. Pick someone else in Daily operations first.`
      : "";
  if (blocker) {
    openModal(`<h3>Can't delete ${esc(s.name)}</h3>
      <p class="muted">${blocker}</p>
      <div class="btnrow">
        <button class="btn ghost" onclick="closeModal()">Close</button>
        ${s.is_active ? `<button class="btn" onclick="closeModal();deactivateStaff('${id}')">Deactivate instead</button>` : ""}
      </div>`);
    return;
  }
  openModal(`<h3>Delete ${esc(s.name)}</h3>
    <p class="muted">This removes them and their chat history for good. Type <b>${esc(s.name)}</b> to confirm.</p>
    <div class="frm">
      <div class="setfield">
        <input id="ds-confirm" placeholder="${esc(s.name)}" autofocus>
        <small class="fielderr" id="ds-err"></small>
      </div>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn danger" id="ds-go">Delete staff</button>
    </div>`);
  $("ds-go").onclick = (e) => busy(e.target, async () => {
    if ($("ds-confirm").value.trim() !== s.name) {
      $("ds-err").textContent = `Type the name exactly: ${s.name}`;
      return;
    }
    await api(`/admin/api/staff/${id}/permanent`, { method: "DELETE" });
    closeModal(); toast(`${s.name} deleted`); loadSettings();
  });
}

async function addStaff(btn) {
  await busy(btn, async () => {
    const name = $("sf-name").value.trim();
    const phone = $("sf-phone").value.replace(/\D/g, "");
    $("sf-name-err").textContent = ""; $("sf-phone-err").textContent = "";
    let bad = false;
    if (name.length < 2) { $("sf-name-err").textContent = "Name must be at least 2 characters."; bad = true; }
    if (phone.length !== 10) {
      $("sf-phone-err").textContent = "Phone must be exactly 10 digits.";
      bad = true;
    } else if (STAFF.some((s) => s.phone.replace(/^\+91/, "") === phone)) {
      $("sf-phone-err").textContent = "This number is already a staff member.";
      bad = true;
    }
    if (bad) return;
    const res = await api("/admin/api/staff", { method: "POST", body: { name, phone, role: $("sf-role").value } });
    toast((res && res.ready) || `${name} added`, false, 7000);
    if (res && res.warning) toast(res.warning, true, 12000);
    $("sf-name").value = ""; $("sf-phone").value = "";
    loadSettings();
  });
}

/* Instagram: say plainly whether it's actually connected. */
function igState() {
  const el = $("ig-status");
  if (!el) return;
  const linked = $("set-igid").value.trim() && $("set-igtoken").value.trim();
  el.className = "connstate" + (linked ? " on" : "");
  el.innerHTML = linked
    ? `<span class="dot"></span>Connected`
    : "Not connected — daily posters will only be sent to you on WhatsApp.";
}
function igPeek() {
  const inp = $("set-igtoken"), btn = $("ig-peek");
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  btn.textContent = show ? "Hide" : "Show";
}
async function saveOps(btn) {
  await busy(btn, async () => {
    const pairs = [
      ["standup_hour", parseInt($("set-standup").value)],
      ["turnaround_days", parseInt($("set-turnaround").value)],
      ["default_washer_phone", $("set-washer").value],
      ["marketing_freq_cap_per_month", parseInt($("set-freqcap").value)],
      ["marketing_monthly_msg_budget", parseInt($("set-budget").value)],
      ["marketing_autonomy", $("set-autonomy").value],
      ["social_daily_enabled", $("set-social").value === "true"],
      ["social_post_hour", parseInt($("set-socialhour").value) || 11],
      ["ig_user_id", $("set-igid").value.trim()],
      ["ig_access_token", $("set-igtoken").value.trim()],
    ];
    for (const [key, value] of pairs) await api("/admin/api/settings", { method: "PUT", body: { key, value } });
    toast("Settings saved — live immediately");
    igState();
  });
}
async function testStandup(btn) {
  await busy(btn, async () => {
    const r = await api("/admin/api/jobs/standup", { method: "POST" });
    toast(`Standup sent to ${r.sent_to} staff member(s)`);
  });
}

/* ============================= message formats ============================= */
let MSG_FORMATS = [];
async function mfLoad() {
  try {
    MSG_FORMATS = await api("/admin/api/message-formats");
    $("mf-key").innerHTML = MSG_FORMATS.map((m, i) =>
      `<option value="${i}">${m.overridden ? "✏️ " : ""}${esc(m.label)}</option>`).join("");
    mfPick();
  } catch (e) { toast(e.message, true); }
}
function mfPick() {
  const m = MSG_FORMATS[parseInt($("mf-key").value) || 0];
  if (!m) return;
  $("mf-text").value = m.current;
  $("mf-ph").textContent = "Placeholders: " + m.placeholders.map((p) => `{${p}}`).join("  ");
  $("mf-flag").innerHTML = m.overridden ? '<span class="tag">customised</span>' : '<span class="tag">default</span>';
}
async function mfSave(btn) {
  const m = MSG_FORMATS[parseInt($("mf-key").value) || 0];
  await busy(btn, async () => {
    await api("/admin/api/message-formats", { method: "PUT", body: { key: m.key, text: $("mf-text").value } });
    toast("Saved — live immediately"); mfLoad();
  });
}
async function mfReset(btn) {
  const m = MSG_FORMATS[parseInt($("mf-key").value) || 0];
  await busy(btn, async () => {
    await api("/admin/api/message-formats", { method: "PUT", body: { key: m.key, text: "" } });
    toast("Back to default"); mfLoad();
  });
}

/* ============================= inbox ============================= */
let THREADS = [], OPEN_PHONE = null, OPEN_THREAD = null, inboxTimer = null;
let LAST_HEAD_SIG = "", LAST_CHAT_PHONE = null, LAST_CHAT_HTML = "";
const EMOJIS = ["😀","😄","😊","🙏","👍","👌","✅","❤️","🎉","😅","😂","🤝","🧺","👔","🧼","⏰","📅","💰","🛵","⚠️","❓","🌟"];
/* The list is PAGED: 50 at a time as you scroll. A shop with 500+ imported
   contacts must open as fast as one with five. Search runs on the server so
   it reaches every contact, including ones who never messaged. */
const TH_PAGE = 50;
let TH_QUERY = "", TH_MORE = false, TH_TOTAL = 0, TH_LOADING = false;

async function fetchThreads(offset = 0) {
  const p = new URLSearchParams({ limit: TH_PAGE, offset, q: TH_QUERY });
  return api(`/admin/api/inbox/threads?${p}`);
}
async function loadThreads() {
  try {
    // the 12s auto-refresh must not yank a scrolled list back to page one
    const keep = Math.max(TH_PAGE, THREADS.length || 0);
    const r = await api(`/admin/api/inbox/threads?${new URLSearchParams(
      { limit: Math.min(keep, 500), offset: 0, q: TH_QUERY })}`);
    THREADS = r.threads; TH_MORE = r.has_more; TH_TOTAL = r.total;
  } catch (e) { $("th-list").innerHTML = errBox(e.message, "loadThreads"); return; }
  loadTplPreview();  // fire-and-forget: makes template bubbles readable
  renderThreads(); updateUnreadBadge();
  if (OPEN_PHONE) openThread(OPEN_PHONE, true, false);
  clearTimeout(inboxTimer);
  if (CURRENT === "inbox" && !document.hidden) inboxTimer = setTimeout(loadThreads, 12000);
}
/* Screen band / doosra app khula = polling band. Wapas aate hi taaza list.
   Phone ki battery aur data dono bachte hain, aur wapas aane par 12s ka
   intezaar nahi karna padta. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearTimeout(inboxTimer); return; }
  if (CURRENT === "inbox") loadThreads();
});
async function loadMoreThreads() {
  if (!TH_MORE || TH_LOADING) return;
  TH_LOADING = true;
  try {
    const r = await fetchThreads(THREADS.length);
    THREADS = THREADS.concat(r.threads);
    TH_MORE = r.has_more; TH_TOTAL = r.total;
    renderThreads();
  } catch (e) { toast(e.message, true); }
  finally { TH_LOADING = false; }
}
/* server-side search, debounced — typing must not fire a query per keystroke */
let _thSearchTimer = null;
function threadSearch(value) {
  clearTimeout(_thSearchTimer);
  _thSearchTimer = setTimeout(() => {
    TH_QUERY = (value || "").trim();
    loadThreads();
  }, 300);
}
function onThreadScroll(el) {
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 220) loadMoreThreads();
}
const avatar = (n) => `<div class="avatar">${esc((n || "?").trim()[0] || "?").toUpperCase()}</div>`;
/* Read-marker keys. Pehle "kk_seen_+919876543210" — yani customer ke
   NUMBER localStorage mein plain pade rehte the (device chori/shared PC =
   PII leak). Ab sirf ek non-reversible short hash. Purane plaintext keys
   pehli load par saaf ho jaate hain. */
const hashPhone = (p) => {
  let h = 5381;
  for (let i = 0; i < p.length; i++) h = ((h * 33) ^ p.charCodeAt(i)) >>> 0;
  return h.toString(36);
};
const seenKey = (p) => "kk_seen_" + hashPhone(String(p || ""));
(function purgeLegacySeenKeys() {
  try {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith("kk_seen_") && /[+0-9]{6,}/.test(k)) localStorage.removeItem(k);
    }
  } catch (e) {}
})();
const isUnread = (t) =>
  t.last_direction === "INBOUND" && t.last_at > (localStorage.getItem(seenKey(t.phone)) || "");
function updateUnreadBadge() {
  const n = THREADS.filter(isUnread).length;
  const b = $("unread-badge");
  if (b) { b.textContent = n; b.classList.toggle("show", n > 0); }
}
/* Phone ka asli jank yahi tha: har 12s poori list ka innerHTML replace —
   scroll upar kood jaata tha aur ungli ke neeche ka DOM gayab ho jaata tha.
   Ab: kuch badla hi nahi to DOM ko chhoo bhi nahi; badla to scroll wapas
   wahi rakho jahan tha. */
let LAST_TH_HTML = "";
function renderThreads() {
  const rows = THREADS;
  const items = rows.map((t) => {
    const chip = t.kind === "staff" ? '<span class="staff-chip">staff</span>' : t.kind === "admin" ? '<span class="staff-chip">👑 you</span>' : "";
    const preview = t.no_messages
      ? `<span class="pv-none">No messages yet — tap to start</span>`
      : `${isUnread(t) ? '<b style="color:#00A884">● </b>' : ""}${
          t.last_direction === "OUTBOUND" ? "✓✓ " : ""}${esc(previewText(t.last_text))}`;
    return `<div class="thread-item ${t.phone === OPEN_PHONE ? "on" : ""}"
      onclick="openThread('${t.phone}')" ontouchstart="thTouchStart(event,'${t.phone}')" ontouchend="thTouchEnd(event,'${t.phone}')">
      <div style="display:flex;gap:10px;align-items:center">
        ${avatar(displayName(t.name, t.phone))}
        <div style="flex:1;min-width:0">
          <div class="nm"><span>${esc(displayName(t.name, t.phone))}${chip}</span><span class="t">${fmtWhen(t.last_at)}</span></div>
          <div class="pv">${preview}</div>
        </div>
      </div></div>`;
  }).join("");
  const footer = TH_MORE
    ? `<div class="th-more" onclick="loadMoreThreads()">⬇ Show more (${rows.length}/${TH_TOTAL})</div>`
    : rows.length
      ? `<div class="th-end">${rows.length} ${TH_QUERY ? "found" : "chats"}</div>`
      : "";
  const html = (items || `<div class="thread-item">${
    TH_QUERY ? "No matches" : T.noData}</div>`) + footer;
  if (html === LAST_TH_HTML) return;          // data wahi ka wahi — repaint kyun?
  const el = $("th-list");
  const keep = el.scrollTop;
  el.innerHTML = html;
  el.scrollTop = keep;                        // scroll jahan tha wahin rahe
  LAST_TH_HTML = html;
}

/* swipe (left = mark read, right = agent toggle) + long-press menu fallback */
let _thTouch = null, _thTimer = null;
function thTouchStart(e, phone) {
  _thTouch = { x: e.touches[0].clientX, t: Date.now(), phone };
  clearTimeout(_thTimer);
  _thTimer = setTimeout(() => { _thTouch = null; threadMenu(phone); }, 550);
}
function thTouchEnd(e, phone) {
  clearTimeout(_thTimer);
  if (!_thTouch || _thTouch.phone !== phone) return;
  const dx = e.changedTouches[0].clientX - _thTouch.x;
  _thTouch = null;
  if (dx < -60) { markRead(phone); e.preventDefault(); }
  else if (dx > 60) { OPEN_PHONE = phone; toggleAgentPause(); e.preventDefault(); }
}
function markRead(phone) {
  localStorage.setItem(seenKey(phone), new Date().toISOString());
  renderThreads(); updateUnreadBadge(); toast("Marked read");
}
function threadMenu(phone) {
  const t = THREADS.find((x) => x.phone === phone) || {};
  openModal(`<h3>${esc(displayName(t.name, phone))}</h3>
    <div class="frm">
      <button class="btn" onclick="closeModal();openThread('${phone}')">💬 Open chat</button>
      <button class="btn ghost" onclick="closeModal();markRead('${phone}')">✓ Mark read</button>
      ${t.kind === "customer" ? `<button class="btn ghost" onclick="closeModal();OPEN_PHONE='${phone}';toggleAgentPause()">🤖 Agent on/off (take over)</button>` : ""}
    </div>`);
}

function closeThreadMobile(push = true) {
  document.body.classList.remove("chat-open");
  const pane = document.querySelector(".chatpane");
  if (pane) pane.removeAttribute("style");
  // Phone par chat band = sach mein band. OPEN_PHONE saaf kiye bina 12s ka
  // refresh (loadThreads -> openThread) wahi chat dobara khol deta tha —
  // back button ka koi matlab hi nahi rehta tha.
  if (window.innerWidth <= 767 && OPEN_PHONE) {
    OPEN_PHONE = null;
    if (THREADS.length) renderThreads();   // list ki green highlight bhi hatao
  }
  if (push && location.hash.startsWith("#inbox/")) {
    NAVIGATING = true; location.hash = "inbox"; setTimeout(() => (NAVIGATING = false), 0);
  }
}
function scrollChatBottom() {
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
  $("newmsg-pill").classList.remove("show");
}

/* One chat bubble — shared by the full render and the optimistic append on
   send, so a sent message can appear instantly without rebuilding the whole
   log (the old openThread() innerHTML rebuild was the freeze on send). */
function oneBubble(m, threadName) {
  let body = esc(m.text || "");
  const raw = m.text || "";
  const img = raw.match(/^\[image:(\/admin\/media\/[\w.\-]+)\]\s*(.*)$/s);
  const tpl = raw.match(/^\[template:([\w]+)\]\s*(.*)$/s);
  const btn = raw.match(/^\[button:([^\]]+)\]\s*(.*)$/s);
  const med = raw.match(/^\[(audio|voice|video|document)\:(\/admin\/media\/[\w.\-]+)\]\s*(.*)$/s);
  const loc = raw.match(/^\[location:([-\d.]+),([-\d.]+)\]\s*(.*)$/s);
  if (img) {
    body = `<img src="${mediaUrl(img[1])}" loading="lazy" width="280" height="210">${esc(img[2] || "")}`;
  } else if (med) {
    const url = mediaUrl(med[2]);
    const label = esc(med[3] || "");
    if (med[1] === "audio" || med[1] === "voice") {
      // WhatsApp ki voice note Ogg/Opus hoti hai — Safari aur iPhone use
      // baja hi nahi sakte, wahan player khali dabba dikhta tha. Isliye
      // saath mein hamesha ek link, jo har jagah kaam karta hai. Aur jab
      // awaaz ke shabd nikle hi na ho, wo bhi saaf likh dete hain — warna
      // owner ko lagta hai ki message khali aaya.
      body = `<audio controls preload="none" src="${url}" style="max-width:250px"></audio>`
        + `<a class="filechip" href="${url}" target="_blank" rel="noopener">🎧 Voice note kholein</a>`
        + (label ? `<div class="vtext">${label}</div>`
                 : `<div class="vtext muted">(awaaz ke shabd nahi mile)</div>`);
    } else if (med[1] === "video") {
      body = `<video controls preload="metadata" src="${url}" width="260" style="border-radius:8px"></video>${label}`;
    } else {
      body = `<a class="filechip" href="${url}" target="_blank" rel="noopener">📄 ${label || "Open file"}</a>`;
    }
  } else if (loc) {
    body = `<a class="filechip" target="_blank" rel="noopener"
      href="https://www.google.com/maps/search/?api=1&query=${loc[1]},${loc[2]}">📍 ${esc(loc[3] || "Location")}</a>`;
  } else if (tpl) {
    const t = TPL_PREVIEW[tpl[1]];
    const params = (tpl[2] || "").split(" | ").filter((p) => p !== "");
    let shown = tpl[2] || "";
    if (t && t.body) {
      shown = params.length
        ? t.body.replace(/\{\{(\d+)\}\}/g, (m0, n) => params[n - 1] ?? m0)
        : t.body;
    }
    // NOTE: keep this markup on ONE line — the bubble is pre-wrap, a newline
    // in the source rendered as a real blank line under every template.
    const btns = (t && t.buttons || []).filter(Boolean)
      .map((b) => `<div class="tplbtn">${esc(b)}</div>`).join("");
    body = `<div class="tplmsg"><span class="tpltxt">${esc(tidy(shown) || tpl[1])}</span><div class="tplname">📑 ${esc(tpl[1])}</div>${btns}</div>`;
  } else if (btn) {
    body = `<span class="tapped">👆 ${esc(btn[1])}</span>`;
  } else {
    // Bheje gaye message ka log "…text [buttons: A, B, C]" hota hai. Wo
    // kachra text ki tarah dikh raha tha — button WhatsApp par dabta hai,
    // yahan sirf ye dikhna chahiye ki kaunse option bheje the.
    const withBtns = (m.text || "").match(/^([\s\S]*?)\s*\[(buttons|list): ([^\]]+)\]\s*$/);
    if (withBtns) {
      const chips = withBtns[3].split(",").map((b) => b.trim()).filter(Boolean)
        .map((b) => `<div class="tplbtn">${esc(b)}</div>`).join("");
      body = `${esc(tidy(withBtns[1]))}<div class="sentbtns">${chips}</div>`;
    } else {
      body = esc(tidy(m.text || ""));
    }
  }
  const quoted = m.reply_to && BY_WAMID[m.reply_to];
  const quote = quoted
    ? `<div class="quoted"><span>${quoted.direction === "INBOUND" ? esc(threadName) : "You"}</span>${esc(oneLine(quoted.text)).slice(0, 90)}</div>`
    : "";
  const meta = `<span class="bt">${fmtClock(m.at)}${
    m.direction === "OUTBOUND" ? " · " + (m.sent_by || "bot") + ticks(m.status) : ""
  }</span>`;
  const reply = m.wamid
    ? `<button class="breply" title="Reply" aria-label="Reply" onclick="replyTo('${m.wamid}')">↩</button>`
    : "";
  return `<div class="bubble ${m.direction === "INBOUND" ? "in" : "out"}">${quote}${body}${meta}${reply}</div>`;
}

/* Optimistic append: drop ONE outbound bubble into the open log and scroll,
   instead of re-fetching + rebuilding the whole thread. The 12s poll later
   replaces the log with server truth (real wamid, delivery ticks), which
   reconciles this bubble — no duplicate, because that is a full replace.
   `bodyHtml` overrides the parsed body (used for a local image preview). */
function appendBubble(m, bodyHtml) {
  const log = $("chat-log");
  if (!log || OPEN_PHONE == null) return;
  if (log.querySelector(".empty")) log.innerHTML = "";   // was the empty state
  let html;
  if (bodyHtml != null) {
    const meta = `<span class="bt">${fmtClock(m.at)} · you <span class="tick">✓</span></span>`;
    html = `<div class="bubble out">${bodyHtml}${meta}</div>`;
  } else {
    html = oneBubble(m, OPEN_THREAD && OPEN_THREAD.name);
    if (m.wamid) BY_WAMID[m.wamid] = m;
  }
  log.insertAdjacentHTML("beforeend", html);
  // keep the poll's dedup cache honest so it does not instantly rebuild;
  // skip the blob-url image case (its cache would be stale on reload)
  if (bodyHtml == null) {
    LAST_CHAT_PHONE = OPEN_PHONE; LAST_CHAT_HTML = log.innerHTML;
    sessionStorage.setItem("kk_thread_" + OPEN_PHONE, log.innerHTML);
  } else {
    LAST_CHAT_HTML = "";   // force the next poll to paint the real image
  }
  scrollChatBottom();
}
const nowIso = () => new Date().toISOString();

/* Close every composer popover (emoji panel) — call after each send, and on
   a tap anywhere outside the panel (wired in init). */
function closePickers() {
  const pal = $("emoji-pal");
  if (pal) pal.classList.remove("open");
}

async function openThread(phone, silent = false, push = true) {
  OPEN_PHONE = phone;
  localStorage.setItem(seenKey(phone), new Date().toISOString());
  updateUnreadBadge();
  // !silent zaroori hai: 12s wala background refresh bhi yahan se guzarta
  // hai — wo sirf content taaza kare, band ki hui chat WAPAS na khole
  if (window.innerWidth <= 767 && !silent) {
    document.body.classList.add("chat-open");
    // belt + braces: inline styles guarantee the full-screen push even if
    // a stylesheet hiccups on some browser
    const pane = document.querySelector(".chatpane");
    if (pane) Object.assign(pane.style, {
      display: "flex", position: "fixed", top: "0", left: "0", right: "0",
      bottom: "0", zIndex: "70", background: "#fff", height: "100dvh",
    });
    if (push) {
      NAVIGATING = true;
      location.hash = "inbox/" + encodeURIComponent(phone);  // Android back closes chat
      setTimeout(() => (NAVIGATING = false), 0);
    }
  }
  const log = $("chat-log");
  const cached = sessionStorage.getItem("kk_thread_" + phone);
  if (!silent) {
    log.innerHTML = cached || skeleton(3);  // instant back-navigation
    renderThreads();
  }
  const prevCount = (OPEN_THREAD?.messages || []).length;
  try { OPEN_THREAD = await api(`/admin/api/inbox/thread?phone=${encodeURIComponent(phone)}`); }
  catch (e) { log.innerHTML = errBox(e.message, "loadThreads"); return; }
  const d = OPEN_THREAD;
  // header sirf tab bane jab uska CONTENT badla — warna har 12s Ping/Agent
  // button ungli ke neeche se recreate ho jaate the
  const headSig = `${d.phone}|${d.name}|${d.kind}|${d.window.open}`;
  if (!silent || headSig !== LAST_HEAD_SIG) {
    $("chat-head").innerHTML = `
    <button class="chat-back" onclick="closeThreadMobile()" aria-label="Back">←</button>
    ${avatar(displayName(d.name, d.phone))}
    <div style="flex:1;min-width:0"><b>${esc(displayName(d.name, d.phone))}</b>
      <div class="muted">${d.phone} · ${d.kind === "staff" ? "Staff 🧑‍🔧" : d.kind === "admin" ? "You 👑" : "Customer"}</div></div>
    <span class="winchip ${d.window.open ? "open" : "closed"}">${d.window.open ? "window open" : "window closed"}</span>
    <button class="btn sm ghost" title="Yaad dilao" onclick="pingThread(this)">🔔 Ping</button>
    ${d.kind === "customer" ? `<button class="btn sm ghost" id="agent-pause-btn" onclick="toggleAgentPause()">🤖 Agent: …</button>` : ""}`;
    refreshPauseBtn();
    LAST_HEAD_SIG = headSig;
  }
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  let lastDay = "";
  // quoted replies point at a wamid — keep a lookup so we can show the words
  BY_WAMID = {};
  (d.messages || []).forEach((m) => { if (m.wamid) BY_WAMID[m.wamid] = m; });
  const html = (d.messages || []).map((m) => {
    let chip = "";
    const day = new Date(m.at).toDateString();
    if (day !== lastDay) {
      lastDay = day;
      chip = `<div class="daychip">${dayName(m.at) || fmtDate(m.at)}</div>`;
    }
    return chip + oneBubble(m, d.name);
  }).join("") || emptyBox("Chat appears here", "💬");
  // wahi 12s wali baat: message log tabhi repaint ho jab sach mein naya
  // message/status aaya ho — warna bubbles + images har tick par flicker
  // karte the aur phone ka scroll atak jaata tha
  const same = silent && phone === LAST_CHAT_PHONE && html === LAST_CHAT_HTML;
  const newCount = (d.messages || []).length;
  if (!same) {
    log.innerHTML = html;
    sessionStorage.setItem("kk_thread_" + phone, html);
    LAST_CHAT_PHONE = phone; LAST_CHAT_HTML = html;
    if (nearBottom || !silent) scrollChatBottom();
    else if (newCount > prevCount) $("newmsg-pill").classList.add("show");
  } else if (!silent) scrollChatBottom();
}
async function refreshPauseBtn() {
  const btn = $("agent-pause-btn");
  if (!btn || !OPEN_PHONE) return;
  // customers list carries agent_paused? Not included — read from threads name only; do a light check via customers cache
  btn.textContent = "🤖 Agent: on/off";
  btn.onclick = toggleAgentPause;
}
let PAUSED_SET = new Set();
async function toggleAgentPause() {
  const nowPaused = !PAUSED_SET.has(OPEN_PHONE);
  try {
    await api("/admin/api/inbox/toggle-agent", { method: "POST", body: { phone: OPEN_PHONE, paused: nowPaused } });
    if (nowPaused) PAUSED_SET.add(OPEN_PHONE); else PAUSED_SET.delete(OPEN_PHONE);
    toast(nowPaused ? "You took over — the bot stays silent on this chat" : "Agent resumed on this chat");
  } catch (e) { toast(e.message, true); }
}
/* offline outbox: queued locally, flushed when the connection returns */
const outbox = () => JSON.parse(localStorage.getItem("kk_outbox") || "[]");
const saveOutbox = (q) => localStorage.setItem("kk_outbox", JSON.stringify(q));
async function flushOutbox() {
  const q = outbox();
  if (!q.length) return;
  const rest = [];
  for (const m of q) {
    try { await api("/admin/api/inbox/send", { method: "POST", body: m }); }
    catch (e) { rest.push(m); }
  }
  saveOutbox(rest);
  if (q.length !== rest.length) {
    toast(`${q.length - rest.length} queued message(s) sent`);
    if (OPEN_PHONE) openThread(OPEN_PHONE, true, false);
  }
}
/* ---- reply to one message (WhatsApp's swipe-to-reply) ---- */
let REPLY_TO = null;
function replyTo(wamid) {
  const m = BY_WAMID[wamid];
  if (!m) return;
  REPLY_TO = wamid;
  const bar = $("reply-bar");
  bar.innerHTML = `<div class="rq"><span>${m.direction === "INBOUND" ? esc(OPEN_THREAD?.name || "They") : "You"}</span>
      ${esc(oneLine(m.text)).slice(0, 110)}</div>
    <button class="rx" onclick="cancelReply()" aria-label="Cancel reply">✕</button>`;
  bar.classList.add("show");
  $("chat-input").focus();
}
function cancelReply() {
  REPLY_TO = null;
  const bar = $("reply-bar");
  bar.classList.remove("show");
  bar.innerHTML = "";
}

async function pingThread(btn) {
  if (!OPEN_PHONE) return;
  await busy(btn, async () => {
    const r = await api("/admin/api/inbox/ping", { method: "POST", body: { phone: OPEN_PHONE } });
    toast("🔔 Ping sent: " + oneLine(r.text).slice(0, 60));
    openThread(OPEN_PHONE, true, false);
  });
}

async function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || !OPEN_PHONE) return;
  const replyTo = REPLY_TO;
  input.value = "";              // clear input
  cancelReply();
  closePickers();                // emoji panel closes on send
  if (!navigator.onLine) {
    saveOutbox([...outbox(), { phone: OPEN_PHONE, text, reply_to: replyTo }]);
    toast("Offline — message is queued, it will send once you're back online");
    return;
  }
  try {
    await api("/admin/api/inbox/send", {
      method: "POST", body: { phone: OPEN_PHONE, text, reply_to: replyTo },
    });
    // show it instantly — no full-thread rebuild; the 12s poll reconciles
    appendBubble({ direction: "OUTBOUND", text, at: nowIso(), sent_by: "you", status: "sent", reply_to: replyTo });
  } catch (e) { toast(e.message, true); input.value = text; }
}
/* new chat with any number */
function newChatModal() {
  openModal(`<h3>➕ New chat</h3>
    <div class="frm">
      <div><label>Mobile number</label><input id="nc-phone" type="tel" inputmode="numeric" placeholder="98765 43210" autofocus></div>
      <div><label>Name (optional)</label><input id="nc-name" placeholder="Customer name"></div>
    </div>
    <p class="muted">For a brand-new number the first message can only be an <b>approved template</b> (WhatsApp's rule) — use the 📑 button once the chat opens.</p>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn" id="nc-go">Open chat</button></div>`);
  $("nc-go").onclick = (e) => busy(e.target, async () => {
    const r = await api("/admin/api/inbox/new-chat", { method: "POST",
      body: { phone: $("nc-phone").value.trim(), name: $("nc-name").value.trim() || null } });
    closeModal();
    await loadThreads();
    openThread(r.phone);
  });
}

/* template picker send (works outside the 24h window) */
let TPL_CACHE = [];
async function tplSendModal() {
  if (!OPEN_PHONE) { toast("Open a chat first", true); return; }
  openModal(`<h3>📑 Send template</h3><div class="frm" id="tps-body">${skeleton(2)}</div>`);
  let note = "";
  try {
    TPL_CACHE = (await api("/admin/api/templates")).filter((t) => t.status === "APPROVED");
    if (!TPL_CACHE.length) note = "⚠️ No template is APPROVED at Meta yet — sending may fail.";
    if (!TPL_CACHE.length) TPL_CACHE = await api("/admin/api/templates/registry");
  } catch (e) {
    // Meta down/blocked -> local registry, honest warning
    try { TPL_CACHE = await api("/admin/api/templates/registry"); } catch (e2) { TPL_CACHE = []; }
    note = "⚠️ Meta API is unreachable — this list is from the local registry; sending may fail.";
  }
  if (!TPL_CACHE.length) {
    $("tps-body").innerHTML = `<p class="muted">No templates found.</p>
      <div class="btnrow"><button class="btn ghost" onclick="closeModal()">OK</button></div>`;
    return;
  }
  if (note) note = `<p class="muted" style="color:var(--warn)">${note}</p>`;
  $("tps-body").innerHTML = `
    ${note}
    <div><label>Template</label><select id="tps-name" onchange="tplSendPick()">
      ${TPL_CACHE.map((t, i) => `<option value="${i}">${esc(t.name)} (${t.category})</option>`).join("")}
    </select></div>
    <div id="tps-params"></div>
    <div style="background:#e5ddd5;border-radius:10px;padding:10px"><div id="tps-preview" style="background:#fff;border-radius:8px;padding:8px 10px;font-size:13px;white-space:pre-wrap"></div></div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="tps-go">Send template</button></div>`;
  tplSendPick();
  $("tps-go").onclick = (e) => busy(e.target, async () => {
    const t = TPL_CACHE[parseInt($("tps-name").value)];
    const n = (t.body.match(/\{\{\d+\}\}/g) || []).length;
    const params = [];
    for (let i = 1; i <= n; i++) params.push($("tps-p" + i).value.trim());
    if (params.some((p) => !p)) throw new Error("Fill in all the variables");
    await api("/admin/api/inbox/send-template", { method: "POST",
      body: { phone: OPEN_PHONE, template_name: t.name, params } });
    closeModal(); closePickers(); toast(T.sent);
    // show the sent template bubble immediately (oneBubble renders the marker)
    appendBubble({ direction: "OUTBOUND", at: nowIso(), sent_by: "you", status: "sent",
      text: `[template:${t.name}] ${params.join(" | ")}` });
  });
}
function tplSendPick() {
  const t = TPL_CACHE[parseInt($("tps-name").value) || 0];
  if (!t) return;
  const n = (t.body.match(/\{\{\d+\}\}/g) || []).length;
  $("tps-params").innerHTML = Array.from({ length: n }, (_, i) =>
    `<div><label>Variable {{${i + 1}}}</label><input id="tps-p${i + 1}" oninput="tplSendPrev()"></div>`).join("");
  tplSendPrev();
}
function tplSendPrev() {
  const t = TPL_CACHE[parseInt($("tps-name").value) || 0];
  if (!t) return;
  let body = t.body;
  (body.match(/\{\{(\d+)\}\}/g) || []).forEach((m) => {
    const i = m.replace(/\D/g, "");
    body = body.replace(m, $("tps-p" + i)?.value || `[${i}]`);
  });
  $("tps-preview").textContent = body;
}

/* bulk customer numbers */
function bulkImportModal() {
  openModal(`<h3>📥 Contacts import</h3>
    <div class="tabs2">
      <button class="tab2 on" id="bi-tab-file" onclick="biTab('file')">📄 Excel / CSV</button>
      <button class="tab2" id="bi-tab-text" onclick="biTab('text')">⌨️ Type / paste</button>
    </div>
    <div id="bi-pane-file">
      <label class="dropzone" id="bi-drop">
        <input type="file" id="bi-file" accept=".csv,.tsv,.xlsx,.xlsm,text/csv" style="display:none">
        <div class="dz-in"><b>📄 Choose a file or drop it here</b>
          <span class="muted">.xlsx or .csv — up to 5MB</span></div>
      </label>
      <div id="bi-file-name" class="muted" style="margin-top:8px"></div>
      <p class="muted" style="margin-top:10px">Columns named <b>phone / mobile / number</b>, <b>name</b>, <b>address</b>
        are detected automatically. No headings? Also fine — any cell that looks like a number is treated as one.
        Existing contacts are <b>never overwritten</b> — only their blank fields are filled in.</p>
    </div>
    <div id="bi-pane-text" style="display:none">
      <div class="frm">
        <textarea id="bi-text" rows="8" placeholder="One number per line:\n9876543210\nSharma ji, 9812345678\nSeema Mam, 98111 22333"></textarea>
      </div>
      <p class="muted">Format: just the number, or 'name, number'.</p>
    </div>
    <div id="bi-result"></div>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn" id="bi-go">Import</button></div>`);

  const drop = $("bi-drop"), fileIn = $("bi-file");
  const showName = () => {
    $("bi-file-name").textContent = fileIn.files?.[0] ? "✅ " + fileIn.files[0].name : "";
  };
  fileIn.onchange = showName;
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer.files?.[0]) { fileIn.files = e.dataTransfer.files; showName(); }
  });

  $("bi-go").onclick = (e) => busy(e.target, async () => {
    let r;
    if (BI_MODE === "file") {
      const f = fileIn.files?.[0];
      if (!f) { toast("Choose a file first", true); return; }
      const fd = new FormData();
      fd.append("file", f);
      r = await api("/admin/api/customers/import-file", { method: "POST", body: fd });
    } else {
      r = await api("/admin/api/customers/bulk", { method: "POST", body: { text: $("bi-text").value } });
    }
    // A row that did not import is the owner's problem to fix — show it,
    // don't bury it in a toast that disappears.
    const badTotal = r.invalid_total ?? (r.invalid || []).length;
    $("bi-result").innerHTML = `<div class="imp-res">
      <div class="imp-row"><b>${r.added}</b> new contacts added</div>
      ${r.updated ? `<div class="imp-row"><b>${r.updated}</b> had blanks filled in</div>` : ""}
      <div class="imp-row muted">${r.skipped_existing} already existed</div>
      ${badTotal ? `<div class="imp-row bad"><b>${badTotal}</b> line(s) could not be read:
        <ul>${(r.invalid || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${badTotal > (r.invalid || []).length ? `<span class="muted">…and ${badTotal - r.invalid.length} more</span>` : ""}</div>` : ""}
    </div>`;
    toast(`${r.added} new contacts added`);
    if (CURRENT === "customers") loadCustomers();
    if (CURRENT === "inbox") loadThreads();
  });
}
let BI_MODE = "file";
function biTab(mode) {
  BI_MODE = mode;
  $("bi-pane-file").style.display = mode === "file" ? "" : "none";
  $("bi-pane-text").style.display = mode === "text" ? "" : "none";
  $("bi-tab-file").classList.toggle("on", mode === "file");
  $("bi-tab-text").classList.toggle("on", mode === "text");
}

function toggleEmojis() { $("emoji-pal").classList.toggle("open"); }
function addEmoji(e) { $("chat-input").value += e; $("chat-input").focus(); }
async function sendMedia(input) {
  if (!input.files || !input.files[0] || !OPEN_PHONE) return;
  const file = input.files[0];
  closePickers();                // emoji panel closes on send
  const fd = new FormData();
  fd.append("phone", OPEN_PHONE);
  fd.append("file", file);
  fd.append("caption", "");
  // local preview URL so the image shows the moment it uploads
  const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
  try {
    await api("/admin/api/inbox/send-media", { method: "POST", body: fd });
    toast(T.sent);
    if (preview) {
      appendBubble({ direction: "OUTBOUND", at: nowIso() },
        `<img src="${preview}" width="280" height="210" style="object-fit:cover;border-radius:10px;display:block">`);
    } else {
      openThread(OPEN_PHONE, true, false);   // non-image: let the poll paint it
    }
  } catch (e) { toast(e.message, true); }
  input.value = "";              // reset the file input so re-picking the same file fires change
}


/* ================= mobile UI probe ==================
   Screen main dekh nahi sakta, isliye phone khud naap kar bhejta hai:
   kya viewport se bahar nikla hua hai, kaunsa tap-target chhota hai, kya
   JS error aaya. `?probe=1` lagao, ek baar scroll karo, bas. */
function _uiOffenders() {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  // Neeche chipki hui cheezein (tabbar) content ko dhak leti hain — unki
  // ooncha-i nikaal lo taaki "kya chhup gaya" bataya ja sake.
  let coverBottom = 0;
  for (const f of document.querySelectorAll("*")) {
    const st = getComputedStyle(f);
    if (st.position !== "fixed" || st.display === "none") continue;
    const r = f.getBoundingClientRect();
    if (r.bottom >= vh - 2 && r.height > 0 && r.height < vh / 2) {
      coverBottom = Math.max(coverBottom, r.height);
    }
  }

  for (const e of document.querySelectorAll("body *")) {
    const st = getComputedStyle(e);
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") continue;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;

    // Poori tarah baayein parked cheez = chhupa hua drawer (sidebar), bug
    // nahi. Pehle isi ne saari report bhar di thi.
    if (r.right <= 0 || r.left >= vw) continue;
    if (st.position === "fixed") continue;

    // 1. dayein se bahar = side scroll / kata hua content
    if (r.right > vw + 1) {
      out.push({ why: "right", el: _elName(e), left: Math.round(r.left),
                 right: Math.round(r.right), w: Math.round(r.width),
                 text: (e.textContent || "").trim().slice(0, 40) });
      continue;
    }
    // 2. text apne dabbe mein nahi sama raha (kat raha hai)
    // "…" se katna design hai, bug nahi — usse chhodo
    if (e.scrollWidth > e.clientWidth + 2 && e.clientWidth > 0 && st.overflowX !== "auto"
        && st.overflowX !== "scroll" && st.textOverflow !== "ellipsis"
        && (e.textContent || "").trim()) {
      out.push({ why: "clipped", el: _elName(e), w: Math.round(r.width),
                 need: e.scrollWidth, has: e.clientWidth,
                 text: (e.textContent || "").trim().slice(0, 40) });
      continue;
    }
    // 3. neeche ki fixed patti ke peeche chhup gaya
    if (coverBottom && r.top < vh && r.bottom > vh - coverBottom && r.height < 200
        && (e.textContent || "").trim() && e.children.length === 0) {
      out.push({ why: "under_tabbar", el: _elName(e), bottom: Math.round(r.bottom),
                 covered_from: Math.round(vh - coverBottom),
                 text: (e.textContent || "").trim().slice(0, 40) });
      continue;
    }
    // 4. padhne layak nahi
    const fs = parseFloat(st.fontSize);
    if (fs && fs < 11 && (e.textContent || "").trim() && e.children.length === 0) {
      out.push({ why: "tiny_text", el: _elName(e), font: fs,
                 text: (e.textContent || "").trim().slice(0, 40) });
    }
  }
  return out.slice(0, 30);
}

function _elName(e) {
  const cls = String(e.className || "").trim().split(/\s+/).filter(Boolean).slice(0, 2).join(".");
  return e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (cls ? "." + cls : "");
}

let _uiErrors = [];
function startUiProbe() {
  window.addEventListener("error", (e) =>
    _uiErrors.push(`${e.message} @ ${String(e.filename || "").split("/").pop()}:${e.lineno}`));
  window.addEventListener("unhandledrejection", (e) =>
    _uiErrors.push("promise: " + String(e.reason).slice(0, 120)));

  const banner = document.createElement("div");
  banner.style.cssText =
    "position:fixed;left:8px;right:8px;bottom:8px;z-index:9999;background:#111B21;color:#fff;" +
    "padding:10px 12px;border-radius:10px;font:600 13px/1.4 system-ui;text-align:center";
  banner.textContent = "🔎 UI probe running…";
  document.body.appendChild(banner);

  const send = async (label) => {
    const body = {
      label, section: CURRENT, url: location.href,
      ua: navigator.userAgent,
      viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 },
      page: { scrollW: document.documentElement.scrollWidth, scrollH: document.documentElement.scrollHeight },
      side_scroll: document.documentElement.scrollWidth > window.innerWidth + 1,
      overflow: _uiOffenders(),
      tiny_taps: _uiTinyTaps(),
      errors: _uiErrors.slice(0, 10),
    };
    try {
      await fetch("/admin/api/ui-report", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), credentials: "same-origin",
      });
      const by = {};
      body.overflow.forEach((o) => (by[o.why] = (by[o.why] || 0) + 1));
      const bits = Object.entries(by).map(([k, v]) => `${v} ${k}`);
      banner.textContent =
        `✅ ${label}: ` + (bits.join(", ") || "layout ok") +
        `, ${body.tiny_taps.length} small buttons, ${body.errors.length} errors`;
    } catch (e) {
      banner.textContent = "⚠️ Could not send report: " + e.message;
    }
  };

  setTimeout(() => send("load"), 1500);
  // har section badalne par dobara — "sara UI" ka matlab har screen
  let last = CURRENT;
  setInterval(() => {
    if (CURRENT !== last) { last = CURRENT; setTimeout(() => send(CURRENT), 800); }
  }, 1000);
  banner.onclick = () => send("manual");
}

/* ============================= init ============================= */
window.addEventListener("DOMContentLoaded", () => {
  // ?probe=1 -> phone khud batata hai ki kya toota hai.
  // Purana version findings <title> mein likhta tha — mobile par title
  // dikhta hi nahi. Ab report server par chali jaati hai.
  if (qs.get("probe")) {  // qs captured before replaceState strips the query
    startUiProbe();
  }
  $("emoji-pal").innerHTML = EMOJIS.map((e) => `<span onclick="addEmoji('${e}')">${e}</span>`).join("");
  // Ab do raste hain: asli login (session cookie) ya purani admin key.
  // Session hai to key maangna bilkul galat hai — isliye pehle poochho.
  // Live updates SIRF sign-in ke baad. Bina iske EventSource 401 par
  // baar-baar dobara judne ki koshish karta rehta — har teen second ek
  // request, login page par baithe rehne bhar ke liye.
  ensureSignedIn().then((ok) => { if (ok) startLiveUpdates(); });
  const h = (location.hash || "#dashboard").slice(1);
  if (h.startsWith("inbox/")) {
    go("inbox", false);
    // Phone par chat kholte hi URL mein "#inbox/<number>" chipak jaata hai.
    // Us hash ke saath agli baar page khulne par seedha CHAT khul jaata tha —
    // user ne "Inbox" socha tha, list dikhni chahiye thi. Aur us waqt
    // history mein list ka koi kadam hota hi nahi, isliye phone ka BACK
    // button poori site se bahar phenk deta tha.
    if (window.matchMedia("(max-width: 767px)").matches) {
      history.replaceState(null, "", "#inbox");
    } else {
      // Bade screen par dono pane ek saath dikhte hain — wahan deep link
      // se chat kholna sahi hai, list gayab nahi hoti.
      setTimeout(() => openThread(decodeURIComponent(h.slice(6)), false, false), 300);
    }
  } else if (h.startsWith("settings/")) {
    go("settings", false);
    stTab(h.slice(9));
  } else {
    go(h, false);
  }

  // keyboard access: the nav / tab bar / sheet items are divs — give them
  // focus + Enter/Space so the app is usable without a touchscreen or mouse
  document.querySelectorAll(".nav div[data-s], .tabbar div, .sheet-grid div, .logout")
    .forEach((el) => { el.setAttribute("tabindex", "0"); el.setAttribute("role", "button"); });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") &&
        e.target.matches?.(".nav div[data-s], .tabbar div, .sheet-grid div, .logout")) {
      e.preventDefault(); e.target.click();
    }
  });

  // tap anywhere outside the emoji panel (and not on its toggle) closes it
  document.addEventListener("click", (e) => {
    const pal = $("emoji-pal");
    if (!pal || !pal.classList.contains("open")) return;
    if (e.target.closest("#emoji-pal")) return;                       // picking an emoji
    if (e.target.closest("[onclick*='toggleEmojis']")) return;        // the toggle itself
    pal.classList.remove("open");
  });

  // PWA: app-shell cache -> instant repeat loads, shell survives offline blips
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/admin/sw.js", { scope: "/admin" }).catch(() => {});
  }

  // offline awareness
  const setNet = () => {
    $("offline-bar").classList.toggle("show", !navigator.onLine);
    if (navigator.onLine) flushOutbox();
  };
  window.addEventListener("online", setNet);
  window.addEventListener("offline", setNet);
  setNet();

  // keyboard-safe composer: visualViewport shrinks -> chat pane follows,
  // so the input is never hidden behind the on-screen keyboard
  if (window.visualViewport) {
    const vv = window.visualViewport;
    const fit = () => {
      const pane = document.querySelector("body.chat-open .chatpane");
      if (!pane) return;
      pane.style.height = vv.height + "px";
      scrollChatBottom();
    };
    vv.addEventListener("resize", fit);
    vv.addEventListener("scroll", fit);
  }

  // "new messages" pill hides once the user reaches the bottom themselves
  $("chat-log").addEventListener("scroll", () => {
    const log = $("chat-log");
    if (log.scrollHeight - log.scrollTop - log.clientHeight < 40)
      $("newmsg-pill").classList.remove("show");
  });
});
