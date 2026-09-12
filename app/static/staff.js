/* Kwik Klin — staff panel. Chhota, bina kisi library ke.
 *
 * Do usool poore file par lage hain:
 *
 * 1. UI kabhi khud tay nahi karta ki kaun kya kar sakta hai. Wo /me ke
 *    `features` aur `is_manager` se sirf DIKHATA hai; asli rok server par
 *    hai. Isliye panel aur API kabhi alag nahi kah sakte.
 *
 * 2. Ek screen, ek sawaal. "Kaam" screen ka sawaal hai "ab kya karun" —
 *    isliye usme pickup, dhulai, delivery aur assign kiye gaye task SAB
 *    ek hi list mein hain, chips se chhante hue. Pehle Route aur Work do
 *    alag tab the jinme aadhi cheezein dono jagah dikhti thin aur aadhi
 *    kahin nahi.
 */

const $ = (id) => document.getElementById(id);
let ME = null;
let NAV = "work";        // work | bills | new | team | me
let FILTER = "all";      // chips ki chuni hui value (har screen ki apni)
let WORK = [];           // kaam ki poori list (server se milaya hua)
let UNREAD = 0;

/* ─── plumbing ──────────────────────────────────────────────────────── */

async function api(path, opts = {}) {
  const res = await fetch(`/staff/api${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* khali body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, err = false, ms = 3200) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");

/* Button dabate hi spinner, phir kaam, phir jawab.
   Bina iske dheeme network par aadmi ko lagta hai click laga hi nahi aur
   wo dobara dabata hai — wahi kaam do baar chal jaata hai. */
async function busy(btn, fn) {
  if (!btn) return fn();
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>';
  try { await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = old; }
}

/* Ek delegated listener — har button par alag handler jodna padta tha,
 * aur modal ka HTML badalte hi wo wiring dobara karni padti thi.
 *
 * Inline onclick="..." yahan chal hi nahi sakta: page ki CSP
 * `script-src 'self'` hai, aur browser inline handler ko chup-chaap gira
 * deta hai — button dikhta hai, dabta hai, aur kuch nahi hota. (Show more
 * isi wajah se mara pada tha; browser mein chala kar hi pata chala.) */
const ACTIONS = {
  close: () => closeModal(),
  share: (n) => { closeModal(); shareBill(n); },
  thread: (c) => { closeModal(); openThread(c); },
  decide: (c) => { closeModal(); decideCancel(c); },
  photo: (n) => { closeModal(); askPhoto(n); },
  cancel: (c) => { closeModal(); askCancel(c); },
  more: (fn) => (fn === "loadWork" ? loadWork({ more: true }) : loadBills({ more: true })),
};
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) fn(el.dataset.arg);
});

function openModal(html) { $("modal-body").innerHTML = html; $("modal-ov").classList.add("open"); }
function closeModal() { $("modal-ov").classList.remove("open"); }
$("modal-ov").addEventListener("click", (e) => { if (e.target.id === "modal-ov") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* "2026-09-14" ko phone par padhna padta hai — "Aaj"/"Kal" ek nazar mein
   samajh aata hai. Beeti hui date laal, taaki late kaam chhupe nahi. */
function whenParts(iso) {
  if (!iso) return { top: "—", sub: "", late: false };
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  if (isNaN(d)) return { top: iso, sub: "", late: false };
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 864e5);
  if (diff === 0) return { top: "Aaj", sub: "", late: false };
  if (diff === 1) return { top: "Kal", sub: "", late: false };
  if (diff === -1) return { top: "Kal", sub: "beet gaya", late: true };
  const top = d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  if (diff < 0) return { top, sub: `${-diff} din late`, late: true };
  return { top, sub: d.toLocaleDateString("en-IN", { weekday: "short" }), late: false };
}

const ROLE = {
  WASHER: "Washerman", DELIVERY: "Delivery",
  // Senior aadmi — kaam bhi karta hai aur dekh-rekh bhi. Use sirf "Washerman"
  // likhna uske kaam ko chhota dikhata hai.
  SUPERVISOR: "Washerman / Manager", MANAGER: "Manager", ADMIN: "Owner",
};
const roleLabel = (r) => ROLE[r] || r;

/* ─── login ─────────────────────────────────────────────────────────── */

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
$("lg-phone").addEventListener("keydown", (e) => { if (e.key === "Enter") $("lg-pass").focus(); });
$("lg-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("lg-go").click(); });

/* ─── boot ──────────────────────────────────────────────────────────── */

async function start() {
  try {
    ME = await api("/me");
  } catch (e) {
    // Server ne SAAF mana kiya (401/402) tabhi login. Network hichki par
    // aadmi ko bahar nahi phenkte — wo bas dobara koshish kar sake.
    $("boot").hidden = true;
    $("login").hidden = false; $("app").hidden = true;
    if (e.status === 402) $("lg-err").textContent = e.message;
    else if (!e.status) $("lg-err").textContent = "Network problem — check your signal, then log in.";
    return;
  }
  $("boot").hidden = true;
  $("login").hidden = true; $("app").hidden = false;
  $("who").textContent = ME.name;
  $("whoRole").textContent = `${roleLabel(ME.role)} · ${ME.shop || ""}`;
  $("pwbanner").hidden = !ME.must_change_password;
  renderNav();
  go("work");
  loadToday();
  loadBell();
  startLive();
}

/* ─── role ke hisaab se nav ─────────────────────────────────────────── */
/* Har role ka pehla sawaal alag hai, isliye pehla tab bhi alag ho sakta
   hai — par sabke liye "Kaam" hi ghar hai. Bill alag jagah hai (pehle wo
   New-bill screen ke neeche chipka tha aur kisi ko milta hi nahi tha). */
function navItems() {
  const out = [["work", "🧺", "Work"]];
  if (ME.features.includes("billing")) {
    out.push(["bills", "🧾", "Bills"], ["new", "＋", "New"]);
  }
  if (ME.is_manager && ME.features.includes("staff_reports")) out.push(["team", "👥", "Team"]);
  out.push(["me", "👤", "You"]);
  return out;
}

function renderNav() {
  $("nav").innerHTML = navItems().map(([k, icon, label]) =>
    `<button data-nav="${k}" class="${NAV === k ? "on" : ""}"><i>${icon}</i>${label}</button>`).join("");
  $("nav").querySelectorAll("[data-nav]").forEach((b) => { b.onclick = () => go(b.dataset.nav); });
}

function go(nav) {
  NAV = nav;
  FILTER = nav === "bills" ? "all" : "all";
  $("q").value = "";
  $("searchrow").hidden = nav !== "bills";
  // Hisaab kaam ke baare mein hai — form aur profile par sirf jagah khata hai
  $("today").hidden = !(nav === "work" || nav === "bills");
  $("nav").querySelectorAll("[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === nav));
  if (nav === "work") loadWork();
  else if (nav === "bills") loadBills();
  else if (nav === "new") showNewBill();
  else if (nav === "team") showTeam();
  else showMe();
}

$("btn-refresh").onclick = (e) => {
  const b = e.currentTarget;
  b.classList.add("turn"); setTimeout(() => b.classList.remove("turn"), 650);
  refreshCurrent({ quiet: false });
  loadToday(); loadBell();
};
$("pw-open").onclick = changePw;
$("btn-bell").onclick = showBell;

function refreshCurrent(opts = {}) {
  if (NAV === "work") loadWork(opts);
  else if (NAV === "bills") loadBills(opts);
  else if (NAV === "team") showTeam(opts);
}

/* ─── aaj ka hisaab ─────────────────────────────────────────────────── */

async function loadToday() {
  try {
    const t = await api("/today");
    const cell = (label, value, cls = "") => `<div class="${cls}"><b>${value}</b>${label}</div>`;
    $("today").innerHTML =
      cell("to do", t.pending, "left") +
      cell("done today", t.done_today) +
      (t.can_collect ? cell("collected", money(t.collected_today), "cash") : "") +
      (t.shop_pending !== undefined ? cell("shop total", t.shop_pending) : "");
    $("today").hidden = !(NAV === "work" || NAV === "bills");
  } catch (e) {
    $("today").hidden = true;   // hisaab na mile to chup — kaam chalta rahe
  }
}

/* ─── notifications ─────────────────────────────────────────────────── */
/* Sirf ek cheez abhi: owner ke wo jawab jo maine nahi padhe. Badge tabhi
   kaam ka hai jab wo sach mein kuch naya bole; har cheez ka badge banate
   hi log dekhna band kar dete hain. */

async function loadBell() {
  try {
    const n = await api("/notifications");
    UNREAD = n.unread || 0;
  } catch (e) { UNREAD = 0; }
  $("bellN").hidden = UNREAD === 0;
  $("bellN").textContent = UNREAD > 9 ? "9+" : String(UNREAD);
}

async function showBell() {
  let n;
  try { n = await api("/notifications"); }
  catch (e) { toast(e.message, true); return; }
  if (!n.items.length) {
    openModal(`<h3>Nothing new</h3>
      <p class="said">Replies from the owner show up here.</p>
      <div class="btnrow"><button class="btn ghost" data-act="close">Got it</button></div>`);
    return;
  }
  openModal(`<h3>Replies</h3>
    <p class="said">${n.items.length} new</p>
    ${n.items.map((i) => `
      <div class="card inset">
        <div class="line1"><b>${esc(i.title)}</b><span class="sub">${esc(i.at)}</span></div>
        <div class="sub">${esc(i.text)}</div>
        <button class="btn ghost sm" data-act="thread" data-arg="${esc(i.code)}">Open</button>
      </div>`).join("")}
    <div class="btnrow"><button class="btn ghost" data-act="close">Close</button></div>`);
}

/* ─── Kaam: ek list, chips se chhanti ───────────────────────────────── */
/*
 * Pehle "Work" (tasks) aur "Route" (stops) do alag tab the. Dikkat: ek hi
 * order dono jagah dikh sakta tha, aur jis order par koi task nahi bana
 * wo sirf Route mein tha — to aadmi ek tab dekh kar samajhta tha ki uska
 * kaam khatam hai. Ab dono ek hi list mein aate hain, ek kram se, aur
 * chips se chhante jaate hain.
 *
 * Kram: pehle late, phir aaj, phir baaki. Wahi kram jisme aadmi kaam
 * karega — sabse upar wahi jo sabse pehle nipatna chahiye.
 */

const KIND = {
  Pickup:   { chip: "pickup",  spine: "pickup",  label: "Collect" },
  Delivery: { chip: "deliver", spine: "deliver", label: "Deliver" },
  Dhulai:   { chip: "wash",    spine: "wash",    label: "Washing" },
};

function workChips() {
  const n = (f) => WORK.filter((w) => f === "all" || w.chip === f).length;
  const out = [["all", "All"]];
  const kinds = [...new Set(WORK.map((w) => w.chip))];
  if (kinds.includes("pickup")) out.push(["pickup", "Collect"]);
  if (kinds.includes("wash")) out.push(["wash", "Washing"]);
  if (kinds.includes("deliver")) out.push(["deliver", "Deliver"]);
  if (kinds.includes("task")) out.push(["task", "Work"]);
  if (WORK.some((w) => w.late)) out.push(["late", "Late"]);
  return out.map(([v, label]) => {
    const c = v === "late" ? WORK.filter((w) => w.late).length : n(v);
    return `<button data-chip="${v}" class="${FILTER === v ? "on" : ""}">${label}<span class="n">${c}</span></button>`;
  }).join("");
}

let WORK_SEQ = 0, MORE_LEFT = false;
async function loadWork(opts = {}) {
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  const mine = ++WORK_SEQ;
  const skip = opts.more ? WORK.length : 0;
  let route = { stops: [], total: 0 }, tasks = { tasks: [], total: 0 };
  try {
    [route, tasks] = await Promise.all([
      api(`/route?limit=${PAGE}&offset=${skip}`).catch(() => ({ stops: [], total: 0 })),
      api(`/tasks?tab=mine&limit=${PAGE}&offset=${skip}`).catch(() => ({ tasks: [], total: 0 })),
    ]);
  } catch (e) {
    if (mine !== WORK_SEQ || opts.quiet) return;
    $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (mine !== WORK_SEQ || NAV !== "work") return;

  // Ek order do jagah se aa sakta hai (uska stop bhi hai, uspar task bhi).
  // Stop zyada kaam ka hai (usme address, due, phone sab hai), isliye task
  // usi row par nishaan ban kar chipak jaata hai — do rows nahi banti.
  // Is page ke rows alag banao, phir purane ke saath jodo. Pehle maine
  // ise ek hi array par likhne ki koshish ki thi aur logic samajh se
  // bahar chala gaya — do saaf hisse behtar hain.
  const page = [];
  const byOrder = new Map();
  for (const s of route.stops) {
    const k = KIND[s.kind] || { chip: "task", spine: "", label: s.kind };
    const w = whenParts(s.delivery);
    const item = {
      type: "stop", chip: k.chip, spine: w.late ? "late" : k.spine, kindLabel: k.label,
      number: s.number, who: s.customer, items: s.items, due: s.due,
      address: s.address, urgent: s.urgent, when: w, late: w.late, tasks: [],
    };
    byOrder.set(s.number, item);
    page.push(item);
  }
  for (const t of tasks.tasks) {
    if (t.status !== "OPEN") continue;
    const onOrder = t.order && byOrder.get(t.order.number);
    if (onOrder) { onOrder.tasks.push(t); continue; }
    const w = t.order ? whenParts(t.order.delivery)
      : t.age_hours < 1 ? { top: "Now", sub: "", late: false }
      : { top: `${t.age_hours}h`, sub: "waiting", late: t.age_hours > 24 };
    page.push({
      type: "task", chip: "task", spine: t.urgent || w.late ? "late" : "", kindLabel: "Job",
      number: t.order ? t.order.number : t.code, who: t.order ? t.order.customer : t.title,
      items: t.order ? t.order.items : "", due: t.order ? t.order.due : 0,
      // Bina order wale kaam par paisa hota hi nahi — na rakam dikhani hai
      // na "chukta" ka ✓, warna aadmi samajhta hai ki paisa aa gaya.
      noMoney: !t.order,
      // Title upar bold mein aa chuka hai; dobara neeche likhna sirf
      // shor hai. Neeche wahi jab order ka naam upar ho.
      urgent: t.urgent, when: w, late: !!w.late, tasks: [t],
      title: t.order ? t.title : "",
    });
  }
  // "Show more" jodta hai, badalta nahi. Ek hi number do baar na aaye —
  // page ke kinare par wahi order dono taraf ho sakta hai.
  const merged = opts.more ? WORK.concat(page) : page;
  const seen = new Set();
  WORK = merged.filter((w) => (seen.has(w.number) ? false : seen.add(w.number)));
  // Kaam ki list do jagah se banti hai (stops + tasks) aur ek hi order
  // dono mein ho sakta hai — isliye dono ke total jodna jhooth hai
  // ("60 of 539" jabki asli rows 300 hain). Yahan sirf itna jaanna hai ki
  // aur bacha hai ya nahi: dono mein se koi bhi poora nahi aaya to haan.
  MORE_LEFT = (route.stops.length >= PAGE) || (tasks.tasks.length >= PAGE);
  WORK.sort((a, b) => (b.late - a.late) || (b.urgent - a.urgent));
  $("chips").innerHTML = workChips();
  $("chips").querySelectorAll("[data-chip]").forEach((b) => {
    b.onclick = () => { FILTER = b.dataset.chip; $("chips").innerHTML = workChips();
      $("chips").querySelectorAll("[data-chip]").forEach((x) => { x.onclick = b.onclick; });
      renderWork(); };
  });
  renderWork();
}

function renderWork() {
  const rows = WORK.filter((w) =>
    FILTER === "all" ? true : FILTER === "late" ? w.late : w.chip === FILTER);
  if (!rows.length) {
    $("list").innerHTML = WORK.length
      ? `<div class="empty"><b>Nothing in this filter</b>Try another one.</div>`
      : `<div class="empty"><b>All clear 👏</b>New work shows up here.</div>`;
    return;
  }
  // "Show more" sirf tab jab chhaant lagi hi na ho — chip ke andar aadhi
  // list dikhana aur "aur hai" kehna jhooth hai.
  $("list").innerHTML = `<div class="reg">${rows.map(workRow).join("")}</div>`
    + (FILTER === "all" && MORE_LEFT
        ? `<div class="morebar"><span>${WORK.length} shown</span>
             <button class="btn ghost sm" data-act="more" data-arg="loadWork">Show more</button></div>`
        : "");
  wireRows();
}

/* Bill list ka "Show more". Ginti hamesha dikhti hai (30 of 108), taaki
   aadmi jaane ki aur kitna baaki hai — aur scroll karne ke bajaye search
   karna behtar hai ya nahi. */
function moreBar(shown, total, fn) {
  if (!total || shown >= total) {
    return total > PAGE
      ? `<div class="morebar"><span>All ${total} shown</span></div>` : "";
  }
  return `<div class="morebar">
    <span>${shown} of ${total}</span>
    <button class="btn ghost sm" data-act="more" data-arg="${fn}">Show more</button>
  </div>`;
}

function workRow(w) {
  const t = w.tasks[0];
  const asked = t && t.cancel_requested;
  const due = w.noMoney ? ""
    : w.due > 0 ? `<span class="amt due">${money(w.due)}</span>`
    : `<span class="amt paid-tick">✓</span>`;
  return `<article class="row" data-num="${esc(w.number)}">
    <div class="spine ${w.spine}"></div>
    <div class="when ${w.late ? "late" : ""}">
      <b>${esc(w.when.top)}</b>${w.when.sub ? `<span>${esc(w.when.sub)}</span>` : ""}
    </div>
    <div class="body">
      <div class="line1"><b>${w.urgent ? "🔴 " : ""}${esc(w.who)}</b>${due}</div>
      <div class="sub">
        <span class="tag ${w.chip === "task" ? "task" : w.spine || "wash"}">${esc(w.kindLabel)}</span>${esc(w.number)}${w.items ? " · " + esc(w.items) : ""}
      </div>
      ${w.title && w.type === "task" ? `<div class="sub note">${esc(w.title)}</div>` : ""}
      ${asked ? `<div class="sub mt-xs"><span class="tag late">Cancel maanga</span>${esc(t.cancel_reason || "")}</div>` : ""}
      ${w.address ? `<div class="sub mt-xs">📍 ${esc(w.address)}</div>` : ""}
      <div class="acts">
        ${w.type === "stop" ? `<button class="btn ghost sm" data-do="call">Call</button>` : ""}
        ${w.address ? `<button class="btn ghost sm" data-do="map" data-addr="${esc(w.address)}">Route</button>` : ""}
        ${t ? `<button class="btn go sm" data-do="done" data-code="${esc(t.code)}">Done</button>` : ""}
        ${t ? `<button class="btn ghost sm" data-do="ask" data-code="${esc(t.code)}">Ask</button>` : ""}
        ${ME.features.includes("cod_collection") && w.due > 0
          ? `<button class="btn money sm" data-do="pay" data-due="${w.due}">${money(w.due)} collect</button>` : ""}
        <button class="btn ghost sm" data-do="more">⋯</button>
      </div>
    </div>
  </article>`;
}

function wireRows() {
  $("list").querySelectorAll("[data-do]").forEach((b) => {
    b.onclick = () => {
      const num = b.closest("[data-num]").dataset.num;
      const d = b.dataset;
      if (d.do === "call") return callCustomer(num);
      if (d.do === "map") return window.open(
        "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(d.addr),
        "_blank", "noopener");
      if (d.do === "done") return askDone(d.code);
      if (d.do === "ask") return openThread(d.code);
      if (d.do === "pay") return askCollect(num, parseFloat(d.due));
      if (d.do === "more") return moreMenu(num);
    };
  });
}

function moreMenu(number) {
  const w = WORK.find((x) => x.number === number) || {};
  const t = (w.tasks || [])[0];
  openModal(`<h3>${esc(number)}</h3>
    <p class="said">${esc(w.who || "")}</p>
    <div class="btnrow stack">
      <button class="btn ghost" data-act="share" data-arg="${esc(number)}">🧾 Send bill on WhatsApp</button>
      <button class="btn ghost" data-act="photo" data-arg="${esc(number)}">📷 Add a photo</button>
      ${t && ME.features.includes("cancel_approval") && !t.cancel_requested
        ? `<button class="btn ghost" data-act="cancel" data-arg="${esc(t.code)}">🛑 Request cancel</button>` : ""}
      ${t && ME.is_manager && t.cancel_requested
        ? `<button class="btn danger" data-act="decide" data-arg="${esc(t.code)}">Decide on cancel</button>` : ""}
    </div>
    <div class="btnrow"><button class="btn ghost" data-act="close">Close</button></div>`);
}

/* ─── kaam par actions ──────────────────────────────────────────────── */

function askDone(code) {
  openModal(`<h3>${esc(code)} — done?</h3>
    <p class="said">Add a note if you need to, otherwise just confirm.</p>
    <textarea id="m-note" placeholder="Note (optional)"></textarea>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Not now</button>
      <button class="btn go" id="m-ok">Yes, done</button>
    </div>`);
  $("m-ok").onclick = (e) => busy(e.currentTarget, async () => {
    await api(`/tasks/${encodeURIComponent(code)}/done`, { method: "POST", body: { note: $("m-note").value.trim() } });
    closeModal(); toast(`${code} closed ✅`); loadWork(); loadToday();
  });
}

/* Sawaal-jawab ek thread mein — pehle sawaal owner ke WhatsApp par chala
   jaata tha aur staff ko na apna sawaal dikhta tha na jawab. */
async function openThread(code) {
  let th;
  try { th = await api(`/tasks/${encodeURIComponent(code)}/messages`); }
  catch (e) { toast(e.message, true); return; }
  loadBell();
  const msgs = th.messages.length
    ? `<div class="thread">${th.messages.map((m) => `
        <div class="msg ${m.who}">${esc(m.text)}<span class="at">${esc(m.who === "staff" ? "Aapne" : m.name)} · ${esc(m.at)}</span></div>`).join("")}</div>`
    : `<p class="said">No messages yet.</p>`;
  openModal(`<h3>${esc(th.title)}</h3>
    <p class="said">${esc(code)}</p>
    ${msgs}
    <label for="m-q">Ask the owner</label>
    <textarea id="m-q" placeholder="e.g. is the address right? how many items?"></textarea>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Close</button>
      <button class="btn go" id="m-send">Send</button>
    </div>`);
  $("m-send").onclick = (e) => {
    const text = $("m-q").value.trim();
    if (text.length < 2) { toast("Write your question first", true); return; }
    return busy(e.currentTarget, async () => {
      await api(`/tasks/${encodeURIComponent(code)}/ask`, { method: "POST", body: { text } });
      toast("Sent to the owner 🙏");
      openThread(code);
    });
  };
}

function askCancel(code) {
  openModal(`<h3>${esc(code)} — request a cancel?</h3>
    <p class="said">You cannot cancel this yourself. Write the reason and your manager will decide.</p>
    <textarea id="m-r" placeholder="e.g. customer refused"></textarea>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Not now</button>
      <button class="btn danger" id="m-ok">Send</button>
    </div>`);
  $("m-ok").onclick = (e) => {
    const reason = $("m-r").value.trim();
    if (reason.length < 3) { toast("Write the reason first", true); return; }
    return busy(e.currentTarget, async () => {
      await api(`/tasks/${encodeURIComponent(code)}/cancel-request`, { method: "POST", body: { reason } });
      closeModal(); toast("Sent to your manager"); loadWork();
    });
  };
}

function decideCancel(code) {
  const w = WORK.find((x) => (x.tasks || []).some((t) => t.code === code));
  const t = w && w.tasks.find((x) => x.code === code);
  openModal(`<h3>${esc(code)} — cancel request</h3>
    <p class="said">${esc((t && t.cancel_reason) || "")}</p>
    <div class="btnrow">
      <button class="btn ghost" id="m-no">No, keep it</button>
      <button class="btn danger" id="m-yes">Yes, cancel</button>
    </div>`);
  const send = (e, approve) => busy(e.currentTarget, async () => {
    await api(`/tasks/${encodeURIComponent(code)}/cancel-decide`, { method: "POST", body: { approve } });
    closeModal(); toast(approve ? "Cancelled" : "Cancel refused"); loadWork();
  });
  $("m-yes").onclick = (e) => send(e, true);
  $("m-no").onclick = (e) => send(e, false);
}

function askCollect(order, due) {
  // ₹250.50 due par "251" bharna server se "Only ₹250 is due" laata tha
  const dueStr = Number.isInteger(due) ? String(due) : due.toFixed(2);
  openModal(`<h3>${esc(order)} — payment received</h3>
    <p class="said">${money(due)} due</p>
    <label for="m-amt">Amount</label>
    <input id="m-amt" type="number" inputmode="decimal" value="${dueStr}" min="1" max="${dueStr}" step="0.01">
    <div class="btnrow">
      <button class="btn ghost" id="m-cash">💵 Cash</button>
      <button class="btn go" id="m-upi">📱 UPI</button>
    </div>
    <div class="btnrow"><button class="btn ghost" data-act="close">Not now</button></div>`);
  const send = (e, method) => {
    const amount = parseFloat($("m-amt").value);
    if (!(amount > 0)) { toast("Enter the amount", true); return; }
    if (amount > due + 0.01) { toast(`Only ${money(due)} is due`, true); return; }
    return busy(e.currentTarget, async () => {
      const r = await api(`/orders/${encodeURIComponent(order)}/collect`, { method: "POST", body: { amount, method } });
      closeModal(); toast(`${money(amount)} received ✅ — ${money(r.due)} left`);
      loadToday(); refreshCurrent({ quiet: true });
    });
  };
  $("m-cash").onclick = (e) => send(e, "cash");
  $("m-upi").onclick = (e) => send(e, "upi");
}

/* Poora number maangne par hi milta hai, aur server uska record rakhta
   hai — list mein hamesha masked rehta hai. */
async function callCustomer(number) {
  try {
    const r = await api(`/orders/${encodeURIComponent(number)}/call`);
    location.href = `tel:${r.phone}`;
  } catch (e) { toast(e.message, true); }
}

/* ─── Bill: apni jagah, apni chhaant ────────────────────────────────── */
/* Pehle bill ki list "Naya bill" screen ke neeche chipki thi — jo bill
   banane aaya wahi use dekh paata tha, aur dhoondhne ka koi rasta nahi
   tha. Ab alag tab: search + due/paid chips. */

let BILLS = [], BILLS_TOTAL = 0, BILL_SEQ = 0, Q_TIMER = null;
// Ek page mein kitne. Chhota isliye ki 3G par pehli screen jaldi aaye;
// baaki "Show more" par. 800 order wali dukaan par poori list bhejna
// 79 KB ka payload tha jo har 30 second par dobara utarta tha.
const PAGE = 30;

$("q").addEventListener("input", () => {
  clearTimeout(Q_TIMER);
  // Har akshar par server nahi jaate — 300ms chuppi = ek request.
  Q_TIMER = setTimeout(() => { if (NAV === "bills") loadBills(); }, 300);
});

async function loadBills(opts = {}) {
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  const mine = ++BILL_SEQ;
  const p = new URLSearchParams();
  const q = $("q").value.trim();
  if (q) p.set("q", q);
  if (FILTER === "due" || FILTER === "paid") p.set("pay", FILTER);
  if (FILTER === "mine") p.set("mine", "1");
  p.set("limit", String(PAGE));
  p.set("offset", String(opts.more ? BILLS.length : 0));
  try {
    const r = await api("/bills?" + p.toString());
    // "aur dikhayein" purani list ke aage jodta hai, badalta nahi
    BILLS = opts.more ? BILLS.concat(r.bills) : r.bills;
    BILLS_TOTAL = r.total;
  } catch (e) {
    if (mine !== BILL_SEQ || opts.quiet) return;
    $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (mine !== BILL_SEQ || NAV !== "bills") return;

  const chips = [["all", "All"], ["due", "Unpaid"], ["paid", "Paid"]];
  if (ME.is_manager) chips.push(["mine", "Mine"]);
  $("chips").innerHTML = chips.map(([v, l]) =>
    `<button data-chip="${v}" class="${FILTER === v ? "on" : ""}">${l}</button>`).join("");
  $("chips").querySelectorAll("[data-chip]").forEach((b) => {
    b.onclick = () => { FILTER = b.dataset.chip; loadBills(); };
  });

  if (!BILLS.length) {
    $("list").innerHTML = q
      ? `<div class="empty"><b>No match</b>Nothing found for “${esc(q)}”. Try a bill number or a name.</div>`
      : `<div class="empty"><b>No bills yet</b>${ME.is_manager
          ? "No bills in the shop in the last 14 days."
          : "Bills you make show up here."}</div>`;
    return;
  }
  $("list").innerHTML = `<div class="reg">${BILLS.map(billRow).join("")}</div>`
    + moreBar(BILLS.length, BILLS_TOTAL, "loadBills");
  $("list").querySelectorAll("[data-bill]").forEach((b) => {
    b.onclick = () => {
      const num = b.closest("[data-num]").dataset.num;
      if (b.dataset.bill === "share") return shareBill(num);
      if (b.dataset.bill === "pay") return askCollect(num, parseFloat(b.dataset.due));
    };
  });
}

function billRow(b) {
  const w = whenParts(b.delivery);
  const paid = !(b.due > 0);
  return `<article class="row" data-num="${esc(b.number)}">
    <div class="spine ${paid ? "ready" : w.late ? "late" : "deliver"}"></div>
    <div class="when ${w.late && !paid ? "late" : ""}">
      <b>${esc(w.top)}</b>${w.sub ? `<span>${esc(w.sub)}</span>` : ""}
    </div>
    <div class="body">
      <div class="line1">
        <b>${esc(b.customer)}</b>
        <span class="amt ${paid ? "" : "due"}">${paid ? money(b.total) : money(b.due) + " due"}</span>
      </div>
      <div class="sub">${esc(b.number)} · ${esc(b.created)}${b.items ? " · " + esc(b.items) : ""}</div>
      <div class="acts">
        <button class="btn ghost sm" data-bill="share">🧾 Send</button>
        ${ME.features.includes("cod_collection") && b.due > 0
          ? `<button class="btn money sm" data-bill="pay" data-due="${b.due}">Collect</button>` : ""}
      </div>
    </div>
  </article>`;
}

/* ─── bill WhatsApp par ─────────────────────────────────────────────── */
/* Dukaan ka WhatsApp API juda ho ya na ho, staff ke apne phone ka WhatsApp
   to hai. wa.me link mein number aur bill dono bhare hote hain.
   Link asli <a> hai, window.open nahi: await ke baad window.open ko popup
   blocker rok deta hai; <a> par tap khud user ka gesture hai. */

function waUrl(phone, text) {
  let d = String(phone || "").replace(/\D/g, "").replace(/^0+/, "");
  if (d.length === 10) d = "91" + d;
  return `https://wa.me/${d}?text=${encodeURIComponent(text)}`;
}
let SHARE_TEXT = "";

async function shareBill(number) {
  let r;
  try { r = await api(`/orders/${encodeURIComponent(number)}/receipt`); }
  catch (e) { toast(e.message, true); return; }
  SHARE_TEXT = r.text;
  openModal(`<h3>Send the bill</h3>
    <p class="said">WhatsApp opens with the bill already written — just press send. To ${esc(r.name)}.</p>
    <pre class="sharetext">${esc(r.text)}</pre>
    <div class="btnrow">
      <a class="btn go" href="${esc(waUrl(r.phone, r.text))}" target="_blank" rel="noopener" data-act="close">📲 Open WhatsApp</a>
      <button class="btn ghost" id="m-copy">Copy</button>
    </div>
    <div class="btnrow"><button class="btn ghost" data-act="close">Close</button></div>`);
  $("m-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(SHARE_TEXT); toast("Copied"); }
    catch (e) { toast("Could not copy — select the text above", true); }
  };
}

/* ─── naya bill ─────────────────────────────────────────────────────── */
/* Daam staff nahi bharta — rate card se aata hai, wahi jo WhatsApp wale
   bill par lagta hai. Do jagah do hisaab kabhi nahi. */

let RATES = null, CART = [], PICKED = "";

async function showNewBill() {
  $("chips").innerHTML = "";
  if (!ME.features.includes("billing")) {
    $("list").innerHTML = `<div class="empty"><b>Billing is not in this plan</b>Ask the owner to upgrade.</div>`;
    return;
  }
  if (RATES === null) {
    $("list").innerHTML = `<div class="empty">Loading the rate card…</div>`;
    try { RATES = await api("/rates"); }
    catch (e) { $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  }
  const services = [...new Set(RATES.map((r) => r.service))];
  if (!services.length) {
    $("list").innerHTML = `<div class="empty"><b>The rate card is empty</b>
      The owner adds prices in Settings → Rate card. Bills need prices.</div>`;
    return;
  }
  $("list").innerHTML = `
    <div class="card">
      <h3>Customer</h3>
      <label for="b-name">Name</label>
      <div class="ac-wrap">
        <input id="b-name" type="text" placeholder="Start typing a name" maxlength="60" autocomplete="off">
        <div class="acp" id="b-ac"></div>
      </div>
      <div id="b-picked" class="picked" hidden></div>
      <label for="b-phone">Mobile number</label>
      <input id="b-phone" type="tel" inputmode="numeric" placeholder="98xxxxxxxx" maxlength="15">
    </div>

    <div class="card">
      <h3>Items</h3>
      <div class="frow">
        <div>
          <label for="b-svc">Service</label>
          <select id="b-svc">${services.map((s) => `<option>${esc(s)}</option>`).join("")}</select>
        </div>
        <div>
          <label for="b-item">Item</label>
          <select id="b-item"></select>
        </div>
      </div>
      <div class="addrow">
        <div class="qty">
          <label for="b-qty">Qty</label>
          <input id="b-qty" type="number" inputmode="decimal" value="1" min="0.1" step="0.5">
        </div>
        <button class="btn ghost" id="b-add">Add</button>
      </div>
      <div id="b-cart"></div>
    </div>

    <div class="card" id="b-pay" hidden>
      <h3>Payment</h3>
      <label for="b-adv">Received now (₹)</label>
      <input id="b-adv" type="number" inputmode="decimal" value="0" min="0">
      <button class="btn go wide" id="b-save">Create bill</button>
    </div>`;

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
    const same = CART.find((x) => x.service === svc && x.garment === item);
    if (same) same.qty += qty; else CART.push({ service: svc, garment: item, qty, rate });
    $("b-qty").value = "1";
    renderCart();
  };
  $("b-save").onclick = (e) => saveBill(e.currentTarget);
  wireCustomerSearch();
  renderCart();
}

function renderCart() {
  const box = $("b-cart");
  if (!box) return;
  $("b-pay").hidden = CART.length === 0;
  if (!CART.length) { box.innerHTML = ""; return; }
  const total = CART.reduce((s, i) => s + i.qty * i.rate, 0);
  box.innerHTML = `<div class="cart">${CART.map((i, n) => `
    <div class="cartrow">
      <span>${esc(i.garment)} <small>${esc(i.service)}</small> × ${i.qty}</span>
      <b>${money(i.qty * i.rate)}
        <button class="rm" data-rm="${n}" aria-label="Remove">✕</button></b>
    </div>`).join("")}
    <div class="total"><span>Total</span><span>${money(total)}</span></div></div>`;
  box.querySelectorAll("[data-rm]").forEach((b) => {
    b.onclick = () => { CART.splice(parseInt(b.dataset.rm), 1); renderCart(); };
  });
}

/* Naam likhte hi purana grahak. Counter par sabse badi galti yahi hoti
   thi: wahi grahak har baar naye number ke saath dobara ban jaata tha
   (ek digit idhar-udhar), aur uska purana hisaab kahin aur pada rehta.
   Chunne par number NAHI dikhta — bill server par `ref` se banta hai. */
let AC_TIMER = null, AC_SEQ = 0, AC_HITS = [];

function wireCustomerSearch() {
  const input = $("b-name"), box = $("b-ac");
  if (!input || !box) return;
  input.oninput = () => {
    PICKED = ""; $("b-picked").hidden = true; $("b-phone").disabled = false;
    const q = input.value.trim();
    clearTimeout(AC_TIMER);
    if (q.length < 2) { box.innerHTML = ""; return; }
    const mine = ++AC_SEQ;
    AC_TIMER = setTimeout(async () => {
      let hits = [];
      try { hits = await api(`/customers/search?q=${encodeURIComponent(q)}`); }
      catch (e) { box.innerHTML = ""; return; }   // sujhaav suvidha hai — fail ho to chup
      if (mine !== AC_SEQ) return;                // dheema jawab purani list na dikhaye
      AC_HITS = hits;
      box.innerHTML = hits.length
        ? hits.map((c, i) => `<div data-pick="${i}">${esc(c.name || "No name")} · ${esc(c.phone_masked)}</div>`).join("")
        : `<div class="none">New customer — enter the number below</div>`;
      box.querySelectorAll("[data-pick]").forEach((row) => {
        row.onclick = () => {
          const c = AC_HITS[parseInt(row.dataset.pick, 10)];
          if (!c) return;
          PICKED = c.ref;
          input.value = c.name || "";
          box.innerHTML = "";
          $("b-phone").value = ""; $("b-phone").disabled = true;
          $("b-picked").hidden = false;
          $("b-picked").textContent = `✓ ${c.name || "Customer"} · ${c.phone_masked}`;
        };
      });
    }, 250);
  };
}
// Bahar tap = sujhaav band. Ek hi baar register — pehle har visit par
// naya listener judta tha aur das chakkar ke baad das chal rahe the.
document.addEventListener("click", (e) => {
  const box = $("b-ac");
  if (box && !e.target.closest(".ac-wrap")) box.innerHTML = "";
});

async function saveBill(btn) {
  const phone = $("b-phone").value.trim();
  if (!PICKED && phone.replace(/\D/g, "").length < 10) return toast("Enter the full number", true);
  if (!CART.length) return toast("Add items first", true);
  await busy(btn, async () => {
    const r = await api("/bills", {
      method: "POST",
      body: {
        customer_ref: PICKED,
        customer_phone: PICKED ? "" : phone,
        customer_name: $("b-name").value.trim(),
        advance: parseFloat($("b-adv").value) || 0,
        items: CART.map((i) => ({ service: i.service, garment: i.garment, qty: i.qty })),
      },
    });
    CART = []; PICKED = "";
    toast(`${r.order_number} created — ${money(r.total)}`, false, 5000);
    showNewBill();
    loadToday();
    // Grahak saamne khada hai — bill turant bhej dein
    shareBill(r.order_number);
  });
}

/* ─── team (manager) ────────────────────────────────────────────────── */

async function showTeam(opts = {}) {
  $("chips").innerHTML = "";
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  let rows;
  try { rows = await api("/team"); }
  catch (e) { $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (NAV !== "team") return;
  if (!rows.length) { $("list").innerHTML = `<div class="empty"><b>No staff yet</b>The owner adds them from the dashboard.</div>`; return; }
  $("list").innerHTML = `<div class="reg">${rows.map((s) => `
    <article class="row">
      <div class="spine ${s.open ? "deliver" : "ready"}"></div>
      <div class="when"><b>${s.open}</b><span>open</span></div>
      <div class="body">
        <div class="line1"><b>${esc(s.name)}</b><span class="amt">${s.done_24h} done</span></div>
        <div class="sub">${esc(roleLabel(s.role))} · ${esc(s.phone_masked)}${s.has_login ? "" : " · no panel login"}</div>
      </div>
    </article>`).join("")}</div>`;
}

/* ─── main (profile) ────────────────────────────────────────────────── */

function showMe() {
  $("chips").innerHTML = "";
  $("list").innerHTML = `
    <div class="card">
      <h3>${esc(ME.name)}</h3>
      <div class="kv"><span>Role</span><b>${esc(roleLabel(ME.role))}</b></div>
      <div class="kv"><span>Mobile</span><b>${esc(ME.phone)}</b></div>
      <div class="kv"><span>Shop</span><b>${esc(ME.shop || "—")}</b></div>
      <div class="kv"><span>Plan</span><b>${esc(ME.plan)}</b></div>
      <p class="hint">Your role and access are set by the owner.</p>
    </div>
    <div class="card">
      <button class="btn ghost wide mt0" id="p-pw">Change password</button>
      <button class="btn danger wide" id="p-out">Logout</button>
    </div>`;
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
      <button class="btn ghost" data-act="close">Not now</button>
      <button class="btn go" id="m-ok">Save</button>
    </div>`);
  document.querySelectorAll("[data-eye]").forEach((b) => {
    b.onclick = () => {
      const i = $(b.dataset.eye);
      i.type = i.type === "password" ? "text" : "password";
      b.textContent = i.type === "password" ? "👁" : "🙈";
    };
  });
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

/* ─── photo ─────────────────────────────────────────────────────────── */
/*
 * Camera ki photo 4-12 MB ki hoti hai. Usse jaisi ki taisi bhejna hi wo
 * "app atak gayi" wali dikkat thi: 2G/3G par do-teen minute aur screen par
 * kuch bhi nahi. Yahan wo phone par hi chhoti ho jaati hai — 1600px JPEG,
 * 8 MB se ~300 KB. Saboot ke liye itni saaf kaafi hai (daag, phata hua,
 * kitne peace — sab dikhta hai).
 *
 * imageOrientation: "from-image" — bina iske phone ki photo server par
 * tedhi pahunchti hai, kyunki canvas EXIF ka ghumaav khud nahi lagata.
 */
const PHOTO_MAX = 1600, PHOTO_Q = 0.72, PHOTO_SKIP = 400 * 1024;

async function shrinkPhoto(file) {
  if (!/^image\/(jpeg|png|webp)$/i.test(file.type)) return file;
  if (file.size <= PHOTO_SKIP) return file;
  if (typeof createImageBitmap !== "function") return file;
  let bmp = null, canvas = null;
  try {
    try { bmp = await createImageBitmap(file, { imageOrientation: "from-image" }); }
    catch (e) { bmp = await createImageBitmap(file); }   // purana browser
    const scale = Math.min(1, PHOTO_MAX / Math.max(bmp.width, bmp.height));
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(bmp, 0, 0, w, h);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", PHOTO_Q));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], "photo.jpg", { type: "image/jpeg" });
  } catch (e) {
    return file;      // kuch bhi kaam na kare to asli file — photo rukti nahi
  } finally {
    // Chhote phone par memory turant chhodna zaroori hai, warna doosri
    // photo par browser tab hi maar deta hai.
    if (bmp && bmp.close) bmp.close();
    if (canvas) { canvas.width = 0; canvas.height = 0; }
  }
}

/* fetch upload ka progress nahi deta — isliye XHR. Progress hi wo cheez
   hai jo "atak gaya" ko "chal raha hai" bana deti hai. */
function uploadWithProgress(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.withCredentials = true;
    xhr.timeout = 120000;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* khali */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail) || `Error ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Network problem — check your signal"));
    xhr.ontimeout = () => reject(new Error("Took too long — try again on better signal"));
    xhr.onabort = () => reject(new Error("Upload cancelled"));
    xhr.send(form);
  });
}

const kb = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB");

function askPhoto(number) {
  openModal(`<h3>${esc(number)} — add a photo</h3>
    <p class="said">Proof of the item\u2019s condition — a stain, a tear, or how many pieces.
      If anyone later says it was not like that, this is the answer.</p>
    <input id="ph-file" type="file" accept="image/*" capture="environment">
    <div id="ph-prev" hidden></div>
    <label for="ph-note">Note (optional)</label>
    <input id="ph-note" type="text" maxlength="150" placeholder="e.g. stain on the collar">
    <div id="ph-bar" class="bar" hidden><i></i></div>
    <div class="err" id="ph-err"></div>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Not now</button>
      <button class="btn go" id="m-ok">Send</button>
    </div>`);

  let ready = null, preparing = false;

  // Photo chunte hi chhoti karna shuru — bhejne ke waqt tak taiyar milegi
  $("ph-file").onchange = async () => {
    const f = $("ph-file").files[0];
    ready = null;
    if (!f) { $("ph-prev").hidden = true; return; }
    preparing = true;
    $("ph-err").textContent = "";
    $("ph-prev").hidden = false;
    $("ph-prev").textContent = "Getting the photo ready…";
    const small = await shrinkPhoto(f);
    ready = small; preparing = false;
    $("ph-prev").textContent = small.size < f.size
      ? `Ready — ${kb(f.size)} made smaller to ${kb(small.size)}` : `Ready — ${kb(small.size)}`;
  };

  $("m-ok").onclick = async (e) => {
    const btn = e.currentTarget;
    const chosen = $("ph-file").files[0];
    if (!chosen) { $("ph-err").textContent = "Choose a photo first."; return; }
    $("ph-err").textContent = "";
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.innerHTML = '<span class="spin"></span>';
    const bar = $("ph-bar"); bar.hidden = false;
    const fill = bar.querySelector("i"); fill.style.width = "2%";
    try {
      // choose ke turant baad Send dabaya ho to yahin ruk kar taiyar karo
      while (preparing) await new Promise((r) => setTimeout(r, 60));
      const file = ready || (await shrinkPhoto(chosen));
      const fd = new FormData();
      fd.append("photo", file);
      fd.append("note", $("ph-note").value.trim());
      await uploadWithProgress(
        `/staff/api/orders/${encodeURIComponent(number)}/photo`, fd,
        (p) => { fill.style.width = Math.max(2, Math.round(p * 100)) + "%"; },
      );
      closeModal();
      toast("Photo added — the owner has been told");
    } catch (err) {
      // Fail hone par kaam khatam nahi: wahi photo, ek tap par dobara.
      bar.hidden = true;
      $("ph-err").textContent = err.message;
      btn.disabled = false;
      btn.innerHTML = "Try again";
      return;
    }
    btn.disabled = false; btn.innerHTML = label;
  };
}

/* ─── live updates ──────────────────────────────────────────────────── */
/*
 * Manager ne kaam badla, owner ne jawab diya, kisi ne paisa jama kiya —
 * wo staff ke phone par turant dikhna chahiye, bina refresh ke.
 *
 * SSE, WebSocket nahi: khabar sirf server se phone tak jaati hai, aur
 * connection tootne par browser khud dobara jud jaata hai — jo mobile
 * network par sabse zaroori baat hai. Screen chhupi ho to kuch nahi
 * maangte: chalta hua stream pichhe se battery aur data dono khaata hai.
 */
let LIVE = null, LIVE_TIMER = null, LIVE_POLL = null, LIVE_SEEN = 0;
const SILENCE_MS = 60000;   // server har 20s par dhadkan bhejta hai

const liveProven = () => LIVE_SEEN > 0 && Date.now() - LIVE_SEEN < SILENCE_MS;
const liveSeen = () => { LIVE_SEEN = Date.now(); };

function tick() {
  if (document.hidden) return;
  // Pichhe se aaya refresh list ko "Loading…" se NAHI badalta —
  // jo card aadmi padh raha hai wo uske haath se nikal jaata tha.
  refreshCurrent({ quiet: true });
  loadToday();
  loadBell();
  if (!LIVE) startLive();     // stream toot gayi thi to dobara jodo
}
function soon() { clearTimeout(LIVE_TIMER); LIVE_TIMER = setTimeout(tick, 400); }

document.addEventListener("visibilitychange", () => { if (!document.hidden) soon(); });

function startLive() {
  // Poochhna hamesha chalu — bas jab tak stream khud ko sabit kar rahi hai
  // tab tak chup. Pehle fallback tabhi chalta tha jab onerror teen baar
  // aaye, par 401 par browser dobara judta hi nahi (onerror ek hi baar) —
  // to na stream chalti thi na polling.
  if (!LIVE_POLL) {
    LIVE_POLL = setInterval(() => { if (!liveProven()) tick(); }, 30000);
  }
  if (!("EventSource" in window) || LIVE) return;
  try { LIVE = new EventSource("/staff/api/events", { withCredentials: true }); }
  catch (e) { return; }
  LIVE.addEventListener("ready", liveSeen);
  LIVE.addEventListener("ping", liveSeen);
  LIVE.onmessage = () => { liveSeen(); soon(); };
  LIVE.onerror = (e) => {
    // Source event se, `LIVE` se nahi — neeche wo null ho jaata hai.
    const src = e && e.target;
    if (src && src.readyState === EventSource.CLOSED) {
      // Browser khud nahi jodega (401/5xx) — agla poll dobara koshish karega
      LIVE_SEEN = 0; LIVE = null;
    }
  };
}

/* ─── phone par install ─────────────────────────────────────────────── */
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
  navigator.serviceWorker.register("/staff/sw.js").catch(() => { /* app phir bhi chalegi */ });
}

start();
