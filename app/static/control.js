/* KwikKlin Control — vendor panel.
 *
 * Strict-CSP safe: no inline handlers/styles anywhere, everything wired
 * with addEventListener + event delegation (data-act attributes).
 * Auth: the master key never touches web storage — it is exchanged once
 * for an httpOnly cookie (POST /control/api/session); every later call is
 * just credentials:"same-origin".
 */
"use strict";

/* ---------- tiny i18n layer: one voice (English), swappable ------------- */
const STR = {
  en: {
    signedInAs: (l, lv) => `signed in as ${l} (${lv})`,
    signedOut: "not signed in",
    loadFail: "Could not load",
    retry: "Retry",
    empty: { tenants: ["🏪", "No tenants match", "Clear the filters or add your first client."],
             billing: ["💳", "No billing events yet", "Payments and reminders will show up here."],
             audit: ["🛰", "Nothing logged yet", "Actions you take will appear here."],
             keys: ["🔑", "No per-admin keys", "The .env master key is in use. Create scoped keys to rotate safely."] },
  },
};
const L = "en";
const t = (k) => STR[L][k];

/* ---------- dom helpers ------------------------------------------------- */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString("en-IN",
  { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");
const announce = (msg) => { $("live").textContent = msg; };

function toast(msg, kind = "ok") {
  const box = el("div", `toast toast-${kind}`, msg);
  $("toasts").appendChild(box);
  setTimeout(() => box.remove(), kind === "err" ? 7000 : 4000);
  announce(msg);
}

/* ---------- API (cookie session; no key in storage) --------------------- */
let LEVEL = "read";
const can = (need) => ({ read: 0, write: 1, danger: 2 })[LEVEL] >= ({ read: 0, write: 1, danger: 2 })[need];

async function api(path, opts = {}) {
  const init = Object.assign({ credentials: "same-origin", headers: {} }, opts);
  if (init.body && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { showSignin(); throw new Error("Session expired — sign in again"); }
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.status === 204 ? null : r.json();
}

/* Every mutating action goes through this: toast on both paths, never a
   silent failure, and the caller stays readable. */
async function act(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(okMsg, "ok");
    return out;
  } catch (e) {
    toast(e.message, "err");
    throw e;
  }
}

/* ---------- state / empty / skeleton renderers -------------------------- */
function skeletonRows(tbody, rows, cols) {
  tbody.replaceChildren();
  for (let i = 0; i < rows; i++) {
    const tr = el("tr"), td = el("td");
    td.colSpan = cols;
    td.appendChild(el("span", "skel skel-row"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}
function stateRow(tbody, cols, [ico, title, msg], action) {
  tbody.replaceChildren();
  const tr = el("tr"), td = el("td");
  td.colSpan = cols;
  const box = el("div", "state");
  box.appendChild(el("span", "ico", ico));
  box.appendChild(el("div", "title", title));
  box.appendChild(el("div", null, msg));
  if (action) {
    const b = el("button", "btn-primary", action.label);
    b.addEventListener("click", action.onClick);
    const wrap = el("div", "mt");
    wrap.appendChild(b);
    box.appendChild(wrap);
  }
  td.appendChild(box);
  tr.appendChild(td);
  tbody.appendChild(tr);
}
const errState = (tbody, cols, err, retry) =>
  stateRow(tbody, cols, ["⚠️", t("loadFail"), err.message], { label: t("retry"), onClick: retry });

/* ---------- URL-synced filter state (shareable views) ------------------- */
const T = { sort: "created_at", order: "desc", cursor: "", offset: 0, stack: [] };

function readUrl() {
  const p = new URLSearchParams(location.search);
  $("t-search").value = p.get("q") || "";
  $("t-status").value = p.get("status") || "";
  $("t-plan").value = p.get("plan") || "";
  $("show-deleted").checked = p.get("deleted") === "1";
  T.sort = p.get("sort") || "created_at";
  T.order = p.get("order") || "desc";
}
function writeUrl() {
  const p = new URLSearchParams();
  const q = $("t-search").value.trim(), st = $("t-status").value, pl = $("t-plan").value;
  if (q) p.set("q", q);
  if (st) p.set("status", st);
  if (pl) p.set("plan", pl);
  if ($("show-deleted").checked) p.set("deleted", "1");
  if (T.sort !== "created_at") p.set("sort", T.sort);
  if (T.order !== "desc") p.set("order", T.order);
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

/* ---------- KPIs (drill-down + comparison tooltip) ---------------------- */
const KPI_FILTER = { Trial: "trial", "Past due": "past_due", Active: "active" };

async function loadKpis() {
  const box = $("summary");
  if (!box.children.length) {
    for (let i = 0; i < 8; i++) box.appendChild(el("span", "skel card"));
  }
  let k;
  try { k = await api("/control/api/kpis"); }
  catch (e) { box.replaceChildren(el("div", "state", `${t("loadFail")}: ${e.message}`)); return; }
  const tr = k.trends || {};
  const since = tr._since ? `vs ${new Date(tr._since).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}` : "";
  const tiles = [
    ["Clients", k.clients, tr.clients], ["Active", k.active, tr.active],
    ["Trial", k.trial, null], ["Past due", k.past_due, tr.past_due],
    ["Churned", k.churned, null], ["New (mo)", k.new_this_month, null],
    ["MRR", inr(k.mrr_inr), tr.mrr_inr], ["ARR", inr(k.arr_inr), tr.mrr_inr],
  ];
  box.replaceChildren();
  for (const [label, value, trend] of tiles) {
    const filter = KPI_FILTER[label];
    const node = el(filter ? "button" : "div", "card");
    if (filter) {
      node.type = "button";
      node.dataset.filter = filter;
      node.setAttribute("aria-pressed", String($("t-status").value === filter));
      node.title = `Show only ${label.toLowerCase()} tenants`;
    }
    const b = el("b", null, String(value));
    if (trend != null) {
      const up = trend >= 0;
      // Never colour alone: the arrow AND a signed number carry the meaning,
      // and the exact comparison period lives in the tooltip.
      const chip = el("span", `trend ${up ? "trend-up" : "trend-down"}`,
        ` ${up ? "▲" : "▼"} ${Math.abs(trend)}%`);
      chip.title = `${up ? "Up" : "Down"} ${Math.abs(trend)}% ${since}`;
      b.appendChild(chip);
    }
    node.appendChild(b);
    node.appendChild(el("span", "muted", label));
    box.appendChild(node);
  }
  announce(`KPIs updated: ${k.clients} clients, ${k.active} active, MRR ${inr(k.mrr_inr)}`);
}

/* ---------- tenants ----------------------------------------------------- */
let ROWS = [], CURSOR_ROW = -1;
const COLS = 13;
const badge = (status) => {
  const s = el("span", `badge badge-${status}`, status.replace("_", " "));
  return s;
};
const useCell = (used, limit) => {
  const over = limit != null && used >= limit;
  return el("span", `use${over ? " use-over" : ""}`,
    `${used}${limit != null ? " / " + limit : ""}`);
};
const cell = (label, child) => {
  const td = el("td");
  td.dataset.label = label;
  if (typeof child === "string") td.textContent = child; else if (child) td.appendChild(child);
  return td;
};

async function loadTenants() {
  const body = $("tenants-body");
  skeletonRows(body, 5, COLS);
  const p = new URLSearchParams({
    q: $("t-search").value.trim(), status: $("t-status").value,
    plan: $("t-plan").value, sort: T.sort, order: T.order, limit: "50",
  });
  if ($("show-deleted").checked) p.set("include_deleted", "true");
  if (T.sort === "created_at" && T.cursor) p.set("cursor", T.cursor);
  if (T.sort !== "created_at" && T.offset) p.set("offset", String(T.offset));

  let d;
  try { d = await api("/control/api/tenants?" + p); }
  catch (e) { errState(body, COLS, e, loadTenants); return; }

  ROWS = d.tenants; CURSOR_ROW = -1;
  T._next = d.next_cursor; T._nextOffset = d.next_offset;
  $("t-count").textContent = `${d.tenants.length} of ${d.total}`;
  $("t-next").disabled = !(d.next_cursor || d.next_offset != null);
  $("t-prev").disabled = !T.stack.length;
  document.querySelectorAll("#tenants th").forEach((th) => {
    const btn = th.querySelector("button.sort");
    th.setAttribute("aria-sort", btn && btn.dataset.sort === T.sort
      ? (T.order === "asc" ? "ascending" : "descending") : "none");
  });
  document.querySelectorAll("#summary button.card").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.filter === $("t-status").value)));

  if (!d.tenants.length) {
    stateRow(body, COLS, t("empty").tenants,
      can("write") ? { label: "＋ Add Tenant", onClick: addTenantModal } : null);
    return;
  }
  body.replaceChildren();
  for (const row of d.tenants) body.appendChild(tenantRow(row));
  announce(`${d.tenants.length} tenants shown of ${d.total}`);
}

function tenantRow(x) {
  const u = x.usage || {}, del = !!x.deleted_at;
  const tr = el("tr");
  if (x.trial_warning && !del) tr.classList.add("is-warn");
  tr.dataset.slug = x.slug;
  tr.tabIndex = -1;

  const head = cell("Shop");
  head.className = "cell-head";
  const nameBtn = el("button", "link-cell",
    `${x.trial_warning && !del ? "⚠️ " : ""}${del ? "🗑 " : ""}${x.shop_name}`);
  nameBtn.type = "button";
  nameBtn.dataset.act = "profile";
  nameBtn.dataset.slug = x.slug;
  nameBtn.setAttribute("aria-label", `Open profile for ${x.shop_name}`);
  head.appendChild(nameBtn);
  head.appendChild(el("div", "muted",
    [x.slug, x.city, (x.tags || []).join(", ")].filter(Boolean).join(" · ")));
  tr.appendChild(head);

  tr.appendChild(cell("Plan", `${x.plan_name} (${x.billing_cycle})`));
  tr.appendChild(cell("Status", badge(x.status)));
  tr.appendChild(cell("Days left", x.days_left == null ? "—" : String(x.days_left)));
  tr.appendChild(cell("WhatsApp", x.wa_connected ? "Connected" : "—"));
  tr.appendChild(cell("Orders (mo)", useCell(u.orders_month ?? 0, u.orders_limit)));
  tr.appendChild(cell("WA msgs (mo)", useCell(u.wa_msgs_month ?? 0, u.wa_limit)));
  tr.appendChild(cell("AI (mo)", useCell(u.ai_calls_month ?? 0, u.ai_limit)));
  tr.appendChild(cell("Staff", useCell(u.staff ?? 0, u.staff_limit)));
  tr.appendChild(cell("Users", String(x.users)));
  tr.appendChild(cell("MRR", inr(x.mrr_inr)));
  const owner = cell("Owner", x.owner_name);
  owner.appendChild(el("div", "muted", x.owner_phone));
  tr.appendChild(owner);

  const acts = cell("Actions");
  acts.classList.add("cell-actions");
  const wrap = el("div", "actions");
  const add = (label, act, aria, danger) => {
    const b = el("button", `btn-sm${danger ? " btn-danger" : ""}`, label);
    b.type = "button"; b.dataset.act = act; b.dataset.slug = x.slug;
    if (aria) b.setAttribute("aria-label", aria);
    if ((act === "purge" || act === "soft-delete") && !can("danger")) b.disabled = true;
    else if (!can("write")) b.disabled = true;
    wrap.appendChild(b);
  };
  if (del) {
    add("♻ Restore", "restore", `Restore ${x.shop_name}`);
    add("Purge", "purge", `Permanently delete ${x.shop_name}`, true);
  } else {
    add(x.agent_enabled === false ? "🤖 Agent: OFF" : "🤖 Agent: ON", "agent",
        `${x.agent_enabled === false ? "Switch on" : "Switch off"} the AI agent for ${x.shop_name}`);
    add("Users", "users", `Manage users of ${x.shop_name}`);
    add("Plan", "plan", `Change plan of ${x.shop_name}`);
    add("Status", "status", `Change status of ${x.shop_name}`);
    add("+30d", "extend", `Extend ${x.shop_name} by 30 days`);
    add("Reset link", "pw", `Create password reset link for ${x.shop_name}`);
    add("🗑", "soft-delete", `Move ${x.shop_name} to recycle bin`, true);
  }
  acts.appendChild(wrap);
  tr.appendChild(acts);
  return tr;
}

/* ---------- billing / audit / keys (lazy, below the fold) --------------- */
async function loadBilling() {
  const body = $("billing-body");
  skeletonRows(body, 3, 4);
  let rows;
  try { rows = await api("/control/api/billing/events?limit=30"); }
  catch (e) { return errState(body, 4, e, loadBilling); }
  if (!rows.length) return stateRow(body, 4, t("empty").billing);
  body.replaceChildren();
  for (const e of rows) {
    const tr = el("tr");
    tr.appendChild(cell("When", fmtWhen(e.at)));
    tr.appendChild(cell("Tenant", e.tenant || "—"));
    tr.appendChild(cell("Event", e.type));
    tr.appendChild(cell("Amount", e.amount_inr ? inr(e.amount_inr) : "—"));
    body.appendChild(tr);
  }
}

let AUDIT = [], AUDIT_SHOWN = 0;
const AUDIT_PAGE = 25;

async function loadAudit() {
  const body = $("audit-body");
  skeletonRows(body, 4, 6);
  const slug = $("audit-tenant").value.trim();
  try {
    AUDIT = await api("/control/api/audit?limit=200" + (slug ? `&tenant_slug=${encodeURIComponent(slug)}` : ""));
  } catch (e) { return errState(body, 6, e, loadAudit); }
  AUDIT_SHOWN = 0;
  body.replaceChildren();
  if (!AUDIT.length) { $("btn-audit-more").classList.add("hidden"); return stateRow(body, 6, t("empty").audit); }
  renderMoreAudit();
}

/* Render on demand: 200 rows arrive, 25 paint. Keeps INP low and avoids a
   long task on slower phones. */
function renderMoreAudit() {
  const body = $("audit-body");
  const slice = AUDIT.slice(AUDIT_SHOWN, AUDIT_SHOWN + AUDIT_PAGE);
  const frag = document.createDocumentFragment();
  for (const r of slice) {
    const tr = el("tr");
    tr.appendChild(cell("When", fmtWhen(r.at)));
    tr.appendChild(cell("Tenant", r.tenant || "—"));
    tr.appendChild(cell("Actor", r.actor || r.actor_role));
    tr.appendChild(cell("Action", r.action));
    const det = cell("Details", JSON.stringify(r.args || {}));
    det.classList.add("muted", "tl-detail");
    tr.appendChild(det);
    tr.appendChild(cell("Result", r.ok ? "OK ✓" : "Failed ✗"));
    frag.appendChild(tr);
  }
  body.appendChild(frag);
  AUDIT_SHOWN += slice.length;
  const more = $("btn-audit-more");
  more.classList.toggle("hidden", AUDIT_SHOWN >= AUDIT.length);
  more.textContent = `Load more (${AUDIT.length - AUDIT_SHOWN} left)`;
}

async function loadKeys() {
  const body = $("keys-body");
  skeletonRows(body, 2, 6);
  let rows;
  try { rows = await api("/control/api/admin-keys"); }
  catch (e) {
    return stateRow(body, 6, ["🔒", "Keys need a danger-level key",
      "Sign in with the master key to manage per-admin keys."]);
  }
  if (!rows.length) {
    return stateRow(body, 6, t("empty").keys,
      can("danger") ? { label: "＋ New key", onClick: createKeyModal } : null);
  }
  body.replaceChildren();
  for (const k of rows) {
    const tr = el("tr");
    tr.appendChild(cell("Label", k.label));
    tr.appendChild(cell("Level", k.level));
    tr.appendChild(cell("Created", fmtWhen(k.created_at)));
    tr.appendChild(cell("Last used", fmtWhen(k.last_used_at)));
    tr.appendChild(cell("Status", k.revoked ? "Revoked" : "Active ✓"));
    const acts = cell("Actions");
    acts.classList.add("cell-actions");
    if (!k.revoked) {
      const b = el("button", "btn-sm btn-danger", "Revoke");
      b.type = "button"; b.dataset.act = "revoke-key";
      b.dataset.id = k.id; b.dataset.label = k.label;
      b.setAttribute("aria-label", `Revoke key ${k.label}`);
      acts.appendChild(b);
    }
    tr.appendChild(acts);
    body.appendChild(tr);
  }
}

/* ---------- modal plumbing (focus trap + restore) ----------------------- */
let LAST_FOCUS = null;
function openModal(build, title) {
  LAST_FOCUS = document.activeElement;
  const sheet = $("modal");
  sheet.replaceChildren();
  const h = el("h3", null, title);
  h.id = "modal-title";
  sheet.appendChild(h);
  build(sheet);
  $("ov").hidden = false;
  const first = sheet.querySelector("input, select, button");
  if (first) first.focus();
}
function closeModal() {
  $("ov").hidden = true;
  $("modal").classList.remove("workspace");
  $("modal").replaceChildren();
  if (LAST_FOCUS) LAST_FOCUS.focus();
}
function field(parent, id, label, value = "", type = "text") {
  const l = el("label", null, label);
  l.htmlFor = id;
  const i = el("input");
  i.id = id; i.type = type; i.value = value ?? "";
  parent.appendChild(l); parent.appendChild(i);
  return i;
}
function buttonRow(parent, buttons) {
  const row = el("div", "btnrow");
  for (const b of buttons) {
    const btn = el("button", b.cls || "", b.label);
    btn.type = "button";
    btn.addEventListener("click", b.onClick);
    row.appendChild(btn);
  }
  parent.appendChild(row);
  return row;
}

/* Destructive actions: plain confirm for reversible, TYPED confirm for
   irreversible (purge). Both are dialogs, never a bare browser prompt. */
function confirmDialog({ title, body, confirmLabel, danger, typed, onConfirm }) {
  openModal((sheet) => {
    sheet.appendChild(el("p", null, body));
    let input = null;
    if (typed) {
      input = field(sheet, "confirm-typed", `Type “${typed}” to confirm`);
      input.autocomplete = "off";
    }
    buttonRow(sheet, [
      {
        label: confirmLabel, cls: danger ? "btn-danger" : "btn-primary",
        onClick: async () => {
          if (typed && input.value.trim() !== typed) { toast("Text did not match", "err"); return; }
          closeModal();
          await onConfirm();
        },
      },
      { label: "Cancel", onClick: closeModal },
    ]);
  }, title);
}

/* ---------- tenant actions --------------------------------------------- */
const refresh = () => Promise.all([loadKpis(), loadTenants()]);

async function tenantAction(act, slug) {
  const row = ROWS.find((r) => r.slug === slug) || { shop_name: slug };
  if (act === "profile") return profileModal(slug);
  if (act === "users") return usersModal(slug);
  if (act === "plan") return planModal(slug, row);
  if (act === "status") return statusModal(slug, row);
  if (act === "extend") {
    return confirmDialog({
      title: "Extend subscription", body: `Add 30 days to ${row.shop_name} and mark it active?`,
      confirmLabel: "Extend 30 days",
      onConfirm: async () => {
        await act2(() => api(`/control/api/tenants/${slug}`, { method: "PATCH", body: { extend_days: 30 } }),
          `${row.shop_name} extended by 30 days`);
        refresh();
      },
    });
  }
  if (act === "agent") {
    const turningOff = row.agent_enabled !== false;
    return confirmDialog({
      title: turningOff ? "Switch the AI agent off" : "Switch the AI agent on",
      danger: turningOff,
      body: turningOff
        ? `${row.shop_name}'s bot will stop replying to customers entirely. Messages still arrive in their Inbox so their team can answer by hand.`
        : `${row.shop_name}'s bot will start answering customers again.`,
      confirmLabel: turningOff ? "Switch off" : "Switch on",
      onConfirm: async () => { await setAgent(slug, !turningOff); loadTenants(); },
    });
  }
  if (act === "pw") return passwordModal(slug, row);
  if (act === "soft-delete") {
    return confirmDialog({
      title: "Move to recycle bin", danger: true,
      body: `${row.shop_name}'s logins stop working, but every row of data stays. You can restore it any time.`,
      confirmLabel: "Move to recycle bin",
      onConfirm: async () => {
        await act2(() => api(`/control/api/tenants/${slug}?confirm=${encodeURIComponent(slug)}`, { method: "DELETE" }),
          `${row.shop_name} moved to recycle bin`);
        refresh();
      },
    });
  }
  if (act === "restore") {
    await act2(() => api(`/control/api/tenants/${slug}/restore`, { method: "POST" }), `${row.shop_name} restored`);
    return refresh();
  }
  if (act === "purge") {
    return confirmDialog({
      title: "Permanently delete", danger: true, typed: slug,
      body: `This erases ${row.shop_name} and its users for good. It cannot be undone — export a backup first if you may need it.`,
      confirmLabel: "Delete forever",
      onConfirm: async () => {
        await act2(() => api(`/control/api/tenants/${slug}?confirm=${encodeURIComponent(slug)}&purge=true`, { method: "DELETE" }),
          `${row.shop_name} deleted permanently`);
        refresh();
      },
    });
  }
}
const act2 = act; // alias kept short inside handlers

function selectModal(title, label, options, onPick, current) {
  openModal((sheet) => {
    const l = el("label", null, label);
    l.htmlFor = "sel-value";
    const sel = el("select");
    sel.id = "sel-value";
    for (const [v, text] of options) {
      const o = el("option", null, text);
      o.value = v;
      sel.appendChild(o);
    }
    /* Bina iske dropdown hamesha PEHLA option dikhata tha. Business wale
       tenant par bhi "Basic" khulta, aur bina soche Save dabane par shop
       chupchaap downgrade ho jaati. Status par bhi wahi — hamesha "Trial". */
    if (current != null) sel.value = current;
    sheet.appendChild(l); sheet.appendChild(sel);
    buttonRow(sheet, [
      { label: "Save", cls: "btn-primary", onClick: async () => { const v = sel.value; closeModal(); await onPick(v); } },
      { label: "Cancel", onClick: closeModal },
    ]);
  }, title);
}

/* Values asli plan CODES hain (starter/pro/growth), bikne wale naam nahi.
   Pehle yahan "premium"/"business" tha — backend ALIASES se resolve kar
   leta tha, isliye Save chalta tha, par DB "growth" lautati hai aur us par
   koi option match nahi karta. Isi wajah se Business wali shop par bhi
   dropdown "Basic" dikhata tha. */
const planModal = (slug, row) => selectModal(`Change plan — ${row.shop_name}`, "Plan",
  [["starter", "Basic — ₹999"], ["pro", "Premium — ₹1,999"], ["growth", "Business — ₹3,999"]],
  async (plan) => {
    await act(() => api(`/control/api/tenants/${slug}`, { method: "PATCH", body: { plan } }), "Plan updated");
    refresh();
  }, row.plan);

const statusModal = (slug, row) => selectModal(`Change status — ${row.shop_name}`, "Status",
  [["trial", "Trial"], ["active", "Active"], ["past_due", "Past due (read-only)"],
   ["locked", "Locked"], ["suspended", "Suspended"], ["cancelled", "Cancelled"]],
  async (status) => {
    await act(() => api(`/control/api/tenants/${slug}`, { method: "PATCH", body: { status } }), "Status updated");
    refresh();
  }, row.status);

function linkModal(title, body, path) {
  const url = location.origin + path;
  openModal((sheet) => {
    sheet.appendChild(el("p", null, body));
    sheet.appendChild(el("code", "copy", url));
    buttonRow(sheet, [
      { label: "Copy link", cls: "btn-primary", onClick: async (e) => {
          try { await navigator.clipboard.writeText(url); toast("Link copied"); }
          catch (err) { toast("Copy failed — select the text manually", "err"); } } },
      { label: "Done", onClick: closeModal },
    ]);
  }, title);
}


/* Password help: do raste — link bhejo (client khud set kare) ya turant
   naya password khud set kar do (support call ke liye). Dono mein password
   DB mein sirf hash jaata hai. */
function passwordModal(slug, row) {
  openModal((sheet) => {
    sheet.appendChild(el("p", "muted",
      `Choose how ${row.shop_name}'s owner gets back in. Either way every old session ends and the password is stored only as a hash.`));

    sheet.appendChild(el("h3", "mt", "Set a password now"));
    sheet.appendChild(el("p", "muted",
      "Fastest for a support call — you read it out, they must change it at first login."));
    const pw = field(sheet, "pw-new", "New password (8+ characters)", "", "password");
    pw.autocomplete = "new-password";
    addEyeToggle(pw);
    buttonRow(sheet, [
      { label: "🎲 Generate", onClick: () => { pw.value = randomPassword(); pw.type = "text";
          const b = pw.parentElement.querySelector(".eye"); if (b) b.textContent = "🙈"; } },
      { label: "Set password", cls: "btn-primary", onClick: async () => {
          if (pw.value.trim().length < 8) { toast("At least 8 characters", "err"); return; }
          const d = await act(() => api(`/control/api/tenants/${slug}/set-password`,
            { method: "POST", body: { password: pw.value.trim() } }), null);
          openModal((s2) => {
            s2.appendChild(el("p", null, `Tell ${d.email} this password. They must change it at first login.`));
            s2.appendChild(el("code", "copy", pw.value.trim()));
            buttonRow(s2, [
              { label: "Copy", cls: "btn-primary", onClick: async () => {
                  try { await navigator.clipboard.writeText(pw.value.trim()); toast("Password copied"); }
                  catch (e) { toast("Copy failed — read it from the box", "err"); } } },
              { label: "Done", onClick: closeModal },
            ]);
          }, "Password changed");
        } },
    ]);

    sheet.appendChild(el("h3", "mt", "Or send a set-password link"));
    sheet.appendChild(el("p", "muted",
      "They pick their own password — nobody, including you, ever sees it."));
    buttonRow(sheet, [
      { label: "Create link", onClick: async () => {
          const d = await act(() => api(`/control/api/tenants/${slug}/reset-password`, { method: "POST" }),
            "Reset link created");
          linkModal("Password reset link", `Send this to ${d.email}. It works once.`, d.reset_path);
        } },
      { label: "Cancel", onClick: closeModal },
    ]);
  }, `Password — ${row.shop_name}`);
}

/* Show/hide toggle — wahi jo har login form mein hota hai. */
function addEyeToggle(input) {
  const wrap = el("div", "pw-wrap");
  input.parentElement.insertBefore(wrap, input);
  wrap.appendChild(input);
  const b = el("button", "eye", "👁");
  b.type = "button";
  b.setAttribute("aria-label", "Show password");
  b.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    b.textContent = show ? "🙈" : "👁";
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
    input.focus();
  });
  wrap.appendChild(b);
  return b;
}

const randomPassword = () => {
  const abc = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const a = new Uint32Array(12);
  crypto.getRandomValues(a);
  return [...a].map((n) => abc[n % abc.length]).join("");
};

/* ---------- add tenant -------------------------------------------------- */
const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

function addTenantModal() {
  openModal((sheet) => {
    const shop = field(sheet, "f-shop", "Shop name");
    const slugWrap = el("div");
    const slug = field(slugWrap, "f-slug", "Slug");
    const hint = el("span", "muted");
    slugWrap.appendChild(hint);
    sheet.appendChild(slugWrap);
    const city = field(sheet, "f-city", "City");
    const owner = field(sheet, "f-owner", "Owner name");
    const phone = field(sheet, "f-phone", "Owner phone (+91…)");
    const email = field(sheet, "f-email", "Owner email", "", "email");

    const planL = el("label", null, "Plan"); planL.htmlFor = "f-plan";
    const plan = el("select"); plan.id = "f-plan";
    [["starter", "Basic"], ["premium", "Premium"], ["business", "Business"]]
      .forEach(([v, tx]) => { const o = el("option", null, tx); o.value = v; plan.appendChild(o); });
    const cycleL = el("label", null, "Billing cycle"); cycleL.htmlFor = "f-cycle";
    const cycle = el("select"); cycle.id = "f-cycle";
    ["monthly", "annual"].forEach((v) => { const o = el("option", null, v); o.value = v; cycle.appendChild(o); });
    sheet.append(planL, plan, cycleL, cycle);
    const trial = field(sheet, "f-trial", "Trial length (days)", "7", "number");

    let timer = null;
    const check = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const v = slug.value.trim();
        if (!v) { hint.textContent = ""; return; }
        try {
          const d = await api("/control/api/slug-check?slug=" + encodeURIComponent(v));
          hint.textContent = !d.valid ? "✗ invalid characters" : d.available ? "✓ available" : "✗ already taken";
        } catch (e) { hint.textContent = ""; }
      }, 300);
    };
    shop.addEventListener("input", () => { slug.value = slugify(shop.value); check(); });
    slug.addEventListener("input", check);

    buttonRow(sheet, [
      { label: "Create and invite", cls: "btn-primary", onClick: async () => {
          const body = {
            shop_name: shop.value.trim(), slug: slug.value.trim() || null,
            city: city.value.trim(), owner_name: owner.value.trim(),
            phone: phone.value.trim(), email: email.value.trim(),
            plan: plan.value, cycle: cycle.value,
            trial_days: parseInt(trial.value || "0", 10),
          };
          const d = await act(() => api("/control/api/tenants", { method: "POST", body }),
            `${body.shop_name} created`);
          refresh();
          linkModal("Send this set-password link",
            `WhatsApp: ${d.invite_sent_on_whatsapp ? "sent automatically" : "not sent — share it yourself"}. We never create or store their password.`,
            d.invite_path);
        } },
      { label: "Cancel", onClick: closeModal },
    ]);
  }, "New tenant");
}

/* ---------- profile ----------------------------------------------------- */
function bar(parent, label, used, limit) {
  const wrap = el("div", "mt");
  wrap.appendChild(el("div", "muted",
    `${label}: ${used}${limit != null ? " / " + limit : " (unlimited)"}`));
  if (limit != null) {
    const pct = limit ? Math.min(100, Math.round(used * 100 / limit)) : 0;
    const b = el("div", "bar" + (pct >= 90 ? " bad" : pct >= 70 ? " warn" : ""));
    const i = el("i");
    i.style.setProperty("--pct", pct + "%");   // CSSOM, not an inline attribute
    b.appendChild(i);
    wrap.appendChild(b);
  }
  parent.appendChild(wrap);
}

/* ---------- CLIENT WORKSPACE -------------------------------------------
   Ek client par click = uska poora control ek jagah, tabs mein:
   Overview · Usage & limits · Recharge · WhatsApp · AI agent · Users ·
   Billing · Activity · Danger. Har tab apna data alag se load karta hai
   (workspace turant khulta hai, spinner ke peeche nahi baithta). */
const WS_TABS = [
  ["overview", "Overview"], ["usage", "Usage & limits"], ["recharge", "Recharge"],
  ["whatsapp", "WhatsApp"], ["agent", "AI agent"], ["users", "Users"],
  ["billing", "Billing"], ["activity", "Activity"], ["danger", "Danger"],
];

async function profileModal(slug, openTab) {
  let d;
  try { d = await api(`/control/api/tenants/${slug}`); }
  catch (e) { toast(e.message, "err"); return; }

  LAST_FOCUS = document.activeElement;
  const sheet = $("modal");
  sheet.replaceChildren();
  sheet.classList.add("workspace");
  $("ov").hidden = false;

  // header: name, status, plan, quick facts
  const head = el("div", "ws-head");
  const title = el("h3", null, d.shop_name);
  title.id = "modal-title";
  const line1 = el("div", "row");
  line1.append(title, badge(d.status),
    el("span", "chip", `${d.plan_name} · ${d.billing_cycle}`),
    el("span", "chip", d.wa_connected ? "WhatsApp ✓" : "WhatsApp —"),
    el("span", `chip ${d.agent_enabled === false ? "chip-off" : "chip-on"}`,
      d.agent_enabled === false ? "Agent OFF" : "Agent ON"));
  head.appendChild(line1);
  head.appendChild(el("div", "muted",
    `${d.slug} · ${d.owner_name} · ${d.owner_phone} · joined ${fmtWhen(d.created_at)}`));
  const quick = el("div", "btnrow");
  quick.append(
    wsBtn("👤 Login as client", "btn-primary", () => impersonate(slug, d)),
    wsBtn("🔑 Password", "", () => passwordModal(slug, d)),
    wsBtn("⬇ Backup", "", () => download(slug, "json", true)),
    wsBtn("✕ Close", "", closeModal));
  head.appendChild(quick);
  sheet.appendChild(head);

  // tabs
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "tablist");
  const panel = el("div", "ws-panel");
  panel.id = "ws-panel";
  panel.setAttribute("role", "tabpanel");
  for (const [id, label] of WS_TABS) {
    const b = el("button", "tab", label);
    b.type = "button";
    b.id = `tab-${id}`;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", "false");
    b.addEventListener("click", () => selectTab(slug, d, id));
    tabs.appendChild(b);
  }
  sheet.appendChild(tabs);
  sheet.appendChild(panel);
  selectTab(slug, d, openTab || "overview");
}

const wsBtn = (label, cls, onClick) => {
  const b = el("button", cls, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
};

function selectTab(slug, d, id) {
  [...document.querySelectorAll(".tabs .tab")].forEach((b) =>
    b.setAttribute("aria-selected", String(b.id === `tab-${id}`)));
  const panel = $("ws-panel");
  panel.replaceChildren();
  panel.setAttribute("aria-labelledby", `tab-${id}`);
  ({
    overview: tabOverview, usage: tabUsage, recharge: tabRecharge,
    whatsapp: tabWhatsapp, agent: tabAgent, users: tabUsers,
    billing: tabBilling, activity: tabActivity, danger: tabDanger,
  })[id](panel, slug, d);
}

const reopen = (slug, tab) => { closeModal(); profileModal(slug, tab); };

/* --- tab: overview (stat tiles + profile edit) ------------------------- */
function tabOverview(box, slug, d) {
  const u = d.usage || {}, sub = d.subscription || {};
  const tiles = el("div", "ws-tiles");
  const tile = (label, value, sub2) => {
    const t = el("div", "ws-tile");
    t.appendChild(el("b", null, String(value)));
    t.appendChild(el("span", "muted", label));
    if (sub2) t.appendChild(el("div", "muted", sub2));
    tiles.appendChild(t);
  };
  tile("Orders this month", u.orders_month ?? 0, u.orders_limit == null ? "unlimited" : `limit ${u.orders_limit}`);
  tile("WhatsApp msgs", u.wa_msgs_month ?? 0, u.wa_limit == null ? "unlimited" : `limit ${u.wa_limit}`);
  tile("AI calls", u.ai_calls_month ?? 0, u.ai_limit == null ? "unlimited" : `limit ${u.ai_limit}`);
  tile("Staff", u.staff ?? 0, u.staff_limit == null ? "unlimited" : `limit ${u.staff_limit}`);
  tile("WA recharge", (d.credits || {}).wa ?? 0, "messages left");
  tile("AI recharge", (d.credits || {}).ai ?? 0, "calls left");
  tile("MRR", inr(d.mrr_inr), sub.current_period_end ? `paid till ${fmtWhen(sub.current_period_end)}` :
    sub.trial_ends_at ? `trial till ${fmtWhen(sub.trial_ends_at)}` : "");
  tile("Users", d.users, `${d.setup_fee_paid ? "setup paid" : "setup due"}`);
  box.appendChild(tiles);

  box.appendChild(el("h3", "mt", "Profile"));
  const shop = field(box, "p-shop", "Shop name", d.shop_name);
  const owner = field(box, "p-owner", "Owner", d.owner_name);
  const g = el("div", "grid-2");
  const phone = field(g, "p-phone", "Phone", d.owner_phone);
  const email = field(g, "p-email", "Email", d.owner_email || "");
  const city = field(g, "p-city", "City", d.city || "");
  const tags = field(g, "p-tags", "Tags (comma separated)", (d.tags || []).join(", "));
  box.appendChild(g);
  const notes = field(box, "p-notes", "Notes", d.notes || "");
  buttonRow(box, [{ label: "Save profile", cls: "btn-primary", onClick: async () => {
    await act(() => api(`/control/api/tenants/${slug}`, { method: "PATCH", body: {
      shop_name: shop.value.trim(), owner_name: owner.value.trim(),
      owner_phone: phone.value.trim(), owner_email: email.value.trim(),
      city: city.value.trim(), notes: notes.value,
      tags: tags.value.split(",").map((s) => s.trim()).filter(Boolean),
    } }), "Profile saved");
    loadTenants(); reopen(slug, "overview");
  } }]);

  box.appendChild(el("h3", "mt", "Plan & subscription"));
  const pg = el("div", "btnrow");
  pg.append(
    wsBtn("Change plan", "", () => planModal(slug, d)),
    wsBtn("Change status", "", () => statusModal(slug, d)),
    wsBtn("+30 days", "", () => confirmDialog({
      title: "Extend subscription", body: `Add 30 days to ${d.shop_name} and mark it active?`,
      confirmLabel: "Extend 30 days",
      onConfirm: async () => {
        await act(() => api(`/control/api/tenants/${slug}`, { method: "PATCH", body: { extend_days: 30 } }),
          "Extended by 30 days");
        loadTenants(); reopen(slug, "overview");
      },
    })));
  box.appendChild(pg);
}

/* --- tab: usage & limits ---------------------------------------------- */
function tabUsage(box, slug, d) {
  const u = d.usage || {}, c = d.credits || {};
  bar(box, "Orders this month", u.orders_month ?? 0, u.orders_limit);
  bar(box, "WhatsApp messages", u.wa_msgs_month ?? 0, u.wa_limit);
  bar(box, "AI calls", u.ai_calls_month ?? 0, u.ai_limit);
  bar(box, "Staff", u.staff ?? 0, u.staff_limit);
  box.appendChild(el("p", "muted mt",
    `Recharge left — WhatsApp: ${c.wa ?? 0} · AI: ${c.ai ?? 0}. ` +
    "Limit finishes → recharge is used automatically; when that is empty too, sending stops."));
  if (d.billing_estimate && d.billing_estimate.wa_overage_msgs) {
    box.appendChild(el("p", "muted",
      `Overage this month: ${d.billing_estimate.wa_overage_msgs} messages ≈ ${inr(d.billing_estimate.wa_overage_inr)}`));
  }

  box.appendChild(el("h3", "mt", "Override the plan limits"));
  box.appendChild(el("p", "muted", "Blank = leave unchanged · -1 = unlimited"));
  const g = el("div", "grid-2");
  const ai = field(g, "lo-ai", "AI calls / month", "");
  const wa = field(g, "lo-wa", "WA messages / month", "");
  const or = field(g, "lo-orders", "Orders / month", "");
  const st = field(g, "lo-staff", "Staff", "");
  box.appendChild(g);
  ai.placeholder = String(u.ai_limit ?? "unlimited");
  wa.placeholder = String(u.wa_limit ?? "unlimited");
  or.placeholder = String(u.orders_limit ?? "unlimited");
  st.placeholder = String(u.staff_limit ?? "unlimited");
  const num = (i) => i.value.trim() === "" ? null : parseInt(i.value, 10);
  buttonRow(box, [
    { label: "Save limits", cls: "btn-primary", onClick: async () => {
        await act(() => api(`/control/api/tenants/${slug}/limits`, { method: "PUT", body: {
          ai_usage_limit: num(ai), whatsapp_message_limit: num(wa),
          max_orders_month: num(or), max_staff: num(st),
        } }), "Limits updated");
        loadTenants(); reopen(slug, "usage");
      } },
    { label: "Reset to plan defaults", onClick: async () => {
        await act(() => api(`/control/api/tenants/${slug}/limits`, { method: "PUT", body: { clear: true } }),
          "Back to plan defaults");
        loadTenants(); reopen(slug, "usage");
      } },
  ]);
}

/* --- tab: recharge ----------------------------------------------------- */
async function tabRecharge(box, slug, d) {
  const c = d.credits || {};
  const tiles = el("div", "ws-tiles");
  for (const [label, val] of [["WhatsApp messages left", c.wa ?? 0], ["AI calls left", c.ai ?? 0]]) {
    const t = el("div", "ws-tile");
    t.appendChild(el("b", null, String(val)));
    t.appendChild(el("span", "muted", label));
    tiles.appendChild(t);
  }
  box.appendChild(tiles);
  box.appendChild(el("p", "muted",
    "Recharge kicks in only after the plan limit is used up. Each extra message or AI call spends one credit."));

  const g = el("div", "grid-2");
  const kindL = el("label", null, "What to recharge");
  kindL.htmlFor = "rc-kind";
  const kind = el("select");
  kind.id = "rc-kind";
  [["wa", "WhatsApp messages"], ["ai", "AI calls"]].forEach(([v, tx]) => {
    const o = el("option", null, tx); o.value = v; kind.appendChild(o);
  });
  const amtWrap = el("div");
  const amt = field(amtWrap, "rc-amt", "How many (use minus to take back)", "1000", "number");
  g.append(kindL, kind);
  box.appendChild(g);
  box.appendChild(amtWrap);
  const validity = field(box, "rc-days", "Valid for (days) — blank = never expires", "", "number");
  const reason = field(box, "rc-why", "Note — GPay ref, amount, anything", "");

  const quick = el("div", "btnrow");
  [500, 1000, 5000, 10000].forEach((n) =>
    quick.appendChild(wsBtn(`+${n}`, "", () => { amt.value = String(n); })));
  box.appendChild(quick);

  buttonRow(box, [{ label: "Apply recharge", cls: "btn-primary", onClick: async () => {
    const n = parseInt(amt.value, 10);
    if (!n) { toast("Enter an amount", "err"); return; }
    const days = parseInt(validity.value, 10);
    const r = await act(() => api(`/control/api/tenants/${slug}/credits`, { method: "POST",
      body: { kind: kind.value, amount: n, reason: reason.value.trim(),
              valid_days: days > 0 ? days : null } }), null);
    toast(`${kind.value === "wa" ? "WhatsApp" : "AI"} recharge: ${r.changed_by > 0 ? "+" : ""}${r.changed_by} → balance ${r.balance}` +
      (r.expires_at ? ` (valid till ${fmtWhen(r.expires_at)})` : ""));
    loadTenants(); reopen(slug, "recharge");
  } }]);

  box.appendChild(el("h3", "mt", "Recharge history"));
  const list = el("div", "timeline");
  list.appendChild(el("p", "muted", "Loading…"));
  box.appendChild(list);
  try {
    const h = await api(`/control/api/tenants/${slug}/credits?limit=30`);
    list.replaceChildren();
    if (!h.history.length) list.appendChild(el("p", "muted", "No recharges yet."));
    for (const r of h.history) {
      const item = el("div", "tl-item");
      item.appendChild(el("b", null, `${r.amount > 0 ? "+" : ""}${r.amount} ${r.kind === "wa" ? "WA msgs" : "AI calls"}`));
      item.appendChild(el("span", "muted",
        ` · balance ${r.balance_after} · ${r.by} · ${fmtWhen(r.at)}${r.reason ? " · " + r.reason : ""}` +
        (r.expires_at ? (r.expired ? ` · expired ${fmtWhen(r.expires_at)}` : ` · valid till ${fmtWhen(r.expires_at)}`) : "")));
      list.appendChild(item);
    }
  } catch (e) { list.replaceChildren(el("p", "muted", "Could not load history")); }
}

/* --- tab: whatsapp ------------------------------------------------------ */
function tabWhatsapp(box, slug, d) {
  const sub = d.subscription || {};
  box.appendChild(el("p", "muted", d.wa_connected
    ? "Connected. Messages from this number land in this client's inbox and replies go out from it."
    : "Not connected yet. Paste the Meta credentials for this client's number."));
  const g = el("div", "grid-2");
  const pnid = field(g, "wa-pnid", "Phone number ID", sub.wa_phone_number_id || "");
  const waba = field(g, "wa-waba", "WABA ID", "");
  box.appendChild(g);
  const tok = field(box, "wa-token", "Access token (stored, never shown again)", "", "password");
  tok.autocomplete = "new-password";
  addEyeToggle(tok);
  buttonRow(box, [{ label: "Validate with Meta and save", cls: "btn-primary", onClick: async () => {
    if (!tok.value.trim()) { toast("Paste the access token first", "err"); return; }
    await act(() => api(`/control/api/tenants/${slug}/whatsapp`, { method: "POST", body: {
      phone_number_id: pnid.value.trim(), waba_id: waba.value.trim(), token: tok.value.trim(),
    } }), "WhatsApp connected");
    loadTenants(); reopen(slug, "whatsapp");
  } }]);
  box.appendChild(el("p", "muted mt",
    "Meta's own limits (250 → 1K → 10K unique customers/day) apply on top of this and rise with the number's quality rating."));
}

/* --- tab: AI agent ------------------------------------------------------ */
function tabAgent(box, slug, d) {
  const on = d.agent_enabled !== false;
  const u = d.usage || {}, c = d.credits || {};
  box.appendChild(el("p", null, on
    ? "The AI agent is answering this client's customers."
    : "The AI agent is OFF — the bot stays silent, messages still reach their Inbox."));
  buttonRow(box, [
    { label: on ? "⏸ Switch agent OFF" : "▶ Switch agent ON",
      cls: on ? "btn-danger" : "btn-primary",
      onClick: () => confirmDialog({
        title: on ? "Switch the AI agent off" : "Switch the AI agent on", danger: on,
        body: on
          ? "The bot will stop replying to customers entirely. Their team can still answer by hand from the Inbox."
          : "The bot will start answering customers again.",
        confirmLabel: on ? "Switch off" : "Switch on",
        onConfirm: async () => { await setAgent(slug, !on); loadTenants(); reopen(slug, "agent"); },
      }) },
  ]);
  box.appendChild(el("h3", "mt", "AI budget"));
  bar(box, "AI calls this month", u.ai_calls_month ?? 0, u.ai_limit);
  box.appendChild(el("p", "muted", `Recharge left: ${c.ai ?? 0} calls`));
  buttonRow(box, [
    { label: "Recharge AI", onClick: () => reopen(slug, "recharge") },
    { label: "Change AI limit", onClick: () => reopen(slug, "usage") },
  ]);
}

/* --- tab: users --------------------------------------------------------- */
async function tabUsers(box, slug, d) {
  const list = el("div", "timeline");
  list.appendChild(el("p", "muted", "Loading…"));
  box.appendChild(list);
  let rows = [];
  try { rows = await api(`/control/api/tenants/${slug}/users`); }
  catch (e) { list.replaceChildren(el("p", "muted", e.message)); return; }
  list.replaceChildren();
  if (!rows.length) list.appendChild(el("p", "muted", "No users yet."));
  for (const u of rows) {
    const line = el("div", "tl-item");
    line.appendChild(el("b", null, u.name));
    line.appendChild(el("span", "muted",
      ` · ${u.email} · ${u.role} · ${!u.is_active ? "revoked" : u.pending_invite ? "invite pending" : "active"}`));
    if (u.is_active) {
      const b = el("button", "btn-sm btn-danger", "Revoke");
      b.type = "button";
      b.setAttribute("aria-label", `Revoke access for ${u.email}`);
      b.addEventListener("click", () => confirmDialog({
        title: "Revoke access", danger: true,
        body: `${u.email} will be signed out immediately and cannot sign in again.`,
        confirmLabel: "Revoke access",
        onConfirm: async () => {
          await act(() => api(`/control/api/tenants/${slug}/users/${u.id}/revoke`, { method: "POST" }),
            "Access revoked");
          reopen(slug, "users");
        },
      }));
      line.appendChild(b);
    }
    list.appendChild(line);
  }
  box.appendChild(el("h3", "mt", "Invite a user"));
  const name = field(box, "iu-name", "Name");
  const email = field(box, "iu-email", "Email", "", "email");
  const roleL = el("label", null, "Role");
  roleL.htmlFor = "iu-role";
  const role = el("select");
  role.id = "iu-role";
  ["STAFF", "MANAGER", "ACCOUNTANT", "OWNER"].forEach((r) => {
    const o = el("option", null, r); o.value = r; role.appendChild(o);
  });
  box.append(roleL, role);
  buttonRow(box, [{ label: "Create invite link", cls: "btn-primary", onClick: async () => {
    const r = await act(() => api(`/control/api/tenants/${slug}/users/invite`, { method: "POST",
      body: { name: name.value.trim(), email: email.value.trim(), role: role.value } }),
      "Invite created");
    linkModal("Invite link", `Send this to ${r.email} (${r.role}). They set their own password.`, r.invite_path);
  } }]);
}

/* --- tab: billing ------------------------------------------------------- */
function tabBilling(box, slug, d) {
  const sub = d.subscription || {};
  box.appendChild(el("p", "muted", [
    `status ${d.status}`,
    sub.trial_ends_at ? `trial ends ${fmtWhen(sub.trial_ends_at)}` : null,
    sub.current_period_end ? `paid till ${fmtWhen(sub.current_period_end)}` : null,
    `setup fee ${sub.setup_fee_paid ? "paid" : "due"}`,
    sub.rzp_subscription_id ? `razorpay ${sub.rzp_subscription_id}` : "no subscription yet",
  ].filter(Boolean).join(" · ")));
  box.appendChild(el("h3", "mt", `Invoices (${d.invoices.length})`));
  if (!d.invoices.length) box.appendChild(el("p", "muted", "No invoices yet."));
  for (const i of d.invoices) {
    const line = el("div", "tl-item");
    line.appendChild(el("b", null, inr(i.amount_inr)));
    line.appendChild(el("span", "muted", ` · ${i.plan} ${i.cycle} · ${fmtWhen(i.date)} · ${i.payment_id}`));
    box.appendChild(line);
  }
  buttonRow(box, [
    { label: "⬇ Export JSON", onClick: () => download(slug, "json", false) },
    { label: "⬇ Export CSV", onClick: () => download(slug, "csv", false) },
  ]);
}

/* --- tab: activity ------------------------------------------------------ */
async function tabActivity(box, slug) {
  const list = el("div", "timeline");
  list.appendChild(el("p", "muted", "Loading…"));
  box.appendChild(list);
  let tl = [];
  try { tl = await api(`/control/api/tenants/${slug}/timeline?limit=60`); }
  catch (e) { list.replaceChildren(el("p", "muted", e.message)); return; }
  list.replaceChildren();
  if (!tl.length) list.appendChild(el("p", "muted", "Nothing yet."));
  for (const x of tl) {
    const item = el("div", "tl-item");
    item.appendChild(el("b", null, `${{ audit: "🛠", billing: "💳", invoice: "🧾" }[x.kind] || "•"} ${x.title}`));
    item.appendChild(el("span", "muted", ` · ${x.actor} · ${fmtWhen(x.at)}${x.ok === false ? " · failed" : ""}`));
    if (x.detail) item.appendChild(el("div", "muted tl-detail", JSON.stringify(x.detail)));
    list.appendChild(item);
  }
}

/* --- tab: danger -------------------------------------------------------- */
function tabDanger(box, slug, d) {
  box.appendChild(el("p", "muted",
    "Everything here is logged with your key label and IP. Data is never deleted without a typed confirmation."));
  buttonRow(box, [
    { label: "🗑 Move to recycle bin", cls: "btn-danger", onClick: () =>
        confirmDialog({
          title: "Move to recycle bin", danger: true,
          body: `${d.shop_name}'s logins stop working, but every row of data stays. You can restore it any time.`,
          confirmLabel: "Move to recycle bin",
          onConfirm: async () => {
            await act(() => api(`/control/api/tenants/${slug}?confirm=${encodeURIComponent(slug)}`, { method: "DELETE" }),
              "Moved to recycle bin");
            closeModal(); refresh();
          },
        }) },
    { label: "Permanently delete", cls: "btn-danger", onClick: () =>
        confirmDialog({
          title: "Permanently delete", danger: true, typed: slug,
          body: `This erases ${d.shop_name} and its users for good. Export a backup first if you may need it.`,
          confirmLabel: "Delete forever",
          onConfirm: async () => {
            await act(() => api(`/control/api/tenants/${slug}?confirm=${encodeURIComponent(slug)}&purge=true`, { method: "DELETE" }),
              "Deleted permanently");
            closeModal(); refresh();
          },
        }) },
  ]);
}

function impersonate(slug, d) {
  confirmDialog({
    title: "Open client's dashboard", danger: true,
    body: `You will be signed in as ${d.owner_name}'s owner account for 30 minutes. This is written to the audit log.`,
    confirmLabel: "Open as client",
    onConfirm: async () => {
      const r = await act(() => api(`/control/api/tenants/${slug}/impersonate`, { method: "POST" }),
        "Impersonation session created");
      window.open(r.adopt_path, "_blank", "noopener");
    },
  });
}


async function setAgent(slug, on) {
  await act(() => api(`/control/api/tenants/${slug}/settings`, { method: "PUT", body: { key: "agent_enabled", value: on } }),
    on ? "AI agent switched on" : "AI agent switched off — the bot stays silent");
}

async function download(slug, format, business) {
  try {
    const r = await fetch(`/control/api/tenants/${slug}/export?format=${format}&include_business=${business}`,
      { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    saveBlob(await r.blob(), (r.headers.get("Content-Disposition") || "").split('filename="')[1]?.replace('"', "") || `${slug}.${format}`);
    toast("Export downloaded");
  } catch (e) { toast("Export failed: " + e.message, "err"); }
}
function saveBlob(blob, name) {
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
function exportCsv(name, header, rows) {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = [header.map(esc).join(","), ...rows.map((r) => r.map(esc).join(","))].join("\n");
  saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), name);
  toast("CSV downloaded");
}

/* ---------- users ------------------------------------------------------- */
async function usersModal(slug) {
  let rows;
  try { rows = await api(`/control/api/tenants/${slug}/users`); }
  catch (e) { toast(e.message, "err"); return; }
  openModal((sheet) => {
    if (!rows.length) sheet.appendChild(el("p", "muted", "No users yet."));
    for (const u of rows) {
      const line = el("div", "tl-item");
      line.appendChild(el("b", null, u.name));
      line.appendChild(el("span", "muted",
        ` · ${u.email} · ${u.role} · ${!u.is_active ? "revoked" : u.pending_invite ? "invite pending" : "active"}`));
      if (u.is_active) {
        const b = el("button", "btn-sm btn-danger", "Revoke");
        b.type = "button";
        b.setAttribute("aria-label", `Revoke access for ${u.email}`);
        b.addEventListener("click", () => confirmDialog({
          title: "Revoke access", danger: true,
          body: `${u.email} will be signed out immediately and cannot sign in again.`,
          confirmLabel: "Revoke access",
          onConfirm: async () => {
            await act(() => api(`/control/api/tenants/${slug}/users/${u.id}/revoke`, { method: "POST" }),
              "Access revoked");
            closeModal(); usersModal(slug);
          },
        }));
        line.appendChild(b);
      }
      sheet.appendChild(line);
    }
    sheet.appendChild(el("h3", "mt", "Invite a user"));
    const name = field(sheet, "iu-name", "Name");
    const email = field(sheet, "iu-email", "Email", "", "email");
    const roleL = el("label", null, "Role"); roleL.htmlFor = "iu-role";
    const role = el("select"); role.id = "iu-role";
    ["STAFF", "MANAGER", "ACCOUNTANT", "OWNER"].forEach((r) => {
      const o = el("option", null, r); o.value = r; role.appendChild(o);
    });
    sheet.append(roleL, role);
    buttonRow(sheet, [
      { label: "Create invite link", cls: "btn-primary", onClick: async () => {
          const d = await act(() => api(`/control/api/tenants/${slug}/users/invite`, { method: "POST",
            body: { name: name.value.trim(), email: email.value.trim(), role: role.value } }),
            "Invite created");
          linkModal("Invite link", `Send this to ${d.email} (${d.role}). They set their own password.`, d.invite_path);
        } },
      { label: "Close", onClick: closeModal },
    ]);
  }, `Users — ${slug}`);
}

/* ---------- admin keys -------------------------------------------------- */
function createKeyModal() {
  openModal((sheet) => {
    const label = field(sheet, "k-label", "Label (who is it for)");
    const lvlL = el("label", null, "Level"); lvlL.htmlFor = "k-level";
    const lvl = el("select"); lvl.id = "k-level";
    [["read", "read — view only"], ["write", "write — everyday actions"], ["danger", "danger — delete, reset, keys"]]
      .forEach(([v, tx]) => { const o = el("option", null, tx); o.value = v; lvl.appendChild(o); });
    lvl.value = "write";
    sheet.append(lvlL, lvl);
    buttonRow(sheet, [
      { label: "Create key", cls: "btn-primary", onClick: async () => {
          const d = await act(() => api("/control/api/admin-keys", { method: "POST",
            body: { label: label.value.trim(), level: lvl.value } }), "Key created");
          openModal((s2) => {
            s2.appendChild(el("p", null, "This key is shown once — the server only keeps a hash. Copy it now."));
            s2.appendChild(el("code", "copy", d.key));
            buttonRow(s2, [
              { label: "Copy", cls: "btn-primary", onClick: async () => {
                  try { await navigator.clipboard.writeText(d.key); toast("Key copied"); }
                  catch (e) { toast("Copy failed — select it manually", "err"); } } },
              { label: "Done", onClick: () => { closeModal(); loadKeys(); } },
            ]);
          }, `${d.label} (${d.level})`);
        } },
      { label: "Cancel", onClick: closeModal },
    ]);
  }, "New admin key");
}

/* ---------- command palette (Ctrl/Cmd-K) -------------------------------- */
const COMMANDS = [
  { id: "add", label: "Add tenant", hint: "create", run: addTenantModal, need: "write" },
  { id: "backup", label: "Run backup now", hint: "job", run: () => runBackup(), need: "write" },
  { id: "sweep", label: "Run subscription sweep + dunning", hint: "job", run: () => runSweep(), need: "write" },
  { id: "key", label: "Create admin key", hint: "security", run: createKeyModal, need: "danger" },
  { id: "csv", label: "Export tenants as CSV", hint: "export", run: () => exportTenantsCsv() },
  { id: "trial", label: "Filter: trials", hint: "view", run: () => setStatusFilter("trial") },
  { id: "pastdue", label: "Filter: past due", hint: "view", run: () => setStatusFilter("past_due") },
  { id: "clear", label: "Clear all filters", hint: "view", run: () => { $("t-search").value = ""; $("t-status").value = ""; $("t-plan").value = ""; resetAndLoad(); } },
  { id: "bin", label: "Toggle recycle bin", hint: "view", run: () => { $("show-deleted").checked = !$("show-deleted").checked; resetAndLoad(); } },
  { id: "signout", label: "Sign out", hint: "session", run: () => signOut() },
];
let PAL = [], PAL_I = 0;

function openPalette() {
  $("palette-ov").hidden = false;
  const inp = $("palette-input");
  inp.value = "";
  renderPalette("");
  inp.focus();
}
function closePalette() { $("palette-ov").hidden = true; }

function renderPalette(q) {
  const needle = q.trim().toLowerCase();
  const cmds = COMMANDS.filter((c) => (!c.need || can(c.need)) && c.label.toLowerCase().includes(needle))
    .map((c) => ({ label: c.label, hint: c.hint, run: c.run }));
  const tenants = ROWS.filter((r) => !needle || r.shop_name.toLowerCase().includes(needle) || r.slug.includes(needle))
    .slice(0, 8)
    .map((r) => ({ label: r.shop_name, hint: `${r.slug} · open profile`, run: () => profileModal(r.slug) }));
  PAL = [...cmds, ...tenants].slice(0, 12);
  PAL_I = 0;
  const list = $("palette-list");
  list.replaceChildren();
  if (!PAL.length) {
    const li = el("li", null, "No matches");
    li.setAttribute("role", "option");
    list.appendChild(li);
    return;
  }
  PAL.forEach((item, i) => {
    const li = el("li");
    li.id = `pal-${i}`;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(i === 0));
    li.appendChild(el("span", null, item.label));
    li.appendChild(el("span", "hint", item.hint));
    li.addEventListener("click", () => { closePalette(); item.run(); });
    list.appendChild(li);
  });
  $("palette-input").setAttribute("aria-activedescendant", "pal-0");
}
function movePalette(delta) {
  if (!PAL.length) return;
  PAL_I = (PAL_I + delta + PAL.length) % PAL.length;
  [...$("palette-list").children].forEach((li, i) =>
    li.setAttribute("aria-selected", String(i === PAL_I)));
  $("palette-input").setAttribute("aria-activedescendant", `pal-${PAL_I}`);
  $("palette-list").children[PAL_I].scrollIntoView({ block: "nearest" });
}

/* ---------- jobs -------------------------------------------------------- */
async function runBackup() {
  const d = await act(() => api("/control/api/backup/run", { method: "POST" }), null);
  toast(d.verified ? `Backup verified: ${d.file.split(/[\\/]/).pop()}` : "Backup written but verification FAILED — check logs",
    d.verified ? "ok" : "err");
}
async function runSweep() {
  const d = await act(() => api("/control/api/dunning/run", { method: "POST" }), null);
  const m = d.moved || {};
  toast(`Sweep done — ${m.past_due || 0} to past due, ${m.locked || 0} locked, ${m.reminders || 0} reminders`);
  refresh();
}
function exportTenantsCsv() {
  exportCsv("tenants.csv",
    ["shop", "slug", "plan", "cycle", "status", "days_left", "users", "mrr_inr", "owner", "phone", "city", "wa_connected"],
    ROWS.map((r) => [r.shop_name, r.slug, r.plan_name, r.billing_cycle, r.status, r.days_left,
      r.users, r.mrr_inr, r.owner_name, r.owner_phone, r.city, r.wa_connected ? "yes" : "no"]));
}
function exportAuditCsv() {
  exportCsv("audit.csv", ["when", "tenant", "actor", "action", "details", "ok"],
    AUDIT.map((r) => [r.at, r.tenant, r.actor || r.actor_role, r.action, JSON.stringify(r.args || {}), r.ok]));
}

/* ---------- filters / paging ------------------------------------------- */
function resetAndLoad() { T.cursor = ""; T.offset = 0; T.stack = []; writeUrl(); loadTenants(); }
function setStatusFilter(v) { $("t-status").value = v; resetAndLoad(); }
function setSort(col) {
  if (T.sort === col) T.order = T.order === "asc" ? "desc" : "asc";
  else { T.sort = col; T.order = "asc"; }
  resetAndLoad();
}

/* ---------- session ----------------------------------------------------- */
function showSignin() {
  $("signin").classList.remove("hidden");
  $("appbar").classList.add("hidden");
  // Khali tables dikhane se panel "toota hua" lagta tha — signed out par
  // ek saaf explanation, data sections chhupi hui.
  $("main").classList.add("hidden");
  $("signedout").classList.remove("hidden");
  $("session-label").textContent = STR[L].signedOut;
  const k = $("key");
  if (k) k.focus();
}
function showApp(level, label) {
  LEVEL = level;
  $("signin").classList.add("hidden");
  $("appbar").classList.remove("hidden");
  $("main").classList.remove("hidden");
  $("signedout").classList.add("hidden");
  $("session-label").textContent = STR[L].signedInAs(label, level);
}
async function signOut() {
  try { await api("/control/api/session/logout", { method: "POST" }); } catch (e) {}
  showSignin();
  toast("Signed out");
}

async function boot() {
  readUrl();
  let s = null;
  try { s = await api("/control/api/session"); } catch (e) { /* no cookie yet */ }
  if (!s) { showSignin(); return; }
  showApp(s.level, s.label);
  await Promise.all([loadKpis(), loadTenants()]);   // above the fold first
  lazySections();                                    // rest after first paint
}

/* Below-the-fold sections load when they scroll into view (or right after
   first paint on tall screens) — keeps first render fast. */
function lazySections() {
  const jobs = [["billing-wrap", loadBilling], ["audit-wrap", loadAudit], ["keys-wrap", loadKeys]];
  if (!("IntersectionObserver" in window)) { jobs.forEach(([, fn]) => fn()); return; }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const job = jobs.find(([id]) => $(id) === e.target);
      if (job) { job[1](); io.unobserve(e.target); }
    }
  }, { rootMargin: "200px" });
  jobs.forEach(([id]) => io.observe($(id)));
}

/* ---------- wiring (all listeners; zero inline handlers) ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  $("signin").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("key");
    const key = input.value.trim();
    input.value = "";                       // never keep it around
    if (!key) return;
    try {
      const s = await (await fetchOrThrow("/control/api/session", {
        method: "POST", headers: { "X-API-Key": key }, credentials: "same-origin",
      })).json();
      showApp(s.level, s.label);
      toast(`Signed in as ${s.label}`);
      await Promise.all([loadKpis(), loadTenants()]);
      lazySections();
    } catch (err) { toast("Sign-in failed: " + err.message, "err"); }
  });

  addEyeToggle($("key"));
  $("btn-signout").addEventListener("click", signOut);
  $("btn-add").addEventListener("click", addTenantModal);
  $("btn-backup").addEventListener("click", runBackup);
  $("btn-sweep").addEventListener("click", runSweep);
  $("btn-palette").addEventListener("click", openPalette);
  $("btn-new-key").addEventListener("click", createKeyModal);
  $("btn-export-tenants").addEventListener("click", exportTenantsCsv);
  $("btn-export-audit").addEventListener("click", exportAuditCsv);
  $("btn-audit-filter").addEventListener("click", loadAudit);
  $("btn-audit-more").addEventListener("click", renderMoreAudit);

  let searchTimer = null;
  $("t-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(resetAndLoad, 350);     // debounce: 1 request per pause
  });
  $("t-status").addEventListener("change", resetAndLoad);
  $("t-plan").addEventListener("change", resetAndLoad);
  $("show-deleted").addEventListener("change", resetAndLoad);
  $("t-next").addEventListener("click", () => {
    if (T._next) { T.stack.push(T.cursor); T.cursor = T._next; }
    else if (T._nextOffset != null) { T.stack.push(T.offset); T.offset = T._nextOffset; }
    loadTenants();
  });
  $("t-prev").addEventListener("click", () => {
    if (!T.stack.length) return;
    const prev = T.stack.pop();
    if (T.sort === "created_at") T.cursor = prev; else T.offset = prev;
    loadTenants();
  });

  document.querySelectorAll("th button.sort").forEach((b) =>
    b.addEventListener("click", () => setSort(b.dataset.sort)));

  // one delegated handler for every row action + KPI drill-down
  document.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (act) { tenantAction(act.dataset.act, act.dataset.slug); return; }
    const kpi = e.target.closest("button.card[data-filter]");
    if (kpi) {
      setStatusFilter($("t-status").value === kpi.dataset.filter ? "" : kpi.dataset.filter);
      return;
    }
    const rk = e.target.closest('[data-act="revoke-key"]');
    if (rk) return;
  });
  $("keys-body").addEventListener("click", (e) => {
    const b = e.target.closest('[data-act="revoke-key"]');
    if (!b) return;
    confirmDialog({
      title: "Revoke key", danger: true,
      body: `Anything using “${b.dataset.label}” stops working immediately, including open panel sessions.`,
      confirmLabel: "Revoke key",
      onConfirm: async () => {
        await act(() => api(`/control/api/admin-keys/${b.dataset.id}/revoke`, { method: "POST" }), "Key revoked");
        loadKeys();
      },
    });
  });

  $("ov").addEventListener("click", (e) => { if (e.target.id === "ov") closeModal(); });
  $("palette-ov").addEventListener("click", (e) => { if (e.target.id === "palette-ov") closePalette(); });
  $("palette-input").addEventListener("input", (e) => renderPalette(e.target.value));
  $("palette-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
    else if (e.key === "Enter") { e.preventDefault(); const item = PAL[PAL_I]; if (item) { closePalette(); item.run(); } }
  });

  // global keyboard: Cmd/Ctrl-K palette, / search, j/k rows, Enter open, Esc close
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (e.key === "Escape") {
      if (!$("palette-ov").hidden) closePalette();
      else if (!$("ov").hidden) closeModal();
      return;
    }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); $("t-search").focus(); return; }
    if (e.key === "j" || e.key === "k") {
      const rows = [...$("tenants-body").querySelectorAll("tr[data-slug]")];
      if (!rows.length) return;
      CURSOR_ROW = Math.max(0, Math.min(rows.length - 1, CURSOR_ROW + (e.key === "j" ? 1 : -1)));
      rows.forEach((r, i) => r.setAttribute("aria-selected", String(i === CURSOR_ROW)));
      rows[CURSOR_ROW].focus();
      rows[CURSOR_ROW].scrollIntoView({ block: "nearest" });
    }
    if (e.key === "Enter" && CURSOR_ROW >= 0) {
      const row = $("tenants-body").querySelectorAll("tr[data-slug]")[CURSOR_ROW];
      if (row) profileModal(row.dataset.slug);
    }
  });

  boot();
});

async function fetchOrThrow(url, init) {
  const r = await fetch(url, init);
  if (!r.ok) {
    let d = `HTTP ${r.status}`;
    try { d = (await r.json()).detail || d; } catch (e) {}
    throw new Error(d);
  }
  return r;
}
