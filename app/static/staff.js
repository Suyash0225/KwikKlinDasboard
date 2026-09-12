/* Staff panel — chhota, bina kisi library ke.
 *
 * Ek niyam: UI kabhi khud tay nahi karta ki kaun kya kar sakta hai. Wo
 * /staff/api/me ke `features` aur `is_manager` se sirf DIKHATA hai; asli
 * rok server par hai. Isliye panel aur API kabhi alag nahi kah sakte.
 */
const $ = (id) => document.getElementById(id);
let ME = null, TAB = "mine", NAV = "mine", TASKS = [];

async function api(path, opts = {}) {
  const res = await fetch(`/staff/api${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, err = false, ms = 3000) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/* Button dabate hi spinner, phir kaam, phir jawab.
 *
 * Pehle in buttons par kuch dikhta hi nahi tha: dheeme network par staff ko
 * lagta tha click laga hi nahi aur wo dobara dabata tha — do baar wahi kaam
 * chal jata tha. Ab button turant band (double-tap khatam) aur spinner
 * chalu; galti hui to button wapas wahi ka wahi, taaki dobara koshish ho
 * sake. */
async function busy(btn, fn) {
  if (!btn) return fn();
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>';
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

function openModal(html) { $("modal-body").innerHTML = html; $("modal-ov").classList.add("open"); }
function closeModal() { $("modal-ov").classList.remove("open"); }
$("modal-ov").addEventListener("click", (e) => { if (e.target.id === "modal-ov") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ---------------------------------------------------------------- login */
$("lg-eye").onclick = () => {
  const i = $("lg-pass");
  i.type = i.type === "password" ? "text" : "password";
  $("lg-eye").textContent = i.type === "password" ? "👁" : "🙈";
};
$("lg-go").onclick = async () => {
  const phone = $("lg-phone").value.trim();
  const password = $("lg-pass").value;
  $("lg-err").textContent = "";
  if (phone.replace(/\D/g, "").length < 10) { $("lg-err").textContent = "Enter the full mobile number."; return; }
  if (!password) { $("lg-err").textContent = "Enter your password."; return; }
  $("lg-go").disabled = true;
  try {
    await api("/login", { method: "POST", body: { phone, password } });
    await start();
  } catch (e) {
    $("lg-err").textContent = e.message;
  } finally {
    $("lg-go").disabled = false;
  }
};
$("lg-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("lg-go").click(); });
$("lg-phone").addEventListener("keydown", (e) => { if (e.key === "Enter") $("lg-pass").focus(); });

/* ----------------------------------------------------------------- boot */
async function start() {
  try {
    ME = await api("/me");
  } catch (e) {
    // Server ne SAAF mana kiya (401/402) tabhi login. Network hichki par
    // aadmi ko bahar nahi phenkte — wo bas dobara koshish kar sake.
    $("boot").hidden = true;
    $("login").hidden = false; $("app").hidden = true;
    if (e.status === 402) $("lg-err").textContent = e.message;
    else if (!e.status) $("lg-err").textContent = "Network problem — check your signal, then login.";
    return;
  }
  $("boot").hidden = true;
  $("login").hidden = true; $("app").hidden = false;
  $("who").textContent = ME.name;
  $("whoRole").textContent = `${roleLabel(ME.role)} · ${ME.shop || ""}`;
  $("planChip").textContent = ME.plan;
  $("pwbanner").hidden = !ME.must_change_password;
  $("nav-team").hidden = !(ME.is_manager && ME.features.includes("staff_reports"));
  $("nav-bill").hidden = !ME.features.includes("billing");
  renderTabs();
  await loadTasks();
  renderToday();
  startLiveUpdates();
}

/* ------------------------------------------------------- live updates
 *
 * Manager ne kaam badla, owner ne naya task diya, kisi ne paisa jama
 * kiya — wo staff ke phone par turant dikhna chahiye, bina refresh ke.
 *
 * SSE, WebSocket nahi: khabar sirf server se phone tak jaati hai, aur
 * connection tootne par browser khud dobara jud jaata hai — jo mobile
 * network par sabse zaroori baat hai.
 *
 * Screen chhupi ho to kuch nahi maangte: bina iske chalta hua stream
 * pichhe se battery aur data dono khaata rehta hai. Tab wapas aate hi
 * ek refresh, taaki chhupe rehne ke waqt ka farak ek baar mein poora ho. */
let LIVE = null, LIVE_TIMER = null, LIVE_POLL = null, LIVE_SEEN = 0;
let COUNTS = {};   // /tasks se — tab par kitna kaam bacha hai

/* Stream par aakhri baar kab kuch guzra — khabar ho ya dhadkan. Server har
   20 second par dhadkan bhejta hai, isliye 60 second ki chuppi = stream
   mar chuki. */
const LIVE_SILENCE_MS = 60000;
function liveIsProven() { return LIVE_SEEN > 0 && Date.now() - LIVE_SEEN < LIVE_SILENCE_MS; }
function liveSeen() { LIVE_SEEN = Date.now(); }

/* Pichhe se aaya refresh (SSE/poll) list ko "Loading…" se NAHI badalta —
   purani list tab tak rehti hai jab tak nayi na aa jaye. Pehle har 30
   second par poori screen ek pal ko khaali ho jaati thi, aur aadmi jo
   card padh raha tha wo uske haath se nikal jaata tha. */
function refreshCurrent() {
  if (document.hidden) return;
  if (NAV === "route") showRoute({ quiet: true });
  else if (NAV === "mine") loadTasks({ quiet: true });
  renderToday();
  if (!LIVE) startLiveUpdates();   // stream toot gayi thi to dobara jodo
}

function liveSoon() {
  clearTimeout(LIVE_TIMER);
  LIVE_TIMER = setTimeout(refreshCurrent, 400);
}

/* Poochhna hamesha chalu — bas jab tak stream khud ko sabit kar rahi hai
   tab tak chup. Pehle fallback tabhi chalta tha jab `onerror` teen baar
   aaye, par 401 par browser dobara judta hi nahi (yani onerror ek hi baar
   aata hai) — to na stream chalti thi, na polling. */
function startPolling() {
  if (LIVE_POLL) return;
  LIVE_POLL = setInterval(() => {
    if (liveIsProven()) return;
    refreshCurrent();
  }, 30000);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) liveSoon();
});

function startLiveUpdates() {
  startPolling();
  if (!("EventSource" in window) || LIVE) return;
  try {
    LIVE = new EventSource("/staff/api/events", { withCredentials: true });
  } catch (e) {
    return;
  }
  LIVE.addEventListener("ready", liveSeen);
  LIVE.addEventListener("ping", liveSeen);
  LIVE.onmessage = () => { liveSeen(); liveSoon(); };
  LIVE.onerror = (e) => {
    // Source event se, `LIVE` se nahi — neeche wo null ho jaata hai.
    const src = e && e.target;
    if (src && src.readyState === EventSource.CLOSED) {
      // Browser khud nahi jodega (401/5xx) — agla poll dobara koshish karega.
      LIVE_SEEN = 0;
      LIVE = null;
    }
  };
}
const roleLabel = (r) => ({
  WASHER: "Washerman", DELIVERY: "Delivery",
  // Senior aadmi — kaam bhi karta hai aur dekh-rekh bhi. Use sirf
  // "Washerman" likhna uske kaam ko chhota dikhata hai.
  SUPERVISOR: "Washerman / Manager", MANAGER: "Manager", ADMIN: "Owner",
}[r] || r);

function renderTabs() {
  // Manager ko poori dukaan ke tabs; baaki ko sirf apne. Ye wahi shart hai
  // jo server par bhi lagti hai — yahan sirf dikhawa.
  const tabs = ME.is_manager
    ? [["mine", "Mine"], ["pending", "All pending"], ["done", "Done"], ["cancelled", "Cancelled"]]
    : [["mine", "My work"], ["done", "Done"]];
  // Ginti sirf pending wale tabs par — "Done" par 200 ka number kisi kaam ka nahi.
  const cnt = (v) => (COUNTS[v] > 0 ? `<span class="cnt">${COUNTS[v]}</span>` : "");
  $("tabs").innerHTML = tabs.map(([v, label]) =>
    `<button data-tab="${v}" class="${TAB === v ? "on" : ""}">${label}${cnt(v)}</button>`).join("");
  $("tabs").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { TAB = b.dataset.tab; renderTabs(); loadTasks(); };
  });
}

let TASKS_SEQ = 0;
async function loadTasks(opts = {}) {
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  const mine = ++TASKS_SEQ;
  try {
    const r = await api(`/tasks?tab=${encodeURIComponent(TAB)}`);
    // Tab jaldi-jaldi badla to purana jawab baad mein aakar galat list na dikhaye
    if (mine !== TASKS_SEQ || NAV !== "mine") return;
    TASKS = r.tasks;
    COUNTS = r.counts || {};
    renderTabs();
    renderTasks();
  } catch (e) {
    if (mine !== TASKS_SEQ) return;
    if (opts.quiet) return;            // pichhe ka refresh gira — list rehne do
    $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderTasks() {
  if (!TASKS.length) {
    $("list").innerHTML = `<div class="empty">Nothing here 👍</div>`;
    return;
  }
  $("list").innerHTML = TASKS.map((t) => {
    const o = t.order;
    return `
    <article class="tcard ${t.urgent ? "urgent" : ""}">
      <div class="row1">
        <span class="code">${esc(t.code)}${t.assignee ? " · " + esc(t.assignee) : ""}</span>
        <span class="age">${t.status === "OPEN" ? t.age_hours + "h pending" : esc(t.status)}</span>
      </div>
      <h3>${t.urgent ? "🔴 " : ""}${esc(t.title)}</h3>
      ${o ? `<div class="ord">
        <b>${esc(o.number)} · ${esc(o.customer)}</b>
        <div class="kv"><span>Phone</span><span>${esc(o.phone_masked)}</span></div>
        <div class="kv"><span>Items</span><span>${esc(o.items)}</span></div>
        ${o.delivery ? `<div class="kv"><span>Delivery</span><span class="${dueClass(o.delivery)}">${esc(whenText(o.delivery))}</span></div>` : ""}
        <div class="kv"><span>Due</span><span>₹${o.due.toFixed(0)}</span></div>
        ${o.notes ? `<div class="kv"><span>Note</span><span>${esc(String(o.notes).slice(-120))}</span></div>` : ""}
      </div>` : ""}
      ${t.cancel_requested ? `<div style="margin-top:8px"><span class="pill warn">Cancel requested: ${esc(t.cancel_reason || "")}</span></div>` : ""}
      ${t.eta_text ? `<div style="margin-top:8px"><span class="pill ok">Said: ${esc(t.eta_text)}</span></div>` : ""}
      ${t.status === "OPEN" ? `<div class="acts">
        <button class="btn" data-do="done" data-code="${esc(t.code)}">✅ Done</button>
        <button class="btn ghost" data-do="ask" data-code="${esc(t.code)}">❓ Ask</button>
        ${ME.features.includes("cancel_approval") && !t.cancel_requested
          ? `<button class="btn ghost" data-do="cancel" data-code="${esc(t.code)}">🛑 Cancel</button>` : ""}
        ${o && ME.features.includes("cod_collection") && o.due > 0
          ? `<button class="btn amber" data-do="collect" data-code="${esc(t.code)}" data-order="${esc(o.number)}" data-due="${o.due}">💰 Collect</button>` : ""}
        ${ME.is_manager && t.cancel_requested
          ? `<button class="btn danger" data-do="decide" data-code="${esc(t.code)}">Decide cancel</button>` : ""}
      </div>` : ""}
    </article>`;
  }).join("");
  $("list").querySelectorAll("[data-do]").forEach((b) => { b.onclick = () => act(b.dataset); });
}

/* "2026-09-14" ko phone par padhna padta hai — "Today", "Tomorrow", "Mon 14 Sep"
   ek nazar mein samajh aata hai. Beeti hui date laal, taaki late order chhupe nahi. */
function whenText(iso) {
  if (!iso) return "";
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  if (isNaN(d)) return iso;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 864e5);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  const txt = d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
  return diff < 0 ? `${txt} · ${-diff}d late` : txt;
}
function dueClass(iso) {
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return d < today ? "late" : "";
}

function act(d) {
  if (d.do === "done") return askNote(d.code);
  if (d.do === "ask") return askQuestion(d.code);
  if (d.do === "cancel") return askCancel(d.code);
  if (d.do === "collect") return askCollect(d.order, parseFloat(d.due));
  if (d.do === "decide") return decideCancel(d.code);
}

function askNote(code) {
  openModal(`<h3>${esc(code)} — done?</h3>
    <textarea id="m-note" placeholder="Anything to add? (optional)"></textarea>
    <div class="btnrow">
      <button class="btn ghost" id="m-x">Not now</button>
      <button class="btn" id="m-ok">Yes, done</button>
    </div>`);
  $("m-x").onclick = closeModal;
  $("m-ok").onclick = (e) => busy(e.currentTarget, async () => {
    await api(`/tasks/${encodeURIComponent(code)}/done`, { method: "POST", body: { note: $("m-note").value.trim() } });
    closeModal(); toast(`${code} closed ✅`); loadTasks(); renderToday();
  });
}

function askQuestion(code) {
  openModal(`<h3>${esc(code)} — what do you want to ask?</h3>
    <textarea id="m-q" placeholder="e.g. is the address right? how many items?"></textarea>
    <div class="btnrow">
      <button class="btn ghost" id="m-x">Not now</button>
      <button class="btn" id="m-ok">Send</button>
    </div>`);
  $("m-x").onclick = closeModal;
  $("m-ok").onclick = (e) => {
    const text = $("m-q").value.trim();
    if (text.length < 2) { toast("Write your question first", true); return; }
    return busy(e.currentTarget, async () => {
      await api(`/tasks/${encodeURIComponent(code)}/ask`, { method: "POST", body: { text } });
      closeModal(); toast("Sent to the owner 🙏");
    });
  };
}

function askCancel(code) {
  openModal(`<h3>${esc(code)} — request a cancel?</h3>
    <p style="color:#6b7280;font-size:13.5px;margin:0 0 10px">
      You cannot cancel this yourself — write the reason and your manager will decide.</p>
    <textarea id="m-r" placeholder="Reason — e.g. customer refused"></textarea>
    <div class="btnrow">
      <button class="btn ghost" id="m-x">Not now</button>
      <button class="btn danger" id="m-ok">Send</button>
    </div>`);
  $("m-x").onclick = closeModal;
  $("m-ok").onclick = (e) => {
    const reason = $("m-r").value.trim();
    // pehle yahan chup-chaap return tha — button dabta tha, kuch hota nahi tha
    if (reason.length < 3) { toast("Write the reason first", true); return; }
    return busy(e.currentTarget, async () => {
      await api(`/tasks/${encodeURIComponent(code)}/cancel-request`, { method: "POST", body: { reason } });
      closeModal(); toast("Sent to your manager"); loadTasks();
    });
  };
}

function askCollect(order, due) {
  // ₹250.50 due ho to "251" bharna server par "Only ₹250 is due" deta tha —
  // prefill exact rakho, upar ki hadd bhi wahi.
  const dueStr = Number.isInteger(due) ? String(due) : due.toFixed(2);
  openModal(`<h3>${esc(order)} — payment collected</h3>
    <label>Amount (due ₹${dueStr})</label>
    <input id="m-amt" type="number" inputmode="decimal" value="${dueStr}" min="1" max="${dueStr}" step="0.01">
    <label>How</label>
    <div class="btnrow">
      <button class="btn ghost" id="m-cash">💵 Cash</button>
      <button class="btn ghost" id="m-upi">📱 UPI</button>
    </div>
    <div class="btnrow"><button class="btn ghost" id="m-x">Not now</button></div>`);
  $("m-x").onclick = closeModal;
  const send = (e, method) => {
    const amount = parseFloat($("m-amt").value);
    if (!(amount > 0)) { toast("Enter the amount", true); return; }
    if (amount > due + 0.01) { toast(`Only ₹${dueStr} is due`, true); return; }
    return busy(e.currentTarget, async () => {
      const r = await api(`/orders/${encodeURIComponent(order)}/collect`, { method: "POST", body: { amount, method } });
      closeModal(); toast(`₹${amount} collected ✅ — ₹${r.due.toFixed(0)} left`);
      renderToday();
      if (NAV === "route") showRoute(); else loadTasks();
    });
  };
  $("m-cash").onclick = (e) => send(e, "cash");
  $("m-upi").onclick = (e) => send(e, "upi");
}

function decideCancel(code) {
  const t = TASKS.find((x) => x.code === code) || {};
  openModal(`<h3>${esc(code)} — cancel request</h3>
    <p style="font-size:14px">${esc(t.cancel_reason || "")}</p>
    <div class="btnrow">
      <button class="btn ghost" id="m-no">No, keep it</button>
      <button class="btn danger" id="m-yes">Yes, cancel</button>
    </div>`);
  const send = (e, approve) => busy(e.currentTarget, async () => {
    await api(`/tasks/${encodeURIComponent(code)}/cancel-decide`, { method: "POST", body: { approve } });
    closeModal(); toast(approve ? "Cancelled" : "Cancel refused"); loadTasks();
  });
  $("m-yes").onclick = (e) => send(e, true);
  $("m-no").onclick = (e) => send(e, false);
}

/* ------------------------------------------------------------- profile */
function showProfile() {
  $("list").innerHTML = `
    <article class="tcard">
      <h3>${esc(ME.name)}</h3>
      <div class="ord">
        <div class="kv"><span>Role</span><span>${esc(roleLabel(ME.role))}</span></div>
        <div class="kv"><span>Phone</span><span>${esc(ME.phone)}</span></div>
        <div class="kv"><span>Shop</span><span>${esc(ME.shop || "-")}</span></div>
        <div class="kv"><span>Plan</span><span>${esc(ME.plan)}</span></div>
      </div>
      <div class="acts">
        <button class="btn ghost" id="p-pw">Change password</button>
        <button class="btn danger" id="p-out">Logout</button>
      </div>
      <p class="hint">Your role and access are set by the owner — they cannot be changed here.</p>
    </article>`;
  $("p-pw").onclick = changePw;
  $("p-out").onclick = async () => {
    try { await api("/logout", { method: "POST" }); } catch (e) { /* cookie waise bhi jayegi */ }
    location.reload();
  };
}

function changePw() {
  openModal(`<h3>Change password</h3>
    <label for="p-old">Current password</label>
    <div class="pwrap">
      <input id="p-old" type="password" autocomplete="current-password">
      <button type="button" class="eye" data-eye="p-old" aria-label="Show password">👁</button>
    </div>
    <label for="p-new">New password (at least 6)</label>
    <div class="pwrap">
      <input id="p-new" type="password" autocomplete="new-password">
      <button type="button" class="eye" data-eye="p-new" aria-label="Show password">👁</button>
    </div>
    <div class="err" id="p-err"></div>
    <div class="btnrow">
      <button class="btn ghost" id="m-x">Not now</button>
      <button class="btn" id="m-ok">Save</button>
    </div>`);
  document.querySelectorAll("[data-eye]").forEach((b) => {
    b.onclick = () => {
      const i = $(b.dataset.eye);
      i.type = i.type === "password" ? "text" : "password";
      b.textContent = i.type === "password" ? "👁" : "🙈";
    };
  });
  $("m-x").onclick = closeModal;
  $("m-ok").onclick = async () => {
    const oldp = $("p-old").value, newp = $("p-new").value;
    if (newp.length < 6) { $("p-err").textContent = "That new password is too short."; return; }
    try {
      await api("/password", { method: "POST", body: { old_password: oldp, new_password: newp } });
      closeModal(); toast("Password changed ✅");
      ME.must_change_password = false; $("pwbanner").hidden = true;
    } catch (e) { $("p-err").textContent = e.message; }
  };
}

/* ---------------------------------------------------------------- team */
async function showTeam() {
  $("list").innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const rows = await api("/team");
    $("list").innerHTML = rows.map((s) => `
      <article class="tcard">
        <div class="row1">
          <span class="code">${esc(roleLabel(s.role))}</span>
          <span class="age">${s.has_login ? "" : "no panel login"}</span>
        </div>
        <h3>${esc(s.name)}</h3>
        <div class="ord">
          <div class="kv"><span>Pending now</span><span>${s.open}</span></div>
          <div class="kv"><span>Done in 24h</span><span>${s.done_24h}</span></div>
          <div class="kv"><span>Phone</span><span>${esc(s.phone_masked)}</span></div>
        </div>
      </article>`).join("") || `<div class="empty">No staff yet</div>`;
  } catch (e) {
    $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ nav */
document.querySelectorAll(".bottom button").forEach((b) => {
  b.onclick = () => {
    NAV = b.dataset.nav;
    document.querySelectorAll(".bottom button").forEach((x) => x.classList.toggle("on", x === b));
    $("tabs").hidden = NAV !== "mine";
    if (NAV === "mine") loadTasks();
    else if (NAV === "route") showRoute();
    else if (NAV === "bill") showBill();
    else if (NAV === "team") showTeam();
    else showProfile();
  };
});
$("btn-refresh").onclick = () => {
  // Dabaya to kuch dikhe — ek ghoomta hua icon, taaki dobara na dabaye
  const b = $("btn-refresh");
  b.classList.add("spinning"); setTimeout(() => b.classList.remove("spinning"), 700);
  if (NAV === "mine") loadTasks();
  else if (NAV === "route") showRoute();
  else if (NAV === "bill") { RATES = null; showBill(); }
  else if (NAV === "team") showTeam();
  else showProfile();
};
$("pw-open").onclick = changePw;

start();

/* ------------------------------------------------------------ naya bill */
/* Counter par khada aadmi apne phone se bill banata hai. Daam wo nahi
   bharta — rate card se aata hai, wahi jo WhatsApp wale bill par lagta
   hai. Do jagah do hisaab kabhi nahi. */
let RATES = null, BILL = [];

async function showBill() {
  if (!ME.features.includes("billing")) {
    $("list").innerHTML = `<div class="empty">Billing is not included in this plan.</div>`;
    return;
  }
  if (RATES === null) {
    $("list").innerHTML = `<div class="empty">Loading the rate card…</div>`;
    try { RATES = await api("/rates"); }
    catch (e) { $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  }
  const services = [...new Set(RATES.map((r) => r.service))];
  if (!services.length) {
    $("list").innerHTML = `<div class="empty">No rate card yet — ask the owner to add services and prices in the dashboard (Settings → Rate card). Bills need prices.</div>`;
    return;
  }
  $("list").innerHTML = `
    <article class="tcard">
      <h3>New bill</h3>
      <label for="b-name">Customer's name</label>
      <div class="ac-wrap">
        <input id="b-name" type="text" placeholder="Start typing — old customers show up" maxlength="60" autocomplete="off">
        <div class="acp" id="b-ac"></div>
      </div>
      <div id="b-picked" class="picked" hidden></div>
      <label for="b-phone">Customer's number</label>
      <input id="b-phone" type="tel" inputmode="numeric" placeholder="98xxxxxxxx" maxlength="15">
    </article>

    <article class="tcard">
      <h3>Add items</h3>
      <div class="frow">
        <div>
          <label for="b-svc">Service</label>
          <select id="b-svc">${services.map((s) => `<option>${esc(s)}</option>`).join("")}</select>
        </div>
        <div>
          <label for="b-item">Item</label>
          <select id="b-item"></select>
        </div>
        <div class="qty">
          <label for="b-qty">Qty</label>
          <input id="b-qty" type="number" inputmode="decimal" value="1" min="0.1" step="0.5">
        </div>
      </div>
      <button class="btn wide ghost" id="b-add">+ Add</button>
    </article>

    <article class="tcard" id="b-cartcard" hidden>
      <h3>Bill</h3>
      <div id="b-cart"></div>
      <label for="b-adv">Advance received (₹)</label>
      <input id="b-adv" type="number" inputmode="decimal" value="0" min="0">
      <button class="btn wide" id="b-save">Create bill</button>
    </article>`;
  const fillItems = () => {
    const svc = $("b-svc").value;
    $("b-item").innerHTML = RATES.filter((r) => r.service === svc)
      .map((r) => `<option value="${esc(r.garment)}">${esc(r.garment)} — ₹${r.rate}${r.unit === "kg" ? "/kg" : ""}</option>`)
      .join("");
  };
  fillItems();
  $("b-svc").onchange = fillItems;
  $("b-add").onclick = () => {
    const svc = $("b-svc").value, item = $("b-item").value;
    const qty = parseFloat($("b-qty").value);
    if (!(qty > 0)) return toast("Enter how many", true);
    const rate = (RATES.find((r) => r.service === svc && r.garment === item) || {}).rate || 0;
    const same = BILL.find((x) => x.service === svc && x.garment === item);
    if (same) same.qty += qty; else BILL.push({ service: svc, garment: item, qty, rate });
    $("b-qty").value = "1";
    renderCart();
  };
  $("b-save").onclick = (e) => saveBill(e.currentTarget);
  wireCustomerSearch();
  renderCart();
}

/* Naam likhte hi purana customer.
 *
 * Counter par sabse badi galti yahi hoti thi: wahi grahak har baar naye
 * number ke saath dobara ban jaata tha (ek digit idhar-udhar), aur uska
 * purana hisaab kahin aur padha rehta tha. Naam se chunne par ye khatam.
 *
 * Chunne par number NAHI dikhta — panel ka usool wahi rehta hai. Bill
 * server par `ref` se banta hai, isliye staff bina number dekhe sahi
 * customer par bill bana leta hai. */
let PICKED_REF = "";
let AC_TIMER = null;
let AC_SEQ = 0;
let AC_HITS = [];

function wireCustomerSearch() {
  const input = $("b-name");
  const box = $("b-ac");
  if (!input || !box) return;

  const clearPick = () => {
    PICKED_REF = "";
    $("b-picked").hidden = true;
    $("b-phone").disabled = false;
  };

  input.oninput = () => {
    clearPick();
    const q = input.value.trim();
    clearTimeout(AC_TIMER);
    if (q.length < 2) { box.innerHTML = ""; return; }
    const mine = ++AC_SEQ;
    // Debounce: har akshar par server nahi jaate. 250ms chup = ek request.
    AC_TIMER = setTimeout(async () => {
      let hits = [];
      try {
        hits = await api(`/customers/search?q=${encodeURIComponent(q)}`);
      } catch (e) {
        box.innerHTML = "";       // sujhaav suvidha hai — fail ho to chup
        return;
      }
      // Dheema jawab tez jawab ke baad aakar purani list na dikha de
      if (mine !== AC_SEQ) return;
      AC_HITS = hits;
      box.innerHTML = hits.length
        ? hits.map((c, i) =>
            `<div data-pick="${i}">${esc(c.name || "No name")} · ${esc(c.phone_masked)}</div>`).join("")
        : `<div class="none">New customer — enter the number below</div>`;
      box.querySelectorAll("[data-pick]").forEach((row) => {
        row.onclick = () => {
          const c = AC_HITS[parseInt(row.dataset.pick, 10)];
          if (!c) return;
          PICKED_REF = c.ref;
          input.value = c.name || "";
          box.innerHTML = "";
          // Number chuna ja chuka — ab haath se likhne ki zaroorat nahi
          $("b-phone").value = "";
          $("b-phone").disabled = true;
          $("b-picked").hidden = false;
          $("b-picked").textContent = `✓ ${c.name || "Customer"} · ${c.phone_masked}`;
        };
      });
    }, 250);
  };

}
// Bahar tap = sujhaav band. Ek hi baar — pehle ye har "New bill" par dobara
// judta tha, to das chakkar ke baad das listener chal rahe the.
document.addEventListener("click", (e) => {
  const box = $("b-ac");
  if (box && !e.target.closest(".ac-wrap")) box.innerHTML = "";
});

function renderCart() {
  const box = $("b-cart"), card = $("b-cartcard");
  if (!box) return;
  card.hidden = BILL.length === 0;
  const total = BILL.reduce((s, i) => s + i.qty * i.rate, 0);
  box.innerHTML = BILL.map((i, n) => `
    <div class="kv cartrow">
      <span>${esc(i.garment)} <small>${esc(i.service)}</small> × ${i.qty}</span>
      <b>₹${(i.qty * i.rate).toFixed(0)}
        <button class="rm" data-rm="${n}" aria-label="Remove">✕</button></b>
    </div>`).join("") + `<div class="kv total"><span>Total</span><b>₹${total.toFixed(0)}</b></div>`;
  box.querySelectorAll("[data-rm]").forEach((b) => {
    b.onclick = () => { BILL.splice(parseInt(b.dataset.rm), 1); renderCart(); };
  });
}

async function saveBill(btn) {
  const phone = $("b-phone").value.trim();
  // Purana customer chuna hai to number ki zaroorat hi nahi — wo DB se aayega
  if (!PICKED_REF && phone.replace(/\D/g, "").length < 10) {
    return toast("Enter the full number", true);
  }
  if (!BILL.length) return toast("Add items first", true);
  await busy(btn, async () => {
    const r = await api("/bills", {
      method: "POST",
      body: {
        customer_ref: PICKED_REF,
        customer_phone: PICKED_REF ? "" : phone,
        customer_name: $("b-name").value.trim(),
        advance: parseFloat($("b-adv").value) || 0,
        items: BILL.map((i) => ({ service: i.service, garment: i.garment, qty: i.qty })),
      },
    });
    BILL = [];
    PICKED_REF = "";
    toast(`✅ ${r.order_number} created — ₹${r.total.toFixed(0)}, due ₹${r.due.toFixed(0)}`, false, 6000);
    showBill();
  });
}

/* ------------------------------------------------- phone par install */
let INSTALL_EVT = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  INSTALL_EVT = e;
  $("installbar").hidden = false;
});
$("btn-install").onclick = async () => {
  if (!INSTALL_EVT) return;
  INSTALL_EVT.prompt();
  await INSTALL_EVT.userChoice;
  INSTALL_EVT = null;
  $("installbar").hidden = true;
};
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/staff/sw.js").catch(() => { /* app phir bhi chalega */ });
}


/* ------------------------------------------------------ aaj ka hisaab */
/* Din ki pehli nazar: kitna bacha, kitna nipta, kitna paisa liya. Ye
   sabse upar isliye hai ki phone kholte hi jawab mil jaye — poori list
   scroll karne ki zarurat na pade. */
async function renderToday() {
  const box = document.getElementById("todaybar");
  if (!box) return;
  try {
    const t = await api("/today");
    const tile = (label, value, cls = "") =>
      `<div class="tile ${cls}"><span>${label}</span><b>${value}</b></div>`;
    box.innerHTML =
      tile("Work left", t.pending, t.pending ? "warn" : "ok") +
      tile("Done today", t.done_today, "ok") +
      (t.can_collect ? tile("Collected", "₹" + t.collected_today.toFixed(0)) : "") +
      (t.shop_pending !== undefined
        ? tile("Shop pending", t.shop_pending) : "");
    box.hidden = false;
  } catch (e) {
    box.hidden = true;   // hisaab na mile to chup — kaam to chalta rahe
  }
}

/* ----------------------------------------------------------- raasta */
let ROUTE_SEQ = 0;
async function showRoute(opts = {}) {
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  const mine = ++ROUTE_SEQ;
  try {
    const r = await api("/route");
    if (mine !== ROUTE_SEQ || NAV !== "route") return;
    if (!r.stops.length) {
      $("list").innerHTML = `<div class="empty">Nowhere to go today 👍</div>`;
      return;
    }
    $("list").innerHTML = r.stops.map((s, i) => `
      <article class="tcard ${s.urgent ? "urgent" : ""}">
        <div class="row1">
          <span class="code">${i + 1}. ${esc(s.kind)} · ${esc(s.number)}</span>
          <span class="age ${s.delivery ? dueClass(s.delivery) : ""}">${s.delivery ? esc(whenText(s.delivery)) : ""}</span>
        </div>
        <h3>${s.urgent ? "🔴 " : ""}${esc(s.customer)}</h3>
        <div class="ord">
          ${s.address ? `<div class="kv"><span>Address</span><span>${esc(s.address)}</span></div>` : ""}
          <div class="kv"><span>Items</span><span>${esc(s.items)}</span></div>
          <div class="kv"><span>Due</span><span>₹${s.due.toFixed(0)}</span></div>
        </div>
        <div class="acts">
          <button class="btn ghost" data-call="${esc(s.number)}">📞 Call</button>
          ${s.address ? `<button class="btn ghost" data-map="${esc(s.address)}">🗺️ Route</button>` : ""}
          <button class="btn ghost" data-photo="${esc(s.number)}">📷 Photo</button>
          ${ME.features.includes("cod_collection") && s.due > 0
            ? `<button class="btn amber" data-pay="${esc(s.number)}" data-due="${s.due}">💰 Collect</button>` : ""}
        </div>
      </article>`).join("");
    wireStopButtons();
  } catch (e) {
    if (mine !== ROUTE_SEQ || opts.quiet) return;
    $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function wireStopButtons() {
  $("list").querySelectorAll("[data-call]").forEach((b) => {
    b.onclick = () => callCustomer(b.dataset.call);
  });
  $("list").querySelectorAll("[data-map]").forEach((b) => {
    b.onclick = () => window.open(
      "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(b.dataset.map),
      "_blank", "noopener");
  });
  $("list").querySelectorAll("[data-photo]").forEach((b) => {
    b.onclick = () => askPhoto(b.dataset.photo);
  });
  $("list").querySelectorAll("[data-pay]").forEach((b) => {
    b.onclick = () => askCollect(b.dataset.pay, parseFloat(b.dataset.due));
  });
}

/* Poora number maangne par hi milta hai (aur server uska record rakhta
   hai) — list mein hamesha masked rehta hai. */
async function callCustomer(number) {
  try {
    const r = await api(`/orders/${encodeURIComponent(number)}/call`);
    location.href = `tel:${r.phone}`;
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------- photo */

/* Camera ki photo 4-12 MB ki hoti hai. Usse jaisi ki taisi bhejna hi wo
 * "app atak gayi" wali dikkat thi: 2G/3G par do-teen minute, aur screen par
 * kuch bhi nahi.
 *
 * Yahan wo phone par hi chhoti kar di jati hai — 1600px, JPEG. 8 MB se
 * ~300 KB, yani bees-pachees guna kam data. Saboot ke liye itni saaf kaafi
 * hai (daag, phata hua, kitne peace — sab dikhta hai).
 *
 * Sab kuch async hai: createImageBitmap decode main thread se bahar karta
 * hai aur toBlob bhi rukta nahi, isliye UI chalti rehti hai. Kuch bhi kaam
 * na kare (purana browser, ajeeb format) to asli file chali jaati hai —
 * photo bhejna kabhi rukta nahi, bas bhaari padta hai.
 *
 * imageOrientation: "from-image" — bina iske phone ki photo server par
 * tedhi pahunchti hai, kyunki canvas EXIF ka ghumaav khud nahi lagata. */
const PHOTO_MAX_DIM = 1600;
const PHOTO_QUALITY = 0.72;
const PHOTO_SKIP_BELOW = 400 * 1024;   // itni chhoti photo waise hi theek hai

async function shrinkPhoto(file) {
  if (!/^image\/(jpeg|png|webp)$/i.test(file.type)) return file;
  if (file.size <= PHOTO_SKIP_BELOW) return file;
  if (typeof createImageBitmap !== "function") return file;
  let bmp = null;
  let canvas = null;
  try {
    try {
      bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch (e) {
      bmp = await createImageBitmap(file);   // purana browser: option nahi manta
    }
    const scale = Math.min(1, PHOTO_MAX_DIM / Math.max(bmp.width, bmp.height));
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(bmp, 0, 0, w, h);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", PHOTO_QUALITY));
    if (!blob || blob.size >= file.size) return file;   // bada ho gaya to rehne do
    return new File([blob], "photo.jpg", { type: "image/jpeg" });
  } catch (e) {
    return file;
  } finally {
    // Chhote phone par memory turant chhodna zaroori hai, warna doosri
    // photo par browser tab hi maar deta hai.
    if (bmp && bmp.close) bmp.close();
    if (canvas) { canvas.width = 0; canvas.height = 0; }
  }
}

/* fetch upload ka progress nahi deta — isliye XHR. Progress hi wo cheez hai
 * jo "atak gaya" ko "chal raha hai" bana deti hai. */
function uploadWithProgress(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.withCredentials = true;
    xhr.timeout = 120000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* empty body */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail) || `Error ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Network problem — check your signal"));
    xhr.ontimeout = () => reject(new Error("Took too long — try again on better signal"));
    xhr.onabort = () => reject(new Error("Upload cancelled"));
    xhr.send(form);
  });
}

const kb = (n) => (n >= 1024 * 1024 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB");

function askPhoto(number) {
  openModal(`<h3>${esc(number)} — add a photo</h3>
    <p style="color:#6b7280;font-size:13px;margin:0 0 10px">
      Proof of the item's condition — a stain, a tear, or how many pieces.
      If anyone later says it was not like that, this is the answer.</p>
    <input id="ph-file" type="file" accept="image/*" capture="environment">
    <div id="ph-prev" hidden></div>
    <label for="ph-note">Add a note (optional)</label>
    <input id="ph-note" type="text" maxlength="150" placeholder="e.g. stain on the collar">
    <div id="ph-bar" class="bar" hidden><i></i></div>
    <div class="err" id="ph-err"></div>
    <div class="btnrow">
      <button class="btn ghost" id="m-x">Not now</button>
      <button class="btn" id="m-ok">Send</button>
    </div>`);
  $("m-x").onclick = closeModal;

  let ready = null;        // chhoti ki hui file, pehle se taiyar
  let preparing = false;

  // Photo chunte hi chhoti karna shuru — bhejne ke waqt tak taiyar milegi.
  $("ph-file").onchange = async () => {
    const f = $("ph-file").files[0];
    ready = null;
    if (!f) { $("ph-prev").hidden = true; return; }
    preparing = true;
    $("ph-err").textContent = "";
    $("ph-prev").hidden = false;
    $("ph-prev").textContent = "Getting the photo ready…";
    const small = await shrinkPhoto(f);
    ready = small;
    preparing = false;
    $("ph-prev").textContent = small.size < f.size
      ? `Ready — ${kb(f.size)} made smaller to ${kb(small.size)}`
      : `Ready — ${kb(small.size)}`;
  };

  const send = async (btn) => {
    const chosen = $("ph-file").files[0];
    if (!chosen) { $("ph-err").textContent = "Choose a photo first."; return; }
    $("ph-err").textContent = "";
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.innerHTML = '<span class="spin"></span>';
    const bar = $("ph-bar");
    bar.hidden = false;
    const fill = bar.querySelector("i");
    fill.style.width = "2%";
    try {
      // choose ke turant baad Send dabaya ho to yahin ruk kar taiyar karo
      while (preparing) await new Promise((r) => setTimeout(r, 60));
      const file = ready || (await shrinkPhoto(chosen));
      const fd = new FormData();
      fd.append("photo", file);
      fd.append("note", $("ph-note").value.trim());
      await uploadWithProgress(
        `/staff/api/orders/${encodeURIComponent(number)}/photo`,
        fd,
        (p) => { fill.style.width = Math.max(2, Math.round(p * 100)) + "%"; },
      );
      closeModal();
      toast("📷 Photo added — the owner has been told");
    } catch (err) {
      // Fail hone par kaam khatam nahi hota: wahi photo, ek tap par dobara.
      bar.hidden = true;
      $("ph-err").textContent = err.message;
      btn.disabled = false;
      btn.innerHTML = "🔄 Try again";
      return;
    }
    btn.disabled = false;
    btn.innerHTML = label;
  };
  $("m-ok").onclick = (e) => send(e.currentTarget);
}
