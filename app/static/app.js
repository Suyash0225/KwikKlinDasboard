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
  RECEIVED: "New", IN_WASH: "Washing", IN_DRY: "Drying", IN_IRON: "Ironing",
  READY: "Ready", OUT_FOR_DELIVERY: "Out for delivery", DELIVERED: "Delivered",
  ON_HOLD: "On hold", CANCELLED: "Cancelled",
};
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
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const money = (v) => "₹" + inr.format(Number(v || 0));
const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
};
const fmtWhen = (iso) => {
  const d = new Date(iso), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
};
function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), err ? 5000 : 2600);
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

function kkLogout() {
  localStorage.removeItem("kk_admin_key");
  KEY = "";
  toast("Logged out");
  showLogin();
}

/* login */
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
  tasks: ["Tasks", "Kisko kya kaam diya — pending, hua, kisne kya kaha"],
  agents: ["Agents", "Your AI employees — health and controls"],
  usage: ["AI usage", "Kitna AI use hua aur kitna kharch"],
  training: ["AI training", "Teach the agent your business"],
  activity: ["Activity", "Everything the agent did, and why"],
  settings: ["Settings", "Rates, staff, shop and agent"],
};
let CURRENT = "dashboard", NAVIGATING = false;
function go(sec, push = true) {
  if (!SECTIONS.includes(sec)) sec = "dashboard";
  CURRENT = sec;
  closeSheet();
  // leaving a full-screen mobile chat closes it
  if (sec !== "inbox") closeThreadMobile(false);
  SECTIONS.forEach((s) => { const el = $("sec-" + s); if (el) el.style.display = s === sec ? "" : "none"; });
  document.querySelectorAll(".nav div[data-s]").forEach((el) => el.classList.toggle("on", el.dataset.s === sec));
  document.querySelectorAll(".tabbar div[data-s]").forEach((el) => el.classList.toggle("on", el.dataset.s === sec));
  $("mob-title").textContent = TITLES[sec][0];
  $("sidebar").classList.remove("open");
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
    $("wa-stats").innerHTML = `<span class="muted">📱 WhatsApp stats nahi mile: ${esc(e.message)}</span>`;
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
    .concat(STATUS_SEQ.filter((s) => s !== "DELIVERED").map((s) => [s, `${STATUS_LABEL[s]} <b>${by[s] || 0}</b>`]));
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
        <td>${esc(o.customer)}<div class="muted">${esc(o.phone)}</div></td>
        <td style="max-width:190px"><div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(itemsText(o.items))}</div></td>
        <td><span class="pill ${o.status}">${STATUS_LABEL[o.status]}</span>${isOverdue(o) ? ' <span class="pill UNPAID">Overdue</span>' : ""}</td>
        <td><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span><div class="muted">${money(o.amount_paid)} / ${o.total_amount ? money(o.total_amount) : "—"}</div></td>
        <td>${fmtDate(o.expected_delivery)}</td>
        <td><div class="act">
          <button class="btn sm ghost" onclick="statusModal('${o.order_number}','${o.status}')">Status</button>
          <button class="btn sm ghost" onclick="paymentModal('${o.order_number}')">Payment</button>
          <button class="btn sm ghost" onclick="dateModal('${o.order_number}')">Date</button>
          <button class="btn sm ghost" onclick="orderDetail('${o.order_number}')">👁</button>
          <button class="btn sm ghost" onclick="jumpChat('${o.phone}')">💬</button>
        </div></td>
      </tr>`).join("")}
    </tbody></table>
    <div class="rowcards">${rows.map((o) => `
      <div class="rowcard ${isOverdue(o) ? "overdue" : ""}">
        <div class="r1"><b>${o.order_number}</b><span class="pill ${o.status}">${STATUS_LABEL[o.status]}</span></div>
        <div class="kv"><span>${esc(o.customer)}</span><span>${esc(o.phone)}</span></div>
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

function statusModal(number, current) {
  const nexts = STATUS_SEQ.slice(STATUS_SEQ.indexOf(current) + 1).concat(["ON_HOLD", "CANCELLED"]);
  openModal(`<h3>Update status — ${number}</h3>
    <p class="muted">Current: ${STATUS_LABEL[current]}. Customer is notified automatically on Ready / Out for delivery / Delivered.</p>
    <div class="frm" style="margin-top:10px">
      <select id="st-new">${nexts.map((s) => `<option value="${s}">${STATUS_LABEL[s]}</option>`).join("")}</select>
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
    ? `<div class="muted">Baki: ${money(due)} (bill ${money(o.total_amount)}, mila ${money(o.amount_paid || 0)}) — advance/extra bhi chalega</div>` : "";
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
      <h3>${o.order_number} <span class="pill ${o.status}">${STATUS_LABEL[o.status]}</span></h3>
      <p class="muted">${esc(o.customer_name || "")} · ${esc(o.customer_phone)}</p><hr class="hr">
      <b>Items</b>
      ${(o.items || []).map((i) => `<div class="sumrow"><span>${i.qty} × ${esc(i.type || i.garment || i.service || "?")}</span><span>${i.amount != null ? money(i.amount) : ""}</span></div>`).join("")}
      <div class="sumrow"><span>Discount</span><span>${money(o.discount_amount || 0)}</span></div>
      <div class="sumrow"><span>GST</span><span>${money(o.gst_amount || 0)}</span></div>
      <div class="sumrow total"><span>Total</span><span>${o.total_amount ? money(o.total_amount) : "—"}</span></div>
      <div class="sumrow"><span>Paid</span><span>${money(o.amount_paid)} <span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></span></div>
      <hr class="hr"><b>Timeline</b>
      ${(d.history || []).map((h) => `<div class="sumrow"><span>${STATUS_LABEL[h.new_status] || h.new_status}</span><span class="muted">${fmtWhen(h.changed_at)} · ${esc(h.changed_by)}</span></div>`).join("")}
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
function applyPreset() {
  const i = $("nb-preset").value;
  if (i === "") { $("nb-disc").value = ""; calcBill(); return; }
  const p = (SETTINGS_CACHE.discount_presets || [])[parseInt(i)];
  if (!p) return;
  let sub = 0;
  LINES.forEach((l) => { sub += (parseFloat(l.rate) || 0) * (parseFloat(l.qty) || 0); });
  $("nb-disc").value = p.type === "percent" ? Math.round(sub * p.value) / 100 : p.value;
  calcBill();
}
function addLine() { LINES.push({ service: "", garment: "", qty: 1, rate: "", amount: 0 }); renderLines(); }
function delLine(i) { LINES.splice(i, 1); if (!LINES.length) addLine(); renderLines(); }
const services = () => [...new Set(RATES.filter((r) => r.is_active).map((r) => r.service))];
const garmentsFor = (svc) => RATES.filter((r) => r.is_active && r.service === svc);

function renderLines() {
  $("nb-lines").innerHTML = LINES.map((l, i) => `
    <div class="lineitem">
      <select onchange="LINES[${i}].service=this.value;LINES[${i}].garment='';lineRate(${i})">
        <option value="">Service…</option>
        ${services().map((s) => `<option ${l.service === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
      </select>
      <select onchange="LINES[${i}].garment=this.value;lineRate(${i})">
        <option value="">Item…</option>
        ${garmentsFor(l.service).map((r) => `<option value="${esc(r.garment)}" ${l.garment === r.garment ? "selected" : ""}>${esc(r.garment || "(per kg)")} — ₹${r.rate}/${r.unit}</option>`).join("")}
      </select>
      <input type="number" min="0.1" step="0.1" value="${l.qty}" onchange="LINES[${i}].qty=parseFloat(this.value)||1;calcBill()" title="Qty / kg">
      <input type="number" min="0" step="0.01" value="${l.rate}" placeholder="Rate" onchange="LINES[${i}].rate=parseFloat(this.value)||0;calcBill()">
      <div class="money" id="nb-amt-${i}">${money(l.amount)}</div>
      <button class="btn sm danger del" onclick="delLine(${i})">✕</button>
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
  const disc = parseFloat($("nb-disc").value) || 0;
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
function custAc() {
  const q = $("nb-phone").value.trim().toLowerCase();
  const box = $("nb-ac");
  if (!q || !CUSTOMERS_CACHE) { box.innerHTML = ""; return; }
  const hits = CUSTOMERS_CACHE.filter((c) => c.phone.includes(q) || (c.name || "").toLowerCase().includes(q)).slice(0, 6);
  box.innerHTML = hits.map((c) => `<div onclick="pickCust('${c.phone}','${esc(c.name || "")}')">${esc(c.name || "New customer")} · ${c.phone}</div>`).join("");
}
function pickCust(phone, name) { $("nb-phone").value = phone; $("nb-name").value = name; $("nb-ac").innerHTML = ""; }

async function saveBill(btn) {
  await busy(btn, async () => {
    const t = calcBill();
    const phone = $("nb-phone").value.trim();
    if (!phone) throw new Error("Customer phone is required");
    const items = LINES.filter((l) => l.service && (l.garment || l.service.toLowerCase().includes("kg"))).map((l) => {
      const r = RATES.find((x) => x.service === l.service && x.garment === l.garment) || {};
      return { type: l.garment || l.service, service: l.service, qty: l.qty, rate: l.rate, amount: l.amount, unit: r.unit || "pc" };
    });
    if (!items.length) throw new Error("Add at least one item");
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
    ["nb-phone", "nb-name", "nb-disc", "nb-adv", "nb-notes", "nb-coupon"].forEach((id) => ($(id).value = ""));
    loadDashboard();
  });
}
function showBillSuccess(o) {
  openModal(`<h3>✅ ${o.order_number} created</h3>
    <p class="muted">Total ${o.total_amount ? money(o.total_amount) : "—"} · ${esc(o.customer_name || o.customer_phone)}. Customer notified on WhatsApp; staff got the work order.</p>
    <div class="btnrow" style="margin-top:12px">
      <button class="btn ghost" onclick="printReceipt(${esc(JSON.stringify(o)).replace(/"/g, "&quot;")})">🖨 Print receipt</button>
      <button class="btn ghost" onclick="waBill(${esc(JSON.stringify(o)).replace(/"/g, "&quot;")})">📲 Send bill on WhatsApp</button>
      <button class="btn" onclick="closeModal()">Done</button>
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
async function waBill(o) {
  try {
    await api("/admin/api/inbox/send", { method: "POST", body: { phone: o.customer_phone, text: receiptText(o) } });
    toast(T.sent);
  } catch (e) { toast(e.message, true); }
}

/* ============================= bills ============================= */
let BILLS = [], billFilter = { q: "", status: "", pay: "", from: "", to: "", page: 1 };
async function loadBills() {
  $("bills-list").innerHTML = skeleton(6);
  try { BILLS = await api("/orders?limit=200"); } catch (e) { $("bills-list").innerHTML = errBox(e.message, "loadBills"); return; }
  renderBills();
}
function renderBills() {
  const f = billFilter;
  const rows = BILLS.filter((o) => {
    if (f.status && o.status !== f.status) return false;
    if (f.pay && o.payment_status !== f.pay) return false;
    if (f.from && o.created_at.slice(0, 10) < f.from) return false;
    if (f.to && o.created_at.slice(0, 10) > f.to) return false;
    const q = f.q.toLowerCase();
    if (q && !(o.order_number.toLowerCase().includes(q) || (o.customer_name || "").toLowerCase().includes(q) || o.customer_phone.includes(q))) return false;
    return true;
  });
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  f.page = Math.min(f.page, pages);
  const page = rows.slice((f.page - 1) * PAGE, f.page * PAGE);
  if (!page.length) { $("bills-list").innerHTML = emptyBox("No bills match these filters.", "🧾"); $("bills-pager").innerHTML = ""; return; }
  $("bills-list").innerHTML = `
    <table class="tbl"><thead><tr><th>Invoice</th><th>Customer</th><th>Items</th><th>Total / due</th><th>Status</th><th>Actions</th></tr></thead>
    <tbody>${page.map((o) => billRowHtml(o, "tr")).join("")}</tbody></table>
    <div class="rowcards">${page.map((o) => billRowHtml(o, "card")).join("")}</div>`;
  $("bills-pager").innerHTML = pages > 1
    ? `<button class="btn sm ghost" ${f.page <= 1 ? "disabled" : ""} onclick="billFilter.page--;renderBills()">‹ Prev</button>
       <span class="muted">Page ${f.page} of ${pages}</span>
       <button class="btn sm ghost" ${f.page >= pages ? "disabled" : ""} onclick="billFilter.page++;renderBills()">Next ›</button>` : "";
}
function billRowHtml(o, kind) {
  const due = o.total_amount ? Number(o.total_amount) - Number(o.amount_paid) : null;
  if (kind === "tr") return `<tr>
    <td><b>${o.order_number}</b><div class="muted">${fmtDate(o.created_at)}</div></td>
    <td>${esc(o.customer_name || o.customer_phone)}<div class="muted">${esc(o.customer_phone)}</div></td>
    <td style="max-width:180px"><div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(itemsText(o.items))}</div></td>
    <td class="money">${o.total_amount ? money(o.total_amount) : "—"}${due > 0 ? `<div class="muted">due ${money(due)}</div>` : ""}</td>
    <td><span class="pill ${o.status}">${STATUS_LABEL[o.status]}</span> <span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></td>
    <td><div class="act">
      <button class="btn sm ghost" onclick="orderDetail('${o.order_number}')">👁</button>
      <button class="btn sm ghost" onclick="printReceiptFromOrder('${o.order_number}')">🖨</button>
      <button class="btn sm ghost" onclick="paymentModal('${o.order_number}')">Payment</button>
      <button class="btn sm ghost" onclick="editBillModal('${o.order_number}')">Edit</button>
      <button class="btn sm danger" onclick="deleteBillModal('${o.order_number}')">Delete</button>
    </div></td></tr>`;
  return `<div class="rowcard">
    <div class="r1"><b>${o.order_number}</b><span class="pill ${o.status}">${STATUS_LABEL[o.status]}</span></div>
    <div class="kv"><span>${esc(o.customer_name || o.customer_phone)}</span><span>${fmtDate(o.created_at)}</span></div>
    <div class="kv"><span>${o.total_amount ? money(o.total_amount) : "—"}</span><span class="pill ${o.payment_status}">${o.payment_status.toLowerCase()}</span></div>
    <div class="act"><button class="btn sm ghost" onclick="orderDetail('${o.order_number}')">Details</button>
    <button class="btn sm ghost" onclick="printReceiptFromOrder('${o.order_number}')">Print</button>
    <button class="btn sm ghost" onclick="paymentModal('${o.order_number}')">Payment</button>
    <button class="btn sm ghost" onclick="editBillModal('${o.order_number}')">Edit</button>
    <button class="btn sm danger" onclick="deleteBillModal('${o.order_number}')">Delete</button></div></div>`;
}

/* ---- bill edit / delete ---- */
function billByNumber(num) { return BILLS.find((b) => b.order_number === num); }

function editBillModal(num) {
  const o = billByNumber(num);
  if (!o) return;
  const items = (o.items || []).map((i) => `${i.qty || 1} x ${i.type || i.garment || i.service || ""}`).join("\n");
  openModal(`<h3>Edit bill — ${num}</h3>
    <p class="muted">${esc(o.customer_name || o.customer_phone)} · ${fmtDate(o.created_at)}</p>
    <div class="frm">
      <div class="setfield"><label for="eb-items">Items (ek line mein ek: "2 x shirt")</label>
        <textarea id="eb-items" rows="4">${esc(items)}</textarea></div>
      <div class="split2">
        <div class="setfield"><label for="eb-total">Total (₹)</label>
          <input id="eb-total" type="number" min="0" step="0.01" value="${o.total_amount || ""}">
          <small>Mila hua: ${money(o.amount_paid)} — wo yahan se nahi badalta, Payment se badalta hai.</small></div>
        <div class="setfield"><label for="eb-date">Delivery date</label>
          <input id="eb-date" type="date" value="${o.expected_delivery || ""}"></div>
      </div>
      <div class="setfield"><label for="eb-notes">Internal note (customer ko kabhi nahi jaata)</label>
        <input id="eb-notes" value="${esc(o.notes || "")}"></div>
      <small class="fielderr" id="eb-err"></small>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="eb-save">Save changes</button>
    </div>`);
  $("eb-save").onclick = (e) => busy(e.target, async () => {
    $("eb-err").textContent = "";
    const total = $("eb-total").value === "" ? null : parseFloat($("eb-total").value);
    if (total !== null && !(total >= 0)) { $("eb-err").textContent = "Total sahi nahi hai."; return; }
    const items = $("eb-items").value.split("\n").map((l) => l.trim()).filter(Boolean)
      .map((l) => {
        const m = l.match(/^(\d+)\s*[x×]?\s*(.+)$/i);
        return m ? { qty: parseInt(m[1]), type: m[2].trim() } : { qty: 1, type: l };
      });
    await api(`/orders/${num}`, { method: "PUT", body: {
      items, total_amount: total, expected_delivery: $("eb-date").value || null,
      notes: $("eb-notes").value, edited_by: "dashboard",
    }});
    closeModal(); toast("Bill updated"); loadBills(); loadDashboard();
  });
}

function deleteBillModal(num) {
  const o = billByNumber(num);
  if (!o) return;
  const paid = Number(o.amount_paid || 0);
  openModal(`<h3>Delete bill ${num}?</h3>
    <p class="muted">${esc(o.customer_name || o.customer_phone)} · ${o.total_amount ? money(o.total_amount) : "—"}</p>
    <p class="muted">Bill, uska status history${paid > 0 ? ` aur ${money(paid)} ka payment record` : ""} — sab hamesha ke liye chala jayega. Reports bhi badlengi.</p>
    <div class="frm"><div class="setfield">
      <label for="db-confirm">Confirm karne ke liye <b>${num}</b> type karo</label>
      <input id="db-confirm" placeholder="${num}">
      <small class="fielderr" id="db-err"></small>
    </div></div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn danger" id="db-go">Delete bill</button>
    </div>`);
  $("db-go").onclick = (e) => busy(e.target, async () => {
    if ($("db-confirm").value.trim().toUpperCase() !== num.toUpperCase()) {
      $("db-err").textContent = `Poora number type karo: ${num}`;
      return;
    }
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
async function loadCustomers(quiet = false) {
  if (!quiet) $("cust-list").innerHTML = skeleton(6);
  try { CUSTOMERS_CACHE = await api("/admin/api/customers"); } catch (e) { if (!quiet) $("cust-list").innerHTML = errBox(e.message, "loadCustomers"); return; }
  if (!quiet || CURRENT === "customers") renderCustomers();
}
function renderCustomers() {
  if (!$("cust-list")) return;
  const q = ($("cust-search").value || "").toLowerCase();
  const rows = (CUSTOMERS_CACHE || [])
    .filter((c) => !q || (c.name || "").toLowerCase().includes(q) || c.phone.includes(q))
    .sort((a, b) => Number(b.outstanding) - Number(a.outstanding));
  if (!rows.length) { $("cust-list").innerHTML = emptyBox("No customers yet — they appear after their first bill or message.", "👥"); return; }
  $("cust-list").innerHTML = `
    <table class="tbl"><thead><tr><th>Customer</th><th>Orders</th><th>Business</th><th>Paid</th><th>Outstanding</th><th>Last seen</th><th>Actions</th></tr></thead>
    <tbody>${rows.map((c) => `
      <tr><td>${esc(c.name || "—")}${c.opted_out ? ' <span class="tag">opted out</span>' : ""}<div class="muted">${c.phone}</div></td>
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
      <div class="rowcard"><div class="r1"><b>${esc(c.name || c.phone)}</b><span class="money" style="color:${Number(c.outstanding) > 0 ? "var(--danger)" : "var(--ok)"}">${money(c.outstanding)}</span></div>
      <div class="kv"><span>${c.phone}</span><span>${c.total_orders} orders</span></div>
      <div class="kv"><span>Business ${money(c.business)}</span><span>Paid ${money(c.paid)}</span></div>
      <div class="act">${Number(c.outstanding) > 0 ? `<button class="btn sm" onclick="sendReminder('${c.phone}','${c.outstanding}')">Remind</button>` : ""}
      <button class="btn sm ghost" onclick="jumpChat('${c.phone}')">Chat</button>
      <button class="btn sm ghost" onclick="editCustomerModal('${c.phone}')">Edit</button>
      <button class="btn sm danger" onclick="deleteCustomerModal('${c.phone}')">Delete</button></div></div>`).join("")}</div>`;
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
        <input id="ec-phone" inputmode="numeric" value="${esc(digits)}">
        <small>Number badalne par unki puri chat aur bills isi naye number se judenge.</small>
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
    if (ph.length !== 10) { $("ec-phone-err").textContent = "Phone 10 digit ka hona chahiye."; return; }
    await api(`/admin/api/customers/${encodeURIComponent(phone)}`, { method: "PUT", body: {
      name: $("ec-name").value, phone: ph, address: $("ec-addr").value,
    }});
    closeModal(); toast("Customer updated"); loadCustomers();
  });
}

function deleteCustomerModal(phone) {
  const c = customerByPhone(phone);
  if (!c) return;
  const label = c.name || c.phone;
  const n = Number(c.total_orders || 0);
  openModal(`<h3>Delete ${esc(label)}?</h3>
    <p class="muted">${c.phone}</p>
    <p class="muted">${n
      ? `Unke <b>${n} bill</b>, payments, aur poori chat history bhi delete ho jayegi. Reports ke numbers badal jayenge.`
      : "Inka koi bill nahi hai. Chat history delete ho jayegi."}</p>
    <div class="frm"><div class="setfield">
      <label for="dc-confirm">Confirm karne ke liye <b>${esc(label)}</b> type karo</label>
      <input id="dc-confirm" placeholder="${esc(label)}">
      <small class="fielderr" id="dc-err"></small>
    </div></div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn danger" id="dc-go">Delete customer</button>
    </div>`);
  $("dc-go").onclick = (e) => busy(e.target, async () => {
    if ($("dc-confirm").value.trim() !== label) {
      $("dc-err").textContent = `Bilkul aisa type karo: ${label}`;
      return;
    }
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
    ? `${u.calls_left_today} aur bache hain aaj`
    : "koi limit set nahi";
  const budgetNote = u.monthly_budget_usd
    ? `budget ${usd(u.monthly_budget_usd)}` : "budget set nahi";

  $("usage-kpis").innerHTML =
    kpi("Aaj ke AI calls", u.today.calls, capNote, "", "⚡", "orange") +
    kpi("Aaj ke tokens", kTok(u.today.input_tokens + u.today.output_tokens),
        `in ${kTok(u.today.input_tokens)} · out ${kTok(u.today.output_tokens)}`, "", "🔤", "blue") +
    kpi("Is mahine kharch", u.all_free ? "₹0 (free)" : usd(u.month.cost_usd),
        u.all_free ? "free tier par ho" : budgetNote, "", "💰", "green") +
    kpi("Is raftaar se mahina", u.all_free ? "₹0" : usd(u.projected_month_usd),
        `${u.month.calls} calls ab tak`, "", "📈", "purple");

  // simple bar chart — no library, scales to the busiest day
  const s = u.series || [];
  const max = Math.max(1, ...s.map((d) => d.calls));
  $("usage-chart").innerHTML = s.length
    ? `<div style="display:flex;align-items:flex-end;gap:3px;height:130px">` +
      s.map((d) => `<div title="${d.date}: ${d.calls} calls, ${kTok(d.tokens)} tokens"
        style="flex:1;min-width:0;background:var(--g-blue);border-radius:3px 3px 0 0;
        height:${Math.max(3, (d.calls / max) * 100)}%"></div>`).join("") + `</div>
      <div class="muted" style="display:flex;justify-content:space-between;margin-top:6px">
        <span>${s[0].date.slice(5)}</span><span>aaj</span></div>`
    : emptyBox("Abhi tak koi AI call record nahi hui.", "📊");

  const rows = u.by_purpose || [];
  const totTok = rows.reduce((a, r) => a + r.tokens, 0) || 1;
  $("usage-purpose").innerHTML = rows.length
    ? rows.map((r) => `
      <div class="sumrow"><span>${esc(PURPOSE_LABEL[r.purpose] || r.purpose)}</span>
        <span>${r.calls} calls · ${kTok(r.tokens)}${u.all_free ? "" : " · " + usd(r.cost_usd)}</span></div>
      <div style="height:5px;background:var(--n100);border-radius:3px;margin-bottom:8px">
        <div style="height:5px;width:${Math.round((r.tokens / totTok) * 100)}%;background:var(--g-orange);border-radius:3px"></div>
      </div>`).join("")
    : `<p class="muted">Is mahine abhi kuch nahi.</p>`;

  $("usage-models").innerHTML = `
    <p class="muted">Provider: <b>${esc(u.provider)}</b> · ${esc(u.models.smart)} / ${esc(u.models.cheap)}</p>
    <table class="tbl"><thead><tr><th>Model</th><th>Calls</th><th>Input</th><th>Output</th><th>Kharch</th></tr></thead>
    <tbody>${(u.month.by_model || []).map((m) => `<tr>
      <td><b>${esc(m.model)}</b></td><td>${m.calls}</td>
      <td>${kTok(m.input_tokens)}</td><td>${kTok(m.output_tokens)}</td>
      <td class="money">${m.priced ? usd(m.cost_usd) : '<span class="muted">free</span>'}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">Is mahine koi call nahi.</td></tr>`}</tbody></table>
    <p class="muted" style="margin-top:10px">Rate card aur limit Settings mein badal sakte ho — puraana hisaab bhi naye rate se dobara jud jayega.</p>`;
}

const PURPOSE_LABEL = {
  reply: "Customer ko jawab", intent: "Message samajhna", extract: "Bill/command padhna",
  vision: "Photo se bill", query: "Aapke sawal", marketing: "Campaign likhna",
  social: "Daily poster", other: "Baaki",
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
    kpi("Pending kaam", open.length, "", "", "📋", "orange") +
    kpi("Atke hue", stuck.length, "6 ghante+ ya escalate hua", "", "🚨", "red") +
    kpi("Aaj complete", doneToday.length, "", "", "✅", "green");
}

function renderTaskChips() {
  const counts = {
    OPEN: TASKS.filter((t) => t.status === "OPEN").length,
    DONE: TASKS.filter((t) => t.status === "DONE").length,
    ALL: TASKS.length,
  };
  $("task-chips").innerHTML = [["OPEN", "Pending"], ["DONE", "Ho gaye"], ["ALL", "Sab"]]
    .map(([v, label]) => `<span class="chip ${taskFilter === v ? "on" : ""}"
      onclick="taskFilter='${v}';renderTaskChips();renderTasks()">${label} <b>${counts[v]}</b></span>`)
    .join("");
}

function renderTasks() {
  const rows = TASKS.filter((t) => taskFilter === "ALL" || t.status === taskFilter);
  if (!rows.length) {
    $("task-list").innerHTML = emptyBox(
      taskFilter === "OPEN" ? "Koi kaam pending nahi 🎉" : "Yahan kuch nahi hai.", "✅");
    return;
  }
  $("task-list").innerHTML = `<div class="stafflist">` + rows.map((t) => {
    const open = t.status === "OPEN";
    const late = open && (t.escalated || t.age_hours >= 6);
    return `
    <div class="staffrow ${open ? "" : "off"}" style="${late ? "border-left:3px solid var(--danger)" : ""}">
      <div class="who">
        <div class="nm">${t.urgent ? "🔴 " : ""}${esc(t.title)}</div>
        <div class="meta">
          <span class="badge">${t.code}</span>
          <span class="badge role">${t.staff ? esc(t.staff) : "kisi ko nahi diya"}</span>
          ${t.order_number ? `<span class="badge">${t.order_number}</span>` : ""}
          <span class="statuspill ${open ? "off" : "on"}">${
            t.status === "OPEN" ? `${t.age_hours}h pending` : t.status === "DONE" ? "Ho gaya" : "Cancel"}</span>
          ${t.ping_count ? `<span class="badge">${t.ping_count}x yaad dilaya</span>` : ""}
          ${t.escalated ? `<span class="statuspill off" style="color:var(--danger)">aapko bataya</span>` : ""}
        </div>
        ${t.reply ? `<div class="muted" style="margin-top:6px">💬 ${esc(t.staff || "unhone")}: ${esc(t.reply)}</div>` : ""}
      </div>
      <div class="acts">
        ${open ? `
          <button class="btn sm ghost" onclick="pingTask('${t.code}')">Poochho</button>
          <button class="btn sm" onclick="doneTask('${t.code}')">Ho gaya</button>
          <button class="btn sm ghost" onclick="cancelTask('${t.code}')">Cancel</button>` : ""}
      </div>
    </div>`;
  }).join("") + `</div>`;
}

async function pingTask(code) {
  try { const r = await api(`/admin/api/tasks/${code}/ping`, { method: "POST" }); toast(r.detail); loadTasks(); }
  catch (e) { toast(e.message, true); }
}
async function doneTask(code) {
  try { await api(`/admin/api/tasks/${code}/done`, { method: "POST" }); toast(`${code} band`); loadTasks(); }
  catch (e) { toast(e.message, true); }
}
function cancelTask(code) {
  confirmDialog(`${code} cancel kar dein? Staff ko aur reminder nahi jayenge.`, async () => {
    try { await api(`/admin/api/tasks/${code}/cancel`, { method: "POST" }); toast(`${code} cancel`); loadTasks(); }
    catch (e) { toast(e.message, true); }
  });
}
async function pingAllTasks(btn) {
  await busy(btn, async () => {
    const r = await api("/admin/api/jobs/task-followups", { method: "POST" });
    toast(r.sent ? `${r.sent} logon ko yaad dilaya` : "Abhi kisi ko poochne ki zarurat nahi thi");
    loadTasks();
  });
}

function newTaskModal() {
  const opts = (STAFF || []).filter((s) => s.is_active)
    .map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
  openModal(`<h3>Naya kaam</h3>
    <div class="frm">
      <div class="setfield"><label for="nt-title">Kya karna hai</label>
        <input id="nt-title" placeholder="Sharma ji ka order aaj hi deliver karna hai" autofocus>
        <small>Seedhe unse baat karte hue likho — yahi message unke WhatsApp par jayega.</small>
        <small class="fielderr" id="nt-err"></small></div>
      <div class="split2">
        <div class="setfield"><label for="nt-staff">Kisko</label>
          <select id="nt-staff">${opts || '<option value="">koi staff nahi</option>'}</select></div>
        <div class="setfield"><label for="nt-order">Order (optional)</label>
          <input id="nt-order" placeholder="KK-20260805-01"></div>
      </div>
      <div class="setfield"><label for="nt-urgent">Urgent?</label>
        <select id="nt-urgent"><option value="false">Normal</option><option value="true">Urgent — jaldi poochhunga</option></select></div>
    </div>
    <div class="btnrow">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" id="nt-go">Bhejo aur track karo</button>
    </div>`);
  $("nt-go").onclick = (e) => busy(e.target, async () => {
    const title = $("nt-title").value.trim();
    if (title.length < 2) { $("nt-err").textContent = "Kaam likhna zaroori hai."; return; }
    const r = await api("/admin/api/tasks", { method: "POST", body: {
      title, staff: $("nt-staff").value || null,
      order_number: $("nt-order").value.trim() || null,
      urgent: $("nt-urgent").value === "true",
    }});
    closeModal(); toast(`${r.code} bhej diya`); loadTasks();
  });
}

/* ============================= expenses ============================= */
const EXP_CATS = ["Detergent", "Electricity", "Rent", "Salary", "Transport", "Maintenance", "Other"];
let EXPENSES = [];
async function loadExpenses() {
  $("exp-list").innerHTML = skeleton(4);
  $("exp-cat").innerHTML = EXP_CATS.map((c) => `<option>${c}</option>`).join("");
  $("exp-date").value = new Date().toISOString().slice(0, 10);
  try {
    [EXPENSES, SUMMARY] = await Promise.all([api("/admin/api/expenses"), api("/admin/api/reports/summary")]);
  } catch (e) { $("exp-list").innerHTML = errBox(e.message, "loadExpenses"); return; }
  const t = SUMMARY.today || {}, m = SUMMARY.month || {};
  $("exp-kpis").innerHTML =
    kpi("Expenses today", money(t.expenses || 0), "", "", "📅", "amber") +
    kpi("Expenses this month", money(m.expenses || 0), "", "", "🗓️", "pink") +
    kpi("Profit this month", money(m.profit || 0), "revenue − expenses", "go('reports')", "💰", "green");
  renderExpenses(); renderExpChart();
}
function renderExpenses() {
  if (!EXPENSES.length) { $("exp-list").innerHTML = emptyBox("No expenses recorded yet — add your first one above.", "💸"); return; }
  $("exp-list").innerHTML = `
    <table class="tbl"><thead><tr><th>Date</th><th>Category</th><th>Amount</th><th>Description</th><th></th></tr></thead>
    <tbody>${EXPENSES.map((e) => `
      <tr><td>${fmtDate(e.spent_on)}</td><td>${esc(e.category)}</td><td class="money">${money(e.amount)}</td>
      <td class="muted">${esc(e.description || "")}</td>
      <td><button class="btn sm danger" onclick="delExpense('${e.id}')">✕</button></td></tr>`).join("")}
    </tbody></table>
    <div class="rowcards">${EXPENSES.map((e) => `
      <div class="rowcard"><div class="r1"><b>${esc(e.category)}</b><span class="money">${money(e.amount)}</span></div>
      <div class="kv"><span>${fmtDate(e.spent_on)}</span><span>${esc(e.description || "")}</span></div>
      <div class="act"><button class="btn sm danger" onclick="delExpense('${e.id}')">Delete</button></div></div>`).join("")}</div>`;
}
async function saveExpense(btn) {
  await busy(btn, async () => {
    const amt = parseFloat($("exp-amt").value);
    if (!(amt > 0)) throw new Error("Amount must be greater than 0");
    await api("/admin/api/expenses", { method: "POST", body: { category: $("exp-cat").value, amount: amt, spent_on: $("exp-date").value, description: $("exp-desc").value.trim() || null } });
    $("exp-amt").value = ""; $("exp-desc").value = "";
    toast("Expense saved"); loadExpenses();
  });
}
function delExpense(id) {
  confirmDialog("Delete this expense? This cannot be undone.", async () => {
    try { await api(`/admin/api/expenses/${id}`, { method: "DELETE" }); toast(T.deleted); loadExpenses(); }
    catch (e) { toast(e.message, true); }
  });
}
const PALETTE = ["#f97316", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0e7490", "#dc2626", "#78716c"];
function donutHtml(pairs, elLegend) {
  const total = pairs.reduce((a, [, v]) => a + v, 0) || 1;
  let acc = 0;
  const stops = pairs.map(([k, v], i) => {
    const from = (acc / total) * 360; acc += v;
    return `${PALETTE[i % PALETTE.length]} ${from}deg ${(acc / total) * 360}deg`;
  });
  const legend = pairs.map(([k, v], i) => `<div><span class="sw" style="background:${PALETTE[i % PALETTE.length]}"></span>${esc(k)} — <b>${typeof v === "number" && v > 999 ? money(v) : v}</b></div>`).join("");
  return [`<div class="donut" style="background:conic-gradient(${stops.join(",")})"></div>`, legend];
}
function renderExpChart() {
  const byCat = {};
  EXPENSES.forEach((e) => { byCat[e.category] = (byCat[e.category] || 0) + Number(e.amount); });
  const pairs = Object.entries(byCat).sort((a, b) => b[1] - a[1]);
  if (!pairs.length) { $("exp-chart").innerHTML = ""; return; }
  const [donut, legend] = donutHtml(pairs);
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
  const statusPairs = Object.entries(DASH.counts.by_status || {}).map(([k, v]) => [STATUS_LABEL[k] || k, v]);
  const [sd, sl] = statusPairs.length ? donutHtml(statusPairs) : ["", ""];
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
        ${top.map((c) => `<div class="sumrow"><span>${esc(c.name || c.phone)}</span><span class="money">${money(c.business)}</span></div>`).join("") || emptyBox(T.noData)}
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
    .map(([k, v]) => `${SEGMENT_LABEL[k] || k}: <b>${v}</b>`).join(" · ") || "koi segment nahi";
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
        <p class="muted" style="margin-top:8px">WhatsApp se train: <b>test customer</b> → sawal → <b>sikhao: sahi jawaab</b> → <b>test band</b></p>
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
        <p class="muted" style="margin-top:8px">WhatsApp se: <b>test marketing</b> (preview) · <b>social bhejo</b> (aaj ka poster) · <b>campaign nahi</b> (brake)</p>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <b>🩺 System health</b>
      <div class="filters" style="margin-top:8px">
        <span class="tag">LLM: ${esc(d.health.llm_provider)}</span>
        <span class="tag">Public URL: ${d.health.public_url_set ? "✅ set" : "❌ missing"}</span>
        <span class="tag">Standup: ${d.health.standup_hour}:00 IST</span>
        <span class="tag">Turnaround: ${d.health.turnaround_days} din</span>
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
function renderDocs(docs) {
  $("doc-list").innerHTML = docs.length ? docs.map((d) => `
    <div class="sumrow"><span>📄 <b>${esc(d.document)}</b> <span class="tag">${d.chunks} parts</span>
      <span class="muted">${fmtWhen(d.uploaded_at)}</span></span>
      <button class="btn sm danger" onclick="delDoc('${esc(d.document)}')">✕</button></div>`).join("")
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
function delDoc(name) {
  confirmDialog(`Remove "${name}" from the agent's knowledge?`, async () => {
    try { await api(`/admin/api/training/docs/${encodeURIComponent(name)}`, { method: "DELETE" }); toast(T.deleted); loadTraining(); }
    catch (e) { toast(e.message, true); }
  });
}
function renderFaqs(faqs) {
  $("faq-list").innerHTML = faqs.length ? faqs.map((f) => `
    <div class="sumrow" style="align-items:flex-start;gap:8px">
      <span style="flex:1"><b>Q:</b> ${esc(f.question)}<br><b>A:</b> ${esc(f.answer)} <span class="tag">${f.audience}</span></span>
      <button class="btn sm danger" onclick="delFaq('${f.id}')">✕</button></div>`).join("")
    : `<p class="muted">No FAQ entries yet — add shop timings, prices policy, delivery areas…</p>`;
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

/* ============================= activity ============================= */
async function loadActivity() {
  $("act-list").innerHTML = skeleton(6);
  try {
    const role = $("act-role").value;
    const rows = await api("/admin/api/activity" + (role ? `?role=${role}` : ""));
    if (!rows.length) { $("act-list").innerHTML = emptyBox("No agent activity yet.", "🤖"); return; }
    $("act-list").innerHTML = rows.map((r) => `
      <div class="sumrow" style="align-items:flex-start;border-bottom:1px solid var(--n100);padding:8px 0">
        <span style="flex:1"><b>${esc(r.action)}</b> <span class="tag">${r.role}</span> ${r.actor ? `<span class="muted">${esc(r.actor)}</span>` : ""}
          ${r.args ? `<div class="muted" style="font-size:11.5px">${esc(JSON.stringify(r.args)).slice(0, 160)}</div>` : ""}
          ${r.result ? `<div style="font-size:12px">${esc(r.result).slice(0, 200)}</div>` : ""}</span>
        <span class="muted" style="flex-shrink:0">${fmtWhen(r.at)}</span></div>`).join("");
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
    <table class="tbl" style="min-width:${180 + services.length * 110}px"><thead><tr>
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
  toast("Ab kisi bhi service ke cell mein rate likho — save ho jayega");
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
const ROLE_LABEL = { WASHER: "Washer", DELIVERY: "Delivery" };

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
          ${s.active_orders ? `<span class="badge">${s.active_orders} active order${s.active_orders > 1 ? "s" : ""}</span>` : ""}
        </div>
      </div>
      <div class="acts">
        <button class="btn sm ghost" onclick="editStaffModal('${s.id}')">Edit</button>
        ${s.is_active
          ? `<button class="btn sm ghost" onclick="deactivateStaff('${s.id}')">Deactivate</button>`
          : `<button class="btn sm ghost" onclick="updStaff('${s.id}', {is_active: true})">Reactivate</button>`}
        <button class="btn sm danger" onclick="deleteStaffModal('${s.id}')">Delete</button>
      </div>
    </div>`).join("") + `</div>`;
}

function staffById(id) { return STAFF.find((s) => s.id === id); }

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
    await api(`/admin/api/staff/${id}`, { method: "PUT", body: { name, phone, role: $("es-role").value } });
    closeModal(); toast("Staff updated"); loadSettings();
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
    await api("/admin/api/staff", { method: "POST", body: { name, phone, role: $("sf-role").value } });
    toast(`${name} added`);
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
const EMOJIS = ["😀","😄","😊","🙏","👍","👌","✅","❤️","🎉","😅","😂","🤝","🧺","👔","🧼","⏰","📅","💰","🛵","⚠️","❓","🌟"];
async function loadThreads() {
  try { THREADS = await api("/admin/api/inbox/threads"); } catch (e) { $("th-list").innerHTML = errBox(e.message, "loadThreads"); return; }
  loadTplPreview();  // fire-and-forget: makes template bubbles readable
  renderThreads(); updateUnreadBadge();
  if (OPEN_PHONE) openThread(OPEN_PHONE, true, false);
  clearTimeout(inboxTimer);
  if (CURRENT === "inbox") inboxTimer = setTimeout(loadThreads, 12000);
}
const avatar = (n) => `<div class="avatar">${esc((n || "?").trim()[0] || "?").toUpperCase()}</div>`;
const seenKey = (p) => "kk_seen_" + p;
const isUnread = (t) =>
  t.last_direction === "INBOUND" && t.last_at > (localStorage.getItem(seenKey(t.phone)) || "");
function updateUnreadBadge() {
  const n = THREADS.filter(isUnread).length;
  const b = $("unread-badge");
  if (b) { b.textContent = n; b.classList.toggle("show", n > 0); }
}
function renderThreads() {
  const q = ($("th-search").value || "").toLowerCase();
  const rows = THREADS.filter((t) => !q || t.name.toLowerCase().includes(q) || t.phone.includes(q));
  $("th-list").innerHTML = rows.map((t) => {
    const chip = t.kind === "staff" ? '<span class="staff-chip">staff</span>' : t.kind === "admin" ? '<span class="staff-chip">👑 you</span>' : "";
    return `<div class="thread-item ${t.phone === OPEN_PHONE ? "on" : ""}"
      onclick="openThread('${t.phone}')" ontouchstart="thTouchStart(event,'${t.phone}')" ontouchend="thTouchEnd(event,'${t.phone}')">
      <div style="display:flex;gap:10px;align-items:center">
        ${avatar(t.name)}
        <div style="flex:1;min-width:0">
          <div class="nm"><span>${esc(t.name)}${chip}</span><span class="t">${fmtWhen(t.last_at)}</span></div>
          <div class="pv">${isUnread(t) ? '<b style="color:#00A884">● </b>' : ""}${t.last_direction === "OUTBOUND" ? "✓✓ " : ""}${esc(t.last_text)}</div>
        </div>
      </div></div>`;
  }).join("") || `<div class="thread-item">${T.noData}</div>`;
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
  openModal(`<h3>${esc(t.name || phone)}</h3>
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
  if (push && location.hash.startsWith("#inbox/")) {
    NAVIGATING = true; location.hash = "inbox"; setTimeout(() => (NAVIGATING = false), 0);
  }
}
function scrollChatBottom() {
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
  $("newmsg-pill").classList.remove("show");
}

async function openThread(phone, silent = false, push = true) {
  OPEN_PHONE = phone;
  localStorage.setItem(seenKey(phone), new Date().toISOString());
  updateUnreadBadge();
  if (window.innerWidth <= 767) {
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
  $("chat-head").innerHTML = `
    <button class="chat-back" onclick="closeThreadMobile()" aria-label="Back">←</button>
    ${avatar(d.name)}
    <div style="flex:1;min-width:0"><b>${esc(d.name)}</b>
      <div class="muted">${d.phone} · ${d.kind === "staff" ? "Staff 🧑‍🔧" : d.kind === "admin" ? "You 👑" : "Customer"}</div></div>
    <span class="winchip ${d.window.open ? "open" : "closed"}">${d.window.open ? "window open" : "window closed"}</span>
    ${d.kind === "customer" ? `<button class="btn sm ghost" id="agent-pause-btn" onclick="toggleAgentPause()">🤖 Agent: …</button>` : ""}`;
  refreshPauseBtn();
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  let lastDay = "";
  const html = (d.messages || []).map((m) => {
    let chip = "";
    const day = new Date(m.at).toDateString();
    if (day !== lastDay) {
      lastDay = day;
      const today = new Date().toDateString() === day;
      chip = `<div class="daychip">${today ? "today" : fmtDate(m.at)}</div>`;
    }
    let body = esc(m.text || "");
    const raw = m.text || "";
    const img = raw.match(/^\[image:(\/admin\/media\/[\w.\-]+)\]\s*(.*)$/s);
    const tpl = raw.match(/^\[template:([\w]+)\]\s*(.*)$/s);
    const btn = raw.match(/^\[button:([^\]]+)\]\s*(.*)$/s);
    const med = raw.match(/^\[(audio|voice|video|document)\:(\/admin\/media\/[\w.\-]+)\]\s*(.*)$/s);
    const loc = raw.match(/^\[location:([-\d.]+),([-\d.]+)\]\s*(.*)$/s);
    if (img) {
      body = `<img src="${img[1]}?key=${encodeURIComponent(KEY)}" loading="lazy" width="280" height="210">${esc(img[2] || "")}`;
    } else if (med) {
      // play/open it right here, like WhatsApp — not a dead "[document]" tag
      const url = `${med[2]}?key=${encodeURIComponent(KEY)}`;
      const label = esc(med[3] || "");
      if (med[1] === "audio" || med[1] === "voice") {
        body = `<audio controls preload="none" src="${url}" style="max-width:250px"></audio>${label}`;
      } else if (med[1] === "video") {
        body = `<video controls preload="metadata" src="${url}" width="260" style="border-radius:8px"></video>${label}`;
      } else {
        body = `<a class="filechip" href="${url}" target="_blank" rel="noopener">📄 ${label || "File kholo"}</a>`;
      }
    } else if (loc) {
      body = `<a class="filechip" target="_blank" rel="noopener"
        href="https://www.google.com/maps/search/?api=1&query=${loc[1]},${loc[2]}">📍 ${esc(loc[3] || "Location")}</a>`;
    } else if (tpl) {
      // a template log line is unreadable as "[template:kk_thankyou_rating]" —
      // show the actual text the customer received, with its buttons
      const t = TPL_PREVIEW[tpl[1]];
      body = `<div class="tplmsg">${t ? esc(t.body) : esc(tpl[2] || tpl[1])}
        <div class="tplname">📑 ${esc(tpl[1])}</div>
        ${(t && t.buttons || []).map((b) => `<div class="tplbtn">${esc(b)}</div>`).join("")}</div>`;
    } else if (btn) {
      body = `<span class="tapped">👆 ${esc(btn[1])}</span>`;
    }
    return `${chip}<div class="bubble ${m.direction === "INBOUND" ? "in" : "out"}">${body}
      <span class="bt">${fmtWhen(m.at)}${m.direction === "OUTBOUND" ? " · " + (m.sent_by || "bot") : ""}</span></div>`;
  }).join("") || emptyBox("Chat appears here", "💬");
  log.innerHTML = html;
  sessionStorage.setItem("kk_thread_" + phone, html);
  const newCount = (d.messages || []).length;
  if (nearBottom || !silent) scrollChatBottom();
  else if (newCount > prevCount) $("newmsg-pill").classList.add("show");
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
async function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || !OPEN_PHONE) return;
  input.value = "";
  if (!navigator.onLine) {
    saveOutbox([...outbox(), { phone: OPEN_PHONE, text }]);
    toast("Offline — message queue mein hai, net aate hi jayega");
    return;
  }
  try {
    await api("/admin/api/inbox/send", { method: "POST", body: { phone: OPEN_PHONE, text } });
    openThread(OPEN_PHONE, true, false);
  } catch (e) { toast(e.message, true); input.value = text; }
}
/* new chat with any number */
function newChatModal() {
  openModal(`<h3>➕ New chat</h3>
    <div class="frm">
      <div><label>Mobile number</label><input id="nc-phone" placeholder="98765 43210" autofocus></div>
      <div><label>Name (optional)</label><input id="nc-name" placeholder="Customer name"></div>
    </div>
    <p class="muted">Naya number ho to pehla message sirf <b>approved template</b> se ja sakta hai (WhatsApp ka niyam) — chat khulne par 📑 button use karo.</p>
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
  if (!OPEN_PHONE) { toast("Pehle koi chat kholo", true); return; }
  openModal(`<h3>📑 Template bhejo</h3><div class="frm" id="tps-body">${skeleton(2)}</div>`);
  let note = "";
  try {
    TPL_CACHE = (await api("/admin/api/templates")).filter((t) => t.status === "APPROVED");
    if (!TPL_CACHE.length) note = "⚠️ Meta par abhi koi template APPROVED nahi — bhejne par fail ho sakta hai.";
    if (!TPL_CACHE.length) TPL_CACHE = await api("/admin/api/templates/registry");
  } catch (e) {
    // Meta down/blocked -> local registry, honest warning
    try { TPL_CACHE = await api("/admin/api/templates/registry"); } catch (e2) { TPL_CACHE = []; }
    note = "⚠️ Meta API abhi unreachable — list local registry se hai, send try hoga par fail ho sakta hai.";
  }
  if (!TPL_CACHE.length) {
    $("tps-body").innerHTML = `<p class="muted">Koi template nahi mila.</p>
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
    if (params.some((p) => !p)) throw new Error("Sab variables bharo");
    await api("/admin/api/inbox/send-template", { method: "POST",
      body: { phone: OPEN_PHONE, template_name: t.name, params } });
    closeModal(); toast(T.sent); openThread(OPEN_PHONE, true, false);
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
  openModal(`<h3>📥 Bulk import numbers</h3>
    <div class="frm">
      <textarea id="bi-text" rows="8" placeholder="Ek line mein ek number:\n9876543210\nSharma ji, 9812345678\nSeema Mam, 98111 22333"></textarea>
    </div>
    <p class="muted">Format: sirf number, ya 'naam, number'. Jo pehle se hain wo skip honge. Yaad rahe — marketing message sirf opted-in logon ko jayega.</p>
    <div class="btnrow"><button class="btn ghost" onclick="closeModal()">Cancel</button>
    <button class="btn" id="bi-go">Import</button></div>`);
  $("bi-go").onclick = (e) => busy(e.target, async () => {
    const r = await api("/admin/api/customers/bulk", { method: "POST", body: { text: $("bi-text").value } });
    closeModal();
    toast(`${r.added} naye jude, ${r.skipped_existing} pehle se the` + (r.invalid.length ? `, ${r.invalid.length} galat` : ""));
    loadCustomers();
  });
}

function toggleEmojis() { $("emoji-pal").classList.toggle("open"); }
function addEmoji(e) { $("chat-input").value += e; $("chat-input").focus(); }
async function sendMedia(input) {
  if (!input.files || !input.files[0] || !OPEN_PHONE) return;
  const fd = new FormData();
  fd.append("phone", OPEN_PHONE);
  fd.append("file", input.files[0]);
  fd.append("caption", "");
  try {
    await api("/admin/api/inbox/send-media", { method: "POST", body: fd });
    toast(T.sent); openThread(OPEN_PHONE, true);
  } catch (e) { toast(e.message, true); }
  input.value = "";
}

/* ============================= init ============================= */
window.addEventListener("DOMContentLoaded", () => {
  // dev probe: ?probe=1 writes overflow offenders into the <title>
  if (qs.get("probe")) {  // qs captured before replaceState strips the query
    const report = () => {
      const wide = [...document.querySelectorAll("body *")]
        .filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1
          && getComputedStyle(e).position !== "fixed")
        .slice(0, 6)
        .map((e) => `${e.tagName}.${String(e.className).slice(0, 24)}=${Math.round(e.getBoundingClientRect().right)}`);
      document.title = `PROBE vw=${window.innerWidth} sw=${document.documentElement.scrollWidth} :: ${wide.join(" ; ") || "none"}`;
    };
    report();
    setTimeout(report, 1200);
    setTimeout(report, 3000);
  }
  $("emoji-pal").innerHTML = EMOJIS.map((e) => `<span onclick="addEmoji('${e}')">${e}</span>`).join("");
  if (!KEY) { showLogin(); }
  const h = (location.hash || "#dashboard").slice(1);
  if (h.startsWith("inbox/")) {
    go("inbox", false);
    setTimeout(() => openThread(decodeURIComponent(h.slice(6)), false, false), 300);
  } else if (h.startsWith("settings/")) {
    go("settings", false);
    stTab(h.slice(9));
  } else {
    go(h, false);
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
