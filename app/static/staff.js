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
let NAV = "work";        // work | bills | new | exp | team | me
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
    // 422 par detail ek list hoti hai — "[object Object]" nahi, pehla sandesh
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = (detail[0] && detail[0].msg || "").replace(/^Value error, /, "");
    const err = new Error(detail || `Error ${res.status}`);
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
  detail: (n) => { closeModal(); orderDetailModal(n); },
  jobdone: (c) => { closeModal(); askDone(c); },
  decide: (c) => { closeModal(); decideCancel(c); },
  photo: (n) => { closeModal(); askPhoto(n); },
  message: (n) => { closeModal(); const w = WORK.find((x) => x.number === n); messageMenu(n, Number((w && w.due) || 0)); },
  cancel: (c) => { closeModal(); askCancel(c); },
  more: (fn) => (fn === "loadWork" ? loadWork({ more: true }) : loadBills({ more: true })),
};
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) fn(el.dataset.arg);
});

/* ─── Android back button ────────────────────────────────────────────
 * Pehle back dabate hi PWA band ho jaata tha (history mein ek hi entry).
 * Ab: har screen (go) aur har popup (openModal) history mein ek entry
 * banata hai — back se popup band, phir pichhli screen, aur sabse pehli
 * screen par "Exit?" Yes/No. */
const modalOpen = () => $("modal-ov").classList.contains("open");
function openModal(html) {
  if (!modalOpen() && !(history.state && history.state.kk === "modal")) history.pushState({ kk: "modal" }, "");
  $("modal-body").innerHTML = html; $("modal-ov").classList.add("open");
}
function closeModal() { $("modal-ov").classList.remove("open"); }   // history entry rehti hai; agla popup use reuse karta hai, back use kha jaata hai
$("modal-ov").addEventListener("click", (e) => { if (e.target.id === "modal-ov") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
let EXITING = false;
function askExit() {
  history.pushState({ kk: "nav", nav: NAV }, "");        // root par ruko, app band na ho
  openModal(`<h3>Exit the app?</h3>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">No</button>
      <button class="btn go" id="m-exit">Yes, exit</button>
    </div>`);
  $("m-exit").onclick = () => { EXITING = true; history.go(-3); setTimeout(() => { try { window.close(); } catch (e) { /* browser mana kare to kuch nahi */ } }, 400); };
}
window.addEventListener("popstate", () => {
  const st = history.state || {};
  if (modalOpen()) { closeModal(); if (st.kk !== "root") return; }
  if (st.kk === "root") { if (EXITING) { history.back(); return; } askExit(); return; }
  if (st.kk === "nav" && st.nav && st.nav !== NAV) go(st.nav, false);
});

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

let BOOT = null;          // /boot ka jawab — pehla loadWork/loadToday/loadBell isi se, bina naye request ke
async function start() {
  try {
    BOOT = await api("/boot");
    ME = BOOT.me;
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
  // History ki jad: root -> pehli screen. Back yahan tak aaye to Exit poochho.
  if (!(history.state && history.state.kk)) { history.replaceState({ kk: "root" }, ""); history.pushState({ kk: "nav", nav: "work" }, ""); }
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
/* Plan `billing` kehta hai ki DUKAAN bill bana sakti hai; `can_bill` kehta
   hai ki YE aadmi bana sakta hai (washerman nahi). Dono server se aate hain
   aur dono ek hi jagah se padhe jaate hain — isliye tab dikhna aur API ka
   maanna kabhi alag nahi ho sakte. */
const canBill = () => ME.can_bill && ME.features.includes("billing");

function navItems() {
  const out = [["work", "🧺", "Work"]];
  if (canBill()) {
    out.push(["bills", "🧾", "Bills"], ["new", "＋", "New"]);
  }
  if (ME.can_expense) out.push(["exp", "💸", "Expense"]);
  if (ME.is_manager && ME.features.includes("staff_reports")) out.push(["team", "👥", "Team"]);
  out.push(["me", "👤", "You"]);
  return out;
}

function renderNav() {
  $("nav").innerHTML = navItems().map(([k, icon, label]) =>
    `<button data-nav="${k}" class="${NAV === k ? "on" : ""}"><i>${icon}</i>${label}</button>`).join("");
  $("nav").querySelectorAll("[data-nav]").forEach((b) => { b.onclick = () => go(b.dataset.nav); });
}

async function openPickupBill(code) {
  try {
    const ctx = await api(`/tasks/${encodeURIComponent(code)}/bill-context`);
    PICKUP_BILL_CONTEXT = ctx;
    go("new");
  } catch (e) {
    toast(e.message, true);
  }
}

function go(nav, push = true) {
  if (push && NAV !== nav) history.pushState({ kk: "nav", nav }, "");
  NAV = nav;
  FILTER = nav === "bills" ? "all" : "all";
  $("q").value = "";
  $("searchrow").hidden = nav !== "bills";
  // Hisaab kaam ke baare mein hai — form aur profile par sirf jagah khata hai
  $("today").hidden = !(nav === "work" || nav === "bills");
  $("latebar").hidden = true;        // paintLate() isse wapas laayega
  $("nav").querySelectorAll("[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === nav));
  if (nav === "work") loadWork();
  else if (nav === "bills") loadBills();
  else if (nav === "new") showNewBill();
  else if (nav === "exp") showExpenses();
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
    const t = (BOOT && BOOT.today) || await api("/today");
    if (BOOT) BOOT.today = null;
    const cell = (label, value, cls = "") => `<div class="${cls}"><b>${value}</b>${label}</div>`;
    $("today").innerHTML =
      cell("to do", t.pending, "left") +
      cell("done today", t.done_today) +
      (t.can_collect ? cell("collected", money(t.collected_today), "cash") : "") +
      (t.shop_pending !== undefined ? cell("shop total", t.shop_pending) : "");
    $("today").hidden = !(NAV === "work" || NAV === "bills");
    LATE_N = t.late || 0;
    paintLate();
  } catch (e) {
    $("today").hidden = true;   // hisaab na mile to chup — kaam chalta rahe
    $("latebar").hidden = true;
  }
}

/* Beeta hua kaam khud bolna chahiye.
 *
 * Late order list mein pehle se upar aata hai aur uski date laal hoti hai,
 * par wo tabhi dikhta hai jab aadmi app KHOLE aur neeche padhe. Aur "Late"
 * chip baaki chips jaisa hi lagta hai — usme chubhan nahi hai.
 *
 * Ginti server se aati hai, WORK se nahi: panel ke paas sirf pehla page
 * hota hai, to bees late par bhi banner "2" kehta.
 */
let LATE_N = 0;

function paintLate() {
  const bar = $("latebar");
  // Sirf Kaam wale screen par. Bill banate waqt ye dhyan todta hai, aur
  // filter bhi usi screen ka hai jispar ye le jaata hai.
  if (NAV !== "work" || LATE_N < 1) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.classList.toggle("on", FILTER === "late");
  bar.innerHTML = FILTER === "late"
    ? `<span>⏰ ${LATE_N} late — sab dikha rahe hain</span><b>Wapas</b>`
    : `<span>⏰ ${LATE_N} kaam late ${LATE_N === 1 ? "hai" : "hain"}</span><b>Dekhein</b>`;
}

$("latebar").onclick = () => {
  FILTER = FILTER === "late" ? "all" : "late";
  $("chips").innerHTML = workChips();
  paintLate();
  renderWork();
};

/* ─── notifications ─────────────────────────────────────────────────── */
/* Sirf ek cheez abhi: owner ke wo jawab jo maine nahi padhe. Badge tabhi
   kaam ka hai jab wo sach mein kuch naya bole; har cheez ka badge banate
   hi log dekhna band kar dete hain. */

async function loadBell() {
  try {
    const n = (BOOT && BOOT.notifications) || await api("/notifications");
    if (BOOT) BOOT.notifications = null;
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
  Done:     { chip: "done",    spine: "",        label: "Done" },
};
const inDone = () => FILTER === "done";

function workChips() {
  const n = (f) => WORK.filter((w) => f === "all" || w.chip === f).length;
  const out = [["all", "All"]];
  const kinds = [...new Set(WORK.map((w) => w.chip))];
  if (kinds.includes("pickup")) out.push(["pickup", "Collect"]);
  if (kinds.includes("wash")) out.push(["wash", "Washing"]);
  if (kinds.includes("deliver")) out.push(["deliver", "Deliver"]);
  if (kinds.includes("task")) out.push(["task", "Work"]);
  if (WORK.some((w) => w.late)) out.push(["late", "Late"]);
  // "Done" hamesha: nipta hua kaam (7 din) yahan milta hai, gayab nahi hota
  out.push(["done", "Done"]);
  return out.map(([v, label]) => {
    const c = inDone() ? (v === "done" ? WORK.length : null)
      : v === "done" ? null : v === "late" ? WORK.filter((w) => w.late).length : n(v);
    return `<button data-chip="${v}" class="${FILTER === v ? "on" : ""}">${label}${c === null ? "" : `<span class="n">${c}</span>`}</button>`;
  }).join("");
}

let WORK_SEQ = 0, MORE_LEFT = false;
async function loadWork(opts = {}) {
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  const mine = ++WORK_SEQ;
  const skip = opts.more ? WORK.length : 0;
  let route = { stops: [], total: 0 }, tasks = { tasks: [], total: 0 };
  const pre = BOOT && BOOT.route && !opts.more && !inDone() ? BOOT : null;
  if (pre) { route = pre.route || route; tasks = pre.tasks || tasks; BOOT.route = BOOT.tasks = null; }
  else try {
    [route, tasks] = await Promise.all([
      api(`/route?tab=${inDone() ? "done" : "todo"}&limit=${PAGE}&offset=${skip}`).catch(() => ({ stops: [], total: 0 })),
      inDone() ? { tasks: [], total: 0 }
        : api(`/tasks?tab=mine&limit=${PAGE}&offset=${skip}`).catch(() => ({ tasks: [], total: 0 })),
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
    const w = s.done_at
      ? { top: new Date(s.done_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" }), sub: STATUS_WORD[s.status] || "done", late: false }
      : whenParts(s.delivery);
    const item = {
      type: "stop", chip: k.chip, spine: w.late ? "late" : k.spine, kindLabel: k.label,
      number: s.number, who: s.customer, items: s.items, due: s.due,
      address: s.address, urgent: s.urgent, when: w, late: w.late, tasks: [],
      hasOrder: true, status: s.status, clothes: s.clothes,
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
      hasOrder: !!t.order, status: t.order ? t.order.status : null, clothes: t.order ? t.order.clothes : null,
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
  if (!inDone()) WORK.sort((a, b) => (b.late - a.late) || (b.urgent - a.urgent));   // done: naya sabse upar
  $("chips").innerHTML = workChips();
  paintLate();
  renderWork();
}

/* Chips ka ek hi listener, container par — har chip par apna handler
 * nahi. Pehle yahan wo handler dobara chipkaya jaata tha jo abhi dabaya
 * gaya tha (`x.onclick = b.onclick`), aur wo closure PURANE `b` par band
 * tha. Yani "Washing" dabate hi har chip ka matlab "Washing" ho jaata
 * tha — doosra filter chunna namumkin, jab tak list dobara load na ho.
 *
 * Container kabhi dobara nahi banta, sirf uska andar ka HTML badalta hai,
 * isliye ye listener ek hi baar lagta hai aur hamesha us chip ko padhta
 * hai jispar sach mein tap hua.
 */
$("chips").addEventListener("click", (e) => {
  const b = e.target.closest("[data-chip]");
  if (!b) return;
  const wasDone = inDone();
  FILTER = b.dataset.chip;
  if (NAV === "bills") { loadBills(); return; }
  // Done alag list hai (server se), baaki chips usi list ko chhaantte hain
  if (inDone() || wasDone) { loadWork(); return; }
  $("chips").innerHTML = workChips();
  paintLate();
  renderWork();
});

function renderWork() {
  const rows = WORK.filter((w) =>
    FILTER === "all" || inDone() ? true : FILTER === "late" ? w.late : w.chip === FILTER);
  if (!rows.length) {
    $("list").innerHTML = inDone()
      ? `<div class="empty"><b>Nothing finished yet</b>Work you complete stays here for 7 days.</div>`
      : WORK.length
      ? `<div class="empty"><b>Nothing in this filter</b>Try another one.</div>`
      : `<div class="empty"><b>All clear 👏</b>New work shows up here.</div>`;
    return;
  }
  // "Show more" sirf tab jab chhaant lagi hi na ho — chip ke andar aadhi
  // list dikhana aur "aur hai" kehna jhooth hai.
  $("list").innerHTML = `<div class="reg">${rows.map(workRow).join("")}</div>`
    + ((FILTER === "all" || inDone()) && MORE_LEFT
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

/* Har row ka ek hi mukhya kaam — order ki haalat aur aadmi ke role se.
   Pehle "Done" sirf un rows par tha jin par task bana tha: kisi par Call,
   kisi par Done, kisi par sirf collect — delivery boy samajh nahi paata tha
   ki kis row par kya dabana hai. Ab kram hamesha: Call · kaam · ₹ · ⋯ */
const DELIVERY_ROLES = ["DELIVERY", "MANAGER", "SUPERVISOR", "ADMIN"];
const WASH_ROLES = ["WASHER", "MANAGER", "SUPERVISOR", "ADMIN"];
function primaryAction(w) {
  const t = w.tasks && w.tasks[0];
  const s = w.status;
  const pend = w.clothes && w.clothes.delivered > 0 ? w.clothes.pending : 0;
  if (s && ["READY", "OUT_FOR_DELIVERY"].includes(s) && DELIVERY_ROLES.includes(ME.role)) {
    return { act: "deliver", label: pend ? `Deliver rest (${pend})` : "Deliver" };
  }
  if (s && ["PICKUP_ASSIGNED"].includes(s) && DELIVERY_ROLES.includes(ME.role)) {
    const t = w.tasks && w.tasks.find((x) => x.kind === "pickup" && x.status === "OPEN");
    return t ? { act: "pickup_bill", label: "Create bill & pick up", code: t.code } : { act: "pickup", label: "Picked up" };
  }
  if (s && ["RECEIVED", "PICKED_UP"].includes(s) && WASH_ROLES.includes(ME.role)) {
    return { act: "washing", label: "Start wash" };
  }
  if (s && ["IN_WASH", "IN_DRY", "IN_IRON"].includes(s) && WASH_ROLES.includes(ME.role)) {
    return { act: "ready", label: "Ready" };
  }
  if (t) return { act: "done", label: "Done", code: t.code };
  return null;
}

function workRow(w) {
  const t = w.tasks[0];
  const asked = t && t.cancel_requested;
  // Paisa sirf billing roles ko — server washerman ko due bhejta hi nahi
  const due = (w.noMoney || !ME.can_money || w.due === undefined) ? ""
    : w.due > 0 ? `<span class="amt due">${money(w.due)}</span>`
    : `<span class="amt paid-tick">✓</span>`;
  const main = primaryAction(w);
  const partial = w.clothes && w.clothes.delivered > 0 && w.clothes.pending > 0;
  return `<article class="row tappable" data-num="${esc(w.number)}" data-open="1">
    <div class="spine ${w.spine}"></div>
    <div class="when ${w.late ? "late" : ""}">
      <b>${esc(w.when.top)}</b>${w.when.sub ? `<span>${esc(w.when.sub)}</span>` : ""}
    </div>
    <div class="body">
      <div class="line1"><b>${w.urgent ? "🔴 " : ""}${esc(w.who)}</b>${due}</div>
      <div class="sub">
        <span class="tag ${w.chip === "task" ? "task" : w.spine || "wash"}">${esc(w.kindLabel)}</span>${esc(w.number)}${w.items ? " · " + esc(w.items) : ""}
      </div>
      ${partial ? `<div class="sub mt-xs"><span class="tag late">${w.clothes.pending} pending</span>${w.clothes.delivered} of ${w.clothes.total} delivered</div>` : ""}
      ${w.title && w.type === "task" ? `<div class="sub note">${esc(w.title)}</div>` : ""}
      ${asked ? `<div class="sub mt-xs"><span class="tag late">Cancel maanga</span>${esc(t.cancel_reason || "")}</div>` : ""}
      ${ME.show_route && w.address ? `<div class="sub mt-xs">📍 ${esc(w.address)}</div>` : ""}
      <div class="acts">
        ${w.hasOrder && ME.can_contact ? `<button class="btn ghost sm" data-do="call">Call</button>` : ""}
        ${main ? `<button class="btn go sm" data-do="${main.act}"${main.code ? ` data-code="${esc(main.code)}"` : ""}>${esc(main.label)}</button>` : ""}
        ${ME.show_route && w.address ? `<button class="btn ghost sm" data-do="map" data-addr="${esc(w.address)}">Route</button>` : ""}
        ${ME.can_money && ME.features.includes("cod_collection") && w.due > 0
          ? `<button class="btn money sm" data-do="pay" data-due="${w.due}">${money(w.due)} collect</button>` : ""}
        <button class="btn ghost sm" data-do="more" aria-label="More">⋯</button>
      </div>
    </div>
  </article>`;
}

function runAction(act, num, d = {}) {
  if (act === "call") return callCustomer(num);
  if (act === "map") return window.open(
    "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(d.addr),
    "_blank", "noopener");
  if (act === "done") return askDone(d.code);
  if (act === "ask") return openThread(d.code);
  if (act === "pay") return askCollect(num, parseFloat(d.due));
  if (act === "more") return moreMenu(num);
  if (act === "deliver") return deliverModal(num);
  if (act === "pickup_bill") return openPickupBill(d.code);
  if (act === "pickup") return confirmStep(num, "picked-up", "Picked up the clothes?", "Yes, picked up");
  if (act === "washing") return confirmStep(num, "washing", "Started washing these clothes?", "Yes, washing");
  if (act === "ready") return confirmStep(num, "ready", "Clothes washed and ready?", "Yes, ready");
}

function wireRows() {
  $("list").querySelectorAll("[data-do]").forEach((b) => {
    b.onclick = (ev) => {
      ev.stopPropagation();
      runAction(b.dataset.do, b.closest("[data-num]").dataset.num, b.dataset);
    };
  });
  // Row par kahin bhi tap = poori detail. Delivery boy ko pata hona chahiye
  // ki is order mein kya-kya kapde hain, kitne dene hain, kitna paisa.
  $("list").querySelectorAll("[data-open]").forEach((row) => {
    row.onclick = () => {
      const w = WORK.find((x) => x.number === row.dataset.num);
      if (w && w.hasOrder) return orderDetailModal(w.number);
      if (w && w.tasks[0]) return askDone(w.tasks[0].code);
    };
  });
}

/* ─── order ki poori detail ─────────────────────────────────────────── */
const STATUS_WORD = {
  RECEIVED: "Received", PICKUP_ASSIGNED: "To pick up", PICKED_UP: "Picked up", IN_WASH: "Washing",
  IN_DRY: "Drying", IN_IRON: "Ironing", READY: "Ready", OUT_FOR_DELIVERY: "Out for delivery",
  DELIVERED: "Delivered", ON_HOLD: "On hold", CANCELLED: "Cancelled",
};
/* IMP_006: "Washing for 30h (limit 24h)" — deri ho to laal patti, warna
   halki line. Milestones sirf un plans mein jahan timeline khuli hai. */
const hrs = (h) => h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
function stageLine(t) {
  if (!t) return "";
  const head = t.delayed
    ? `<div class="delay-bar">⏱ Delayed — ${esc(t.delay_reason)}</div>`
    : `<p class="said">⏱ ${esc(t.stage_label)} for ${hrs(t.stage_hours)}${t.stage_limit ? ` · limit ${t.stage_limit}h` : ""}</p>`;
  const ms = (t.milestones || []).map((m) => `<div class="od-sub"><span>${esc(m.label)}</span>
      <span class="${m.late ? "late-t" : ""}">${m.hours != null ? hrs(m.hours) : ""}</span></div>`).join("");
  return head + (ms ? `<div class="od-block"><div class="od-head"><b>Progress</b></div>${ms}</div>` : "");
}

async function orderDetailModal(number) {
  let o;
  try { o = await api(`/orders/${encodeURIComponent(number)}`); }
  catch (e) { toast(e.message, true); return; }
  const w = WORK.find((x) => x.number === number) || { tasks: [], hasOrder: true };
  const main = primaryAction({ ...w, status: o.status, clothes: o.clothes });
  const c = o.clothes || { total: 0, delivered: 0, pending: 0 };
  const lineHtml = (o.lines || []).map((l) => {
    const tag = (d, q) => d >= q ? `<span class="dl-tag ok">✓ delivered</span>`
      : d > 0 ? `<span class="dl-tag part">${q - d} pending</span>` : "";
    if (l.pieces) {
      return `<div class="od-line"><div class="od-top"><b>${esc(l.name)}</b><span>${esc(String(l.weight))} kg</span></div>
        ${l.pieces.map((p) => `<div class="od-sub"><span>${esc(p.type)}</span><span>× ${p.qty} ${tag(p.delivered, p.qty)}</span></div>`).join("")}
      </div>`;
    }
    return `<div class="od-line"><div class="od-top"><b>${esc(l.name)}${l.service && l.service !== l.name ? ` <small>${esc(l.service)}</small>` : ""}</b>
      <span>${l.bag ? `${esc(String(l.weight))} kg` : `× ${l.qty}`} ${tag(l.delivered, l.qty)}</span></div></div>`;
  }).join("") || `<p class="said">No items on this bill.</p>`;
  openModal(`<h3>${o.urgent ? "🔴 " : ""}${esc(o.customer)}</h3>
    <p class="said">${esc(o.number)} · ${esc(STATUS_WORD[o.status] || o.status)}${o.delivery ? ` · delivery ${esc(whenParts(o.delivery).top)}` : ""}${o.urgent ? " · ⚡ URGENT" : ""}</p>
    ${stageLine(o.tracking)}
    <div class="od-block">
      <div class="od-head"><b>Clothes</b><span>${c.total} total${c.delivered ? ` · ${c.delivered} delivered · <b class="late-t">${c.pending} pending</b>` : ""}</span></div>
      ${lineHtml}
    </div>
    ${ME.can_money && o.total !== undefined ? `<div class="od-block">
      <div class="kv"><span>Bill</span><b>${money(o.total)}</b></div>
      ${o.urgent_charge ? `<div class="kv"><span>incl. urgent charge</span><b>${money(o.urgent_charge)}</b></div>` : ""}
      <div class="kv"><span>Paid</span><b>${money(o.paid)}</b></div>
      <div class="kv"><span>Due</span><b class="${o.due > 0 ? "late-t" : ""}">${money(Math.max(0, o.due))}</b></div>
    </div>` : ""}
    ${o.notes ? `<div class="od-block"><b>Notes</b><p class="note">${esc(o.notes)}</p></div>` : ""}
    <div class="btnrow">
      ${ME.can_contact ? `<button class="btn ghost" data-od="call">📞 Call</button>` : ""}
      ${main ? `<button class="btn go" data-od="${main.act}"${main.code ? ` data-code="${esc(main.code)}"` : ""}>${esc(main.label)}</button>` : ""}
      ${o.can_collect && o.due > 0 ? `<button class="btn money" data-od="pay" data-due="${o.due}">${money(o.due)} collect</button>` : ""}
    </div>
    <div class="btnrow">
      ${ME.can_money ? `<button class="btn ghost" data-od="share">🧾 Send bill</button>` : ""}
      ${ME.can_money ? `<button class="btn ghost" data-od="message">💬 Message</button>` : ""}
      <button class="btn ghost" data-act="close">Close</button>
    </div>`);
  $("modal-body").querySelectorAll("[data-od]").forEach((b) => {
    b.onclick = () => {
      closeModal();
      if (b.dataset.od === "share") return shareBill(number);
      if (b.dataset.od === "message") return messageMenu(number, Number(o.due || 0));
      return runAction(b.dataset.od, number, b.dataset);
    };
  });
}

/* ─── delivery: sab ya kuch kapde (BUG_008) ─────────────────────────── */
/* 12 mein se 8 shirt diye — baaki 4 pending rehte hain aur agli baar
   "Deliver rest (4)" dikhta hai. Default: sab chune hue (aam haalat). */
async function statusMenu(number) {
  let o;
  try { o = await api(`/orders/${encodeURIComponent(number)}`); }
  catch (e) { toast(e.message, true); return; }

  const stages = [
    ["RECEIVED", "Received", "Order received"],
    ["PICKUP_ASSIGNED", "Pickup scheduled", "Pickup assigned"],
    ["PICKED_UP", "Picked up", "Clothes picked up"],
    ["IN_WASH", "Wash", "Internal update — no customer message"],
    ["IN_DRY", "Drying", "Internal update — no customer message"],
    ["IN_IRON", "Iron", "Internal update — no customer message"],
    ["READY", "Ready", "Customer will be notified"],
    ["OUT_FOR_DELIVERY", "Out for delivery", "Customer will be notified"],
    ["DELIVERED", "Delivered", "Thank-you + feedback message"],
  ];
  const current = stages.findIndex(([s]) => s === o.status);
  const options = stages.slice(Math.max(0, current + 1));

  if (!options.length) {
    openModal(`<div class="status-head"><span class="status-icon">↻</span><div><h3>Status</h3><p class="said">${esc(o.number)} · ${esc(STATUS_WORD[o.status] || o.status)}</p></div></div>
      <p class="comm-note">This order is already at its latest stage.</p>
      <div class="btnrow"><button class="btn ghost wide" data-act="close">Close</button></div>`);
    return;
  }

  openModal(`<div class="status-head"><span class="status-icon">↻</span><div><h3>Update status</h3><p class="said">${esc(o.number)} · ${esc(o.customer)}</p></div></div>
    <div class="status-list">
      ${options.map(([s,label,desc]) => `<button class="status-option" data-status="${s}"><span class="status-dot"></span><span><b>${label}</b><small>${desc}</small></span></button>`).join("")}
    </div>
    <div class="btnrow"><button class="btn ghost wide" data-act="close">Cancel</button></div>`);

  $("modal-body").querySelectorAll("[data-status]").forEach((b) => {
    b.onclick = () => busy(b, async () => {
      const r = await api(`/orders/${encodeURIComponent(number)}/status`, {
        method: "POST", body: { status: b.dataset.status }
      });
      closeModal();
      toast(`${number} → ${STATUS_WORD[r.status] || r.status}`);
      refreshCurrent({ quiet: true });
      loadToday();
    });
  });
}

async function deliverModal(number) {
  let o;
  try { o = await api(`/orders/${encodeURIComponent(number)}`); }
  catch (e) { toast(e.message, true); return; }
  // har chunne layak cheez ek "slot": {line, piece?, max, name, bag?}
  const slots = [];
  (o.lines || []).forEach((l) => {
    if (l.pieces) {
      l.pieces.forEach((p) => { if (p.pending > 0) slots.push({ line: l.line, piece: p.piece, max: p.pending, name: p.type, group: `${l.name} · ${l.weight} kg` }); });
    } else if (l.pending > 0) {
      slots.push({ line: l.line, max: l.pending, name: l.name, sub: l.service !== l.name ? l.service : "", bag: !!l.bag, weight: l.weight });
    }
  });
  if (!slots.length) { toast("Nothing left to deliver on this bill", true); return; }
  const pick = slots.map((s) => s.max);   // default: sab
  const totalPending = slots.reduce((a, s) => a + s.max, 0);
  let lastGroup = "";
  openModal(`<h3>Deliver — ${esc(o.number)}</h3>
    <p class="said">${esc(o.customer)} · ${totalPending} to deliver${o.clothes.delivered ? ` (${o.clothes.delivered} already delivered)` : ""}</p>
    <div class="btnrow mt0">
      <button type="button" class="btn ghost sm" id="dl-all">Select all</button>
      <button type="button" class="btn ghost sm" id="dl-none">Clear</button>
    </div>
    <div class="dl-list">${slots.map((s, i) => {
      const head = s.group && s.group !== lastGroup ? `<div class="dl-group">${esc(s.group)}</div>` : "";
      lastGroup = s.group || "";
      return head + (s.bag
        ? `<label class="dl-row"><span class="dl-name">${esc(s.name)} <small>${esc(String(s.weight))} kg bag</small></span>
             <input type="checkbox" data-bag="${i}" checked aria-label="Deliver the whole bag"></label>`
        : `<div class="dl-row"><span class="dl-name">${esc(s.name)}${s.sub ? ` <small>${esc(s.sub)}</small>` : ""}</span>
             <div class="stepper sm">
               <button type="button" class="step" data-dl="${i}" data-by="-1" aria-label="One less">−</button>
               <input type="number" inputmode="numeric" min="0" max="${s.max}" value="${s.max}" data-dlq="${i}" aria-label="How many">
               <button type="button" class="step" data-dl="${i}" data-by="1" aria-label="One more">+</button>
             </div>
             <small class="dl-of">of ${s.max}</small></div>`);
    }).join("")}</div>
    <p class="dl-sum" id="dl-sum"></p>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Cancel</button>
      <button class="btn go" id="dl-go">Deliver</button>
    </div>`);
  const body = $("modal-body");
  const paint = () => {
    const n = pick.reduce((a, v) => a + v, 0);
    const left = totalPending - n;
    $("dl-sum").innerHTML = n === 0 ? "Pick the clothes you are handing over."
      : left === 0 ? `<b>All ${n}</b> — the order will be marked <b>delivered</b>.`
      : `Delivering <b>${n}</b> · <b class="late-t">${left} will stay pending</b>`;
    $("dl-go").textContent = n === 0 ? "Deliver" : left === 0 ? `✅ Deliver all ${n}` : `✅ Deliver ${n}`;
    $("dl-go").disabled = n === 0;
    slots.forEach((s, i) => {
      const q = body.querySelector(`[data-dlq="${i}"]`);
      if (q && document.activeElement !== q) q.value = String(pick[i]);
      const cb = body.querySelector(`[data-bag="${i}"]`);
      if (cb) cb.checked = pick[i] > 0;
    });
  };
  body.querySelectorAll("[data-dl]").forEach((b) => {
    b.onclick = () => {
      const i = +b.dataset.dl;
      pick[i] = Math.max(0, Math.min(slots[i].max, pick[i] + parseInt(b.dataset.by, 10)));
      paint();
    };
  });
  body.querySelectorAll("[data-dlq]").forEach((inp) => {
    inp.oninput = () => {
      const i = +inp.dataset.dlq;
      pick[i] = Math.max(0, Math.min(slots[i].max, parseInt(inp.value, 10) || 0));
      paint();
    };
    inp.onblur = () => { inp.value = String(pick[+inp.dataset.dlq]); };
  });
  body.querySelectorAll("[data-bag]").forEach((cb) => {
    cb.onchange = () => { pick[+cb.dataset.bag] = cb.checked ? 1 : 0; paint(); };
  });
  $("dl-all").onclick = () => { slots.forEach((s, i) => { pick[i] = s.max; }); paint(); };
  $("dl-none").onclick = () => { pick.fill(0); paint(); };
  $("dl-go").onclick = (e) => busy(e.currentTarget, async () => {
    const n = pick.reduce((a, v) => a + v, 0);
    const all = n === totalPending;
    const items = all ? null : slots.map((s, i) => ({ line: s.line, ...(s.piece != null ? { piece: s.piece } : {}), qty: pick[i] }))
      .filter((x) => x.qty > 0);
    const r = await api(`/orders/${encodeURIComponent(number)}/deliver`, { method: "POST", body: { items } });
    closeModal();
    toast(r.pending ? `${r.delivered_now} delivered — ${r.pending} still pending` : `${number} delivered ✅`, false, 3500);
    loadToday(); refreshCurrent({ quiet: true });
    // paisa baaki ho to seedha collect — darwaze par wahi agla kaam hai
    if (o.can_collect && o.due > 0) askCollect(number, o.due);
  });
  paint();
}

function confirmStep(number, path, question, yes) {
  const w = WORK.find((x) => x.number === number) || {};
  openModal(`<h3>${esc(question)}</h3>
    <p class="said">${esc(w.who || "")} · ${esc(number)}${w.items ? ` · ${esc(w.items)}` : ""}</p>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Not now</button>
      <button class="btn go" id="m-step">${esc(yes)}</button>
    </div>`);
  $("m-step").onclick = (e) => busy(e.currentTarget, async () => {
    await api(`/orders/${encodeURIComponent(number)}/${path}`, { method: "POST" });
    closeModal();
    toast(`${number} — ${yes.replace(/^Yes, /, "")} ✅`);
    loadToday(); refreshCurrent({ quiet: true });
  });
}

function moreMenu(number) {
  const w = WORK.find((x) => x.number === number) || {};
  const t = (w.tasks || [])[0];
  openModal(`<h3>${esc(number)}</h3>
    <p class="said">${esc(w.who || "")}</p>
    <div class="btnrow stack">
      ${w.hasOrder ? `<button class="btn ghost" data-act="detail" data-arg="${esc(number)}">👁 Full details</button>` : ""}
      ${t ? `<button class="btn ghost" data-act="jobdone" data-arg="${esc(t.code)}">✅ Mark job ${esc(t.code)} done</button>` : ""}
      ${t && ME.can_ask ? `<button class="btn ghost" data-act="thread" data-arg="${esc(t.code)}">❓ Ask the owner</button>` : ""}
      ${ME.can_money ? `<button class="btn ghost" data-act="share" data-arg="${esc(number)}">🧾 Send bill on WhatsApp</button>` : ""}
      ${ME.can_money && w.hasOrder ? `<button class="btn ghost" data-act="message" data-arg="${esc(number)}">💬 Send a message</button>` : ""}
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
    ${ME.can_ask ? `<label for="m-q">Ask the owner</label>
    <textarea id="m-q" placeholder="e.g. how many items? when to deliver?"></textarea>` : ""}
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Close</button>
      ${ME.can_ask ? `<button class="btn go" id="m-send">Send</button>` : ""}
    </div>`);
  // WhatsApp juda nahi — sawaal owner tak jaata hi nahi, to poochhne ka rasta bhi nahi
  if (!ME.can_ask) return;
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

async function askCollect(order, due) {
  // ₹250.50 due par "251" bharna server se "Only ₹250 is due" laata tha
  const dueStr = Number.isInteger(due) ? String(due) : due.toFixed(2);
  // Purana udhaar bhi saamne rakhte hain. Delivery wala darwaze par ek hi
  // baar khada hota hai — wahi ek mauka hai poora paisa maangne ka.
  let prev = 0, prevBills = 0;
  try {
    const d = await api(`/orders/${encodeURIComponent(order)}/dues`);
    prev = d.previous_due || 0; prevBills = d.previous_bills || 0;
  } catch (e) { /* na mile to sirf is bill ka paisa — kaam rukta nahi */ }
  const grand = Math.round((due + prev) * 100) / 100;

  openModal(`<h3>${esc(order)} — payment received</h3>
    <p class="said">${money(due)} is bill ka${prev > 0 ? ` · ${money(prev)} pichhla (${prevBills} bill)` : ""}</p>
    ${prev > 0 ? `<div class="pick sm mb">
      <button type="button" class="pickbtn on" data-only="1">Sirf ye bill<br><b>${money(due)}</b></button>
      <button type="button" class="pickbtn" data-only="0">Kul dena hai<br><b>${money(grand)}</b></button>
    </div>` : ""}
    <label for="m-amt">Amount</label>
    <input id="m-amt" type="number" inputmode="decimal" value="${dueStr}" min="1" step="0.01">
    ${prev > 0 ? `<p class="hint" id="m-note2">Purana chukane par paisa sabse purane bill se lagega.</p>` : ""}
    <div class="btnrow">
      <button class="btn ghost" id="m-cash">💵 Cash</button>
      <button class="btn go" id="m-upi">📱 UPI</button>
    </div>
    <div class="btnrow"><button class="btn ghost" data-act="close">Not now</button></div>`);

  let settlePrev = false;
  document.querySelectorAll("[data-only]").forEach((b) => {
    b.onclick = () => {
      settlePrev = b.dataset.only === "0";
      document.querySelectorAll("[data-only]").forEach((x) => x.classList.toggle("on", x === b));
      $("m-amt").value = settlePrev ? String(grand) : dueStr;
    };
  });

  const send = (e, method) => {
    const amount = parseFloat($("m-amt").value);
    const ceiling = settlePrev ? grand : due;
    if (!(amount > 0)) { toast("Enter the amount", true); return; }
    if (amount > ceiling + 0.01) { toast(`Only ${money(ceiling)} is due`, true); return; }
    return busy(e.currentTarget, async () => {
      const r = await api(`/orders/${encodeURIComponent(order)}/collect`, {
        method: "POST", body: { amount, method, settle_previous: settlePrev },
      });
      closeModal();
      const older = (r.settled_older || []).length;
      toast(
        `${money(amount)} received ✅ — ${money(r.due)} left`
        + (older ? ` · ${older} purana bill chukta` : ""),
      );
      loadToday(); refreshCurrent({ quiet: true });
      // Dukaan ka WhatsApp API juda ho (can_ask) to server khud grahak ko
      // "payment mila" bhejta hai. Nahi to preview — apne WhatsApp se bhejo.
      if (!ME.can_ask && canBill()) composeMessage(order, "payment_thanks");
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
let PICKUP_BILL_CONTEXT = null;
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
  // Wiring upar wale delegated listener se — yahan dobara nahi.

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
      const row = b.closest("[data-num]");
      const num = row.dataset.num;
      if (b.dataset.bill === "msg") return messageMenu(num, parseFloat(row.dataset.due || "0"));
      if (b.dataset.bill === "status") return statusMenu(num);
      if (b.dataset.bill === "pay") return askCollect(num, parseFloat(b.dataset.due));
    };
  });
}

function billRow(b) {
  const w = whenParts(b.delivery);
  const paid = !(b.due > 0);
  return `<article class="bill-card" data-num="${esc(b.number)}" data-due="${Number(b.due) || 0}">
    <div class="bill-accent ${paid ? "paid" : w.late ? "late" : "active"}"></div>
    <div class="bill-main">
      <div class="bill-topline">
        <div><span class="bill-no">${esc(b.number)}</span><span class="bill-date">${esc(b.created)}</span></div>
        <span class="bill-due ${paid ? "paid" : ""}">${paid ? "Paid" : money(b.due) + " due"}</span>
      </div>
      <div class="bill-customer"><b>${esc(b.customer)}</b></div>
      <div class="bill-items">${b.items ? esc(b.items) : "Laundry order"}</div>
      <div class="bill-progress">${billProgress(b.status)}</div>
      <div class="bill-actions">
        <button class="btn ghost sm bill-comm" data-bill="msg">💬 Message</button>
        ${ME.is_manager ? `<button class="btn ghost sm bill-status" data-bill="status">↻ Status</button>` : ""}
        ${ME.features.includes("cod_collection") && b.due > 0 ? `<button class="btn money sm" data-bill="pay" data-due="${b.due}">${money(b.due)} collect</button>` : ""}
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
    <div class="btnrow">
      <a class="btn ghost" href="${esc(rawbtUrl(r.print_text))}" data-act="close">🖨 Bluetooth printer</a>
      <button class="btn ghost" id="m-print">🖨 Print</button>
    </div>
    <p class="hint">Bluetooth printer: install the free <b>RawBT</b> app on this Android phone once and pair the printer in it.</p>
    <div class="btnrow"><button class="btn ghost" data-act="close">Close</button></div>`);
  $("m-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(SHARE_TEXT); toast("Copied"); }
    catch (e) { toast("Could not copy — select the text above", true); }
  };
  $("m-print").onclick = () => {
    closeModal();
    const el = $("receipt");
    el.textContent = r.print_text;
    el.className = r.paper_mm === 80 ? "paper-80" : "paper-58";
    window.print();
  };
}

/* Chhota Bluetooth thermal printer. Saste 58/80mm printer "classic
   Bluetooth" hote hain — browser unse seedha baat nahi kar sakta (Web
   Bluetooth sirf BLE aur HTTPS). Dukaanein Android par RawBT app chalati
   hain: ye link bill ke ESC/POS bytes seedha usko deta hai. Text server se
   ASCII aur 32/48 akshar par kata hua aata hai (₹ ki jagah Rs.). */
function rawbtUrl(text) {
  // ESC @ (reset) + bill + khaali lines + GS V 66 0 (cutter ho to kaat do)
  const body = "\x1b@" + String(text || "").replace(/[^\x0a\x20-\x7e]/g, "") + "\n\n\n\n" + "\x1dVB\x00";
  return `intent:base64,${btoa(body)}#Intent;scheme=rawbt;package=ru.a402d.rawbtprinter;end;`;
}

/* ─── grahak ko message: thank you, payment mila, review ────────────── */
/* Text server banata hai (owner ka apna format bhi). Staff pehle padhta
   hai, phir apne phone ka WhatsApp — wa.me link, number aur text bhara hua.
   Paise ki yaad sirf manager (neeche remindBill), isliye wo yahan tabhi
   jab manager ho aur paisa baaki ho. */
const MSG_KINDS = [
  ["bill", "🧾", "Share bill"],
  ["remind", "💰", "Payment reminder"],
  ["delivery_update", "🚚", "Delivery update"],
  ["review_request", "⭐", "Feedback request"],
];
function messageMenu(number, due) {
  const kinds = MSG_KINDS.filter(([k]) => k !== "remind" || (ME.is_manager && due > 0));
  openModal(`<div class="comm-head"><span class="comm-icon">💬</span><div><h3>Customer communication</h3><p class="said">${esc(number)}</p></div></div>
    <p class="comm-note">Choose a ready-to-send professional message.</p>
    <div class="comm-menu">
      ${kinds.map(([k, ico, label]) => `<button class="comm-option" data-msg="${k}"><span class="comm-option-icon">${ico}</span><span><b>${label}</b><small>${k === "bill" ? "Bill + secure payment link" : k === "remind" ? "Polite reminder for pending payment" : k === "delivery_update" ? "Share the current delivery status" : "Ask for customer feedback"}</small></span><span class="comm-arrow">›</span></button>`).join("")}
    </div>
    <div class="btnrow"><button class="btn ghost wide" data-act="close">Close</button></div>`);
  $("modal-body").querySelectorAll("[data-msg]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.msg === "remind") { closeModal(); return remindBill(number, null); }
      if (b.dataset.msg === "bill") { closeModal(); return shareBill(number); }
      return busy(b, () => composeMessage(number, b.dataset.msg));
    };
  });
}
/* ─── paise ki yaad ─────────────────────────────────────────────────── */
/* Scheduler khud 3 din / 15 din par yaad dilata hai, par wo maanta hai ki
   order deliver ho chuka hai AUR dukaan ka WhatsApp API juda hai. Counter
   par khada aadmi in dono ka intezaar nahi kar sakta — isliye ek button.

   API se chala gaya to bas ek toast. Na gaya to wahi rasta jo bill share
   karta hai: staff ke apne phone ka WhatsApp, text bhara hua. */
async function remindBill(number, btn) {
  await busy(btn, async () => {
    const r = await api(`/orders/${encodeURIComponent(number)}/remind`, { method: "POST" });
    if (r.sent) {
      toast(`${r.name} ko yaad dila diya 🔔`);
      return;
    }
    openModal(`<h3>Yaad dilayein</h3>
      <p class="said">Dukaan ka WhatsApp API se nahi ja paya. Apne phone se bhej dijiye — ${esc(r.name)} ko.</p>
      <pre class="sharetext">${esc(r.text)}</pre>
      <div class="btnrow">
        <a class="btn go" href="${esc(waUrl(r.phone, r.text))}" target="_blank" rel="noopener" data-act="close">📲 WhatsApp kholein</a>
        <button class="btn ghost" id="m-copy">Copy</button>
      </div>
      <div class="btnrow"><button class="btn ghost" data-act="close">Band karein</button></div>`);
    SHARE_TEXT = r.text;
    $("m-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(SHARE_TEXT); toast("Copied"); }
      catch (e) { toast("Copy nahi hua — upar se select kar lijiye", true); }
    };
  });
}

/* ─── naya bill ─────────────────────────────────────────────────────── */
/* Daam staff nahi bharta — rate card se aata hai, wahi jo WhatsApp wale
   bill par lagta hai. Do jagah do hisaab kabhi nahi. */

let RATES = null, CART = [], PICKED = "", NEEDS_PICKUP = false;
// Chhoot: mode "amt" ya "pct", aur ek value. Do alag field rakhne par log
// dono bhar dete hain aur phir poochte hain ki kaunsa laga.
let DISC = { mode: "amt", value: 0 };
// ⚡ Urgent: on, haath se badla?, badli hui rakam. Server bhi yahi hisaab lagata hai.
let URG = { on: false, manual: false, amt: 0 };
// Chune hue purane grahak ka baaki. Naye grahak par 0.
let PREV_DUE = 0, PREV_BILLS = 0;
let REWARDS = [], REWARD_CODE = "";      // 🎁 grahak ke reward, aur jo laga hai
// Bill ka timer (IMP_006): bill form par pehle asli tap/likhne se Save tak.
// Ek hi document listener — #list doosre tab bhi dikhata hai, isliye
// sirf tab ginte hain jab bill form (b-phone) screen par ho.
let BILL_STARTED = 0;
const startBillTimer = (e) => {
  if (!BILL_STARTED && $("b-phone") && e.target.closest && e.target.closest("#list")) BILL_STARTED = Date.now();
};
document.addEventListener("input", startBillTimer, true);
document.addEventListener("click", startBillTimer, true);

async function showNewBill() {
  $("chips").innerHTML = "";
  if (!ME.features.includes("billing")) {
    $("list").innerHTML = `<div class="empty"><b>Billing is not in this plan</b>Ask the owner to upgrade.</div>`;
    return;
  }
  if (!ME.can_bill) {
    $("list").innerHTML = `<div class="empty"><b>Bill counter par banta hai</b>
      Aapke role mein bill banana nahi hai. Manager ya delivery wale se kahein.</div>`;
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
      <div id="b-reward" hidden></div>
      <label for="b-phone">Mobile number</label>
      <input id="b-phone" type="tel" inputmode="numeric" placeholder="98xxxxxxxx" maxlength="15">
    </div>

    <div class="card">
      <h3>Items</h3>
      <label for="b-svc">Service</label>
      <select id="b-svc"></select>
      <label for="b-q" id="b-qlabel">Item</label>
      <input id="b-q" type="search" placeholder="Search item — e.g. shirt" autocomplete="off" enterkeyhint="done">
      <div class="item-list" id="b-items" aria-live="polite"></div>
      <div id="b-cart"></div>
    </div>

    <div class="card" id="b-pay" hidden>
      <h3>Where are the clothes?</h3>
      <div class="pick">
        <button type="button" class="pickbtn on" data-pickup="0">At the shop</button>
        <button type="button" class="pickbtn" data-pickup="1">Collect from customer</button>
      </div>
      <p class="hint" id="b-pickhint">The washing queue gets this bill.</p>

      <button type="button" class="urgbtn mt-lg" id="b-urg" aria-pressed="false">
        <span>⚡ Urgent</span><small id="b-urghint"></small>
      </button>
      <div id="b-urgbox" hidden>
        <label for="b-urgamt">Urgent charge (₹)</label>
        <div class="discrow">
          <input id="b-urgamt" type="number" inputmode="decimal" min="0" step="1">
          <button type="button" class="btn ghost sm" id="b-urgnone">No charge</button>
        </div>
        <p class="hint mt-xs" id="b-urgnote"></p>
      </div>

      <details class="fold mt-lg">
        <summary>Discount <small id="b-discsum">none</small></summary>
        <div class="discrow">
          <div class="pick sm">
            <button type="button" class="pickbtn on" data-disc="amt">₹</button>
            <button type="button" class="pickbtn" data-disc="pct">%</button>
          </div>
          <input id="b-disc" type="number" inputmode="decimal" value="0" min="0" placeholder="0">
        </div>
      </details>

      <h3 class="mt-lg">Payment</h3>
      <label for="b-adv">Received now (₹)</label>
      <input id="b-adv" type="number" inputmode="decimal" value="0" min="0">
    </div>

    <!-- Kul rakam hamesha aankh ke saamne. Counter par sabse zaroori number
         yahi hai, aur pehle wo cart ke andar scroll ho kar chhup jaata tha. -->
    <div class="paybar" id="b-bar" hidden>
      <div class="paybar-sum" id="b-barsum"></div>
      <button class="btn go" id="b-save">Create bill</button>
    </div>`;


  // Chhoot ka mode. ₹ se % par jaate hi value ka matlab badal jaata hai,
  // isliye value shunya kar dete hain — "50" ka 50% ban jaana chori hai.
  DISC = { mode: "amt", value: 0 };
  $("list").querySelectorAll("[data-disc]").forEach((b) => {
    b.onclick = () => {
      DISC.mode = b.dataset.disc;
      DISC.value = 0;
      $("b-disc").value = "0";
      $("list").querySelectorAll("[data-disc]").forEach((x) => x.classList.toggle("on", x === b));
      renderCart();
    };
  });
  $("b-disc").oninput = () => {
    DISC.value = Math.max(0, parseFloat($("b-disc").value) || 0);
    renderCart();
  };
  // ⚡ Urgent — ek tap; charge owner ki setting se, badlo ya "No charge"
  URG = { on: false, manual: false, amt: 0 };
  const cfg = ME.urgent || { type: "percent", value: 50, days: 1 };
  const when = cfg.days === 0 ? "today" : cfg.days === 1 ? "tomorrow" : `in ${cfg.days} days`;
  $("b-urghint").textContent = (cfg.value ? (cfg.type === "flat" ? `+${money(cfg.value)}` : `+${cfg.value}%`) : "no extra charge")
    + ` · delivery ${when}`;
  $("b-urg").onclick = () => {
    URG = { on: !URG.on, manual: false, amt: 0 };
    $("b-urg").classList.toggle("on", URG.on);
    $("b-urg").setAttribute("aria-pressed", String(URG.on));
    $("b-urgbox").hidden = !URG.on;
    renderCart();
  };
  $("b-urgamt").oninput = () => {
    URG.manual = true;
    URG.amt = Math.max(0, parseFloat($("b-urgamt").value) || 0);
    renderCart();
  };
  $("b-urgamt").onfocus = () => $("b-urgamt").select();
  $("b-urgnone").onclick = () => { URG.manual = true; URG.amt = 0; $("b-urgamt").value = "0"; renderCart(); };
  $("b-svc").onchange = () => { PICK_SVC = $("b-svc").value; renderPicker(); };
  $("b-q").oninput = renderPicker;
  $("b-q").onkeydown = (ev) => {
    // Enter = pehla nateeja jod do (ya naya banao, agar kuch na mila)
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    // kg mein Enter = pehla kapda bore mein (weight wali line nahi)
    const first = $("b-items").querySelector("[data-piece], [data-addpiece]")
      || $("b-items").querySelector("[data-tile], [data-create]");
    if (first) first.click();
  };
  $("b-disc").onfocus = () => $("b-disc").select();
  $("b-adv").onfocus = () => $("b-adv").select();
  $("b-adv").oninput = renderCart;
  // Kapde kahan hain — yahi tay karta hai ki bill washer ki kataar mein
  // jayega ya delivery wale ke raaste mein. Pehle ye sawaal poocha hi
  // nahi jaata tha, isliye har bill washer ko jaata tha aur phone par
  // aaya "lene aa jao" wala order delivery wale ko kabhi nahi dikhta tha.
  NEEDS_PICKUP = false;
  $("list").querySelectorAll("[data-pickup]").forEach((b) => {
    b.onclick = () => {
      NEEDS_PICKUP = b.dataset.pickup === "1";
      $("list").querySelectorAll("[data-pickup]").forEach((x) => x.classList.toggle("on", x === b));
      $("b-pickhint").textContent = NEEDS_PICKUP
        ? "Goes to the delivery route as a pickup."
        : "The washing queue gets this bill.";
    };
  });
  $("b-save").onclick = (e) => saveBill(e.currentTarget);
  wireCustomerSearch();

  if (PICKUP_BILL_CONTEXT) {
    const ctx = PICKUP_BILL_CONTEXT;
    PICKED = ctx.customer_ref || "";
    PREV_DUE = 0;
    PREV_BILLS = 0;
    $("b-name").value = ctx.customer_name || "";
    $("b-phone").value = ctx.customer_phone || "";
    $("b-phone").disabled = true;
    $("b-picked").hidden = false;
    $("b-picked").className = "picked";
    $("b-picked").textContent = `🛵 Pickup ${ctx.order_number} · ${ctx.customer_name || "Customer"}`;
    NEEDS_PICKUP = false;
    $("b-pickhint").textContent = "Pickup task linked — this bill completes the collection. No second pickup will be created.";
  }
  renderCart();
}

/* ─── kapde jodna: service dropdown + item search ───────────────────── */
/* Pehle: Service dropdown → Item dropdown → Qty → Add, har kapde par.
   Phir tiles aaye — tez the, par chhoti screen par 20-30 kapdon ke tiles
   mein dhoondhna padta tha. Ab: service chuno, item ka naam likhna shuru
   karo ("shi" → Shirt), neeche patli list — tap = jud gaya, dobara = 2.
   Na mile to wahin "＋ Create" — naam pehle se bhara hua. Doosri service
   mein mile to wo bhi dikhta hai. */
let PICK_SVC = "";
function pickerServices() {
  const all = [...new Set(RATES.map((r) => r.service))];
  // piece wali services pehle — counter par wahi zyada lagti hain
  return all.sort((a, b) => isKgService(a) - isKgService(b));
}
function renderPicker() {
  const box = $("b-items");
  if (!box || !RATES) return;
  const svcs = pickerServices();
  if (!svcs.includes(PICK_SVC)) PICK_SVC = svcs[0] || "";
  const sel = $("b-svc");
  const sig = svcs.join("");
  if (sel.dataset.sig !== sig) {
    // sirf tab dobara bharo jab list badli — warna khula dropdown band ho jaata
    sel.innerHTML = svcs.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
    sel.dataset.sig = sig;
  }
  sel.value = PICK_SVC;

  const raw = ($("b-q").value || "").trim().replace(/\s+/g, " ");
  const q = raw.toLowerCase();
  const kgMode = isKgService(PICK_SVC);
  $("b-q").placeholder = kgMode ? "Search clothes in the bag — e.g. shirt" : "Search item — e.g. shirt";
  $("b-qlabel").textContent = kgMode ? "Clothes in the bag (count only, no rate)" : "Item";
  if (kgMode) { renderKgPicker(box, raw, q); return; }
  const hit = (r) => !q || (r.garment || "by weight").toLowerCase().includes(q);
  const here = RATES.filter((r) => r.service === PICK_SVC && hit(r));
  const elsewhere = q ? RATES.filter((r) => r.service !== PICK_SVC && hit(r)).slice(0, 8) : [];
  const rows = here.concat(elsewhere);
  const line = (r, n, showSvc) => {
    const inCart = CART.find((x) => x.service === r.service && x.garment === r.garment);
    const badge = inCart ? (r.unit === "kg" ? `${inCart.qty} kg` : `× ${inCart.qty}`) : "";
    return `<button type="button" class="item-row${inCart ? " in" : ""}" data-tile="${n}">
      <span class="ir-name">${esc(r.garment || "By weight")}${showSvc ? ` <small>${esc(r.service)}</small>` : ""}</span>
      <span class="ir-price">${money(r.rate)}${r.unit === "kg" ? "/kg" : ""}</span>
      <span class="ir-add${badge ? " n" : ""}">${badge ? esc(badge) : "＋"}</span>
    </button>`;
  };
  let html = here.map((r, n) => line(r, n, false)).join("");
  if (q && !here.length) html += `<p class="ir-none">No “${esc(raw)}” in ${esc(PICK_SVC)}.</p>`;
  if (elsewhere.length) {
    html += `<div class="ir-head">In other services</div>`
      + elsewhere.map((r, k) => line(r, here.length + k, true)).join("");
  }
  // "shi" par Shirt mil gaya to "Create shi" sirf bheed hai — Create tabhi
  // jab is service mein kuch na mile (ya search khaali ho)
  if (!q || !here.length) {
    html += `<button type="button" class="item-row create" data-create="1">${q
      ? `＋ Create “${esc(raw)}” in ${esc(PICK_SVC)}`
      : "＋ New item — not on the list"}</button>`;
  }
  box.innerHTML = html;
  box.querySelectorAll("[data-tile]").forEach((b) => {
    b.onclick = () => tapItem(rows[parseInt(b.dataset.tile, 10)]);
  });
  const create = box.querySelector("[data-create]");
  if (create) create.onclick = () => newRateModal(PICK_SVC, raw);
}
/* ─── KG service: bore ke kapde, sirf ginti ─────────────────────────── */
/* KG service ka rate card par ek hi row hota hai ("By weight"). Pehle yahan
   kapde ka naam likhkar "Create" dabane par wo rate card mein jaane ki
   koshish karta aur 409 aata ("already on the rate card") — kyunki kg mein
   kapde ka koi alag daam hota hi nahi. Ab kg service chunne par search
   kapdon ke NAAM dhoondhta hai; tap = bore mein ek aur (ginti), daam nahi.
   Naam na mile to wahin "＋ Add to the bag" — rate card ko chhuta nahi. */
const COMMON_CLOTHES = [
  "Shirt", "T-shirt", "Pant", "Jeans", "Lower", "Shorts", "Kurta", "Pajama", "Saree",
  "Salwar", "Dupatta", "Frock", "Top", "Jacket", "Sweater", "Towel", "Bedsheet",
  "Pillow cover", "Curtain", "Blanket", "Undergarments", "Socks", "Handkerchief",
];
function isKgService(svc) {
  const rows = (RATES || []).filter((r) => r.service === svc);
  return rows.length > 0 && rows.every((r) => r.unit === "kg");
}
function kgLineFor(svc) {
  let idx = CART.findIndex((x) => x.service === svc && x.unit === "kg");
  if (idx >= 0) return idx;
  const r = RATES.find((x) => x.service === svc && x.unit === "kg");
  if (!r) return -1;
  CART.push({ service: r.service, garment: r.garment, qty: 1, rate: r.rate, card: r.rate, unit: "kg", pieces: [] });
  toast(`${r.service} added — enter the weight in kg below`, false, 2200);
  return CART.length - 1;
}
function renderKgPicker(box, raw, q) {
  const rate = RATES.find((x) => x.service === PICK_SVC && x.unit === "kg");
  const line = CART.find((x) => x.service === PICK_SVC && x.unit === "kg");
  const inBag = new Map((line ? line.pieces : []).map((p) => [p.type.toLowerCase(), p.qty]));
  // bore mein pehle se pade kapde upar, phir rate card ke naam, phir aam kapde
  const seen = new Set();
  const names = [];
  [...(line ? line.pieces.map((p) => p.type) : []),
   ...RATES.map((r) => (r.garment || "").trim()),
   ...COMMON_CLOTHES].forEach((n) => {
    const k = n.toLowerCase();
    if (n && !seen.has(k)) { seen.add(k); names.push(n); }
  });
  const matches = names.filter((n) => !q || n.toLowerCase().includes(q)).slice(0, q ? 12 : 40);
  const count = line ? line.pieces.reduce((s, p) => s + p.qty, 0) : 0;
  let html = `<button type="button" class="item-row kgrow${line ? " in" : ""}" data-weight="1">
      <span class="ir-name">⚖️ By weight <small>${line ? `${count} clothes in the bag` : "tap, then enter the kg"}</small></span>
      <span class="ir-price">${rate ? money(rate.rate) + "/kg" : ""}</span>
      <span class="ir-add${line ? " n" : ""}">${line ? esc(`${line.qty} kg`) : "＋"}</span>
    </button>
    <div class="ir-head">Clothes in the bag — count only</div>`;
  html += matches.map((n, i) => {
    const c = inBag.get(n.toLowerCase());
    return `<button type="button" class="item-row${c ? " in" : ""}" data-piece="${i}">
      <span class="ir-name">${esc(n)}</span>
      <span class="ir-add${c ? " n" : ""}">${c ? esc(`× ${c}`) : "＋"}</span>
    </button>`;
  }).join("");
  // milte-julte naam mil gaye to "Add shi" sirf bheed — tabhi jab kuch na mile
  if (q && !matches.length) {
    html += `<button type="button" class="item-row create" data-addpiece="1">＋ Add “${esc(raw)}” to the bag</button>`;
  }
  box.innerHTML = html;
  box.querySelector("[data-weight]").onclick = () => {
    const idx = kgLineFor(PICK_SVC);
    renderCart();
    const inp = $("b-cart").querySelector(`[data-qty="${idx}"]`);
    if (inp) { inp.focus(); inp.select(); }
  };
  box.querySelectorAll("[data-piece]").forEach((b) => {
    b.onclick = () => addPiece(matches[parseInt(b.dataset.piece, 10)]);
  });
  const add = box.querySelector("[data-addpiece]");
  if (add) add.onclick = () => addPiece(raw);
}
function addPiece(name) {
  let type = String(name || "").trim().replace(/\s+/g, " ").slice(0, 60);
  if (!type) return;
  type = type[0].toUpperCase() + type.slice(1);   // "pillow cover" -> "Pillow cover"
  const idx = kgLineFor(PICK_SVC);
  if (idx < 0) return;
  const line = CART[idx];
  const p = line.pieces.find((x) => x.type.toLowerCase() === type.toLowerCase());
  if (p) p.qty += 1; else line.pieces.push({ type, qty: 1 });
  const searched = $("b-q").value.trim();
  $("b-q").value = "";
  renderCart();
  if (navigator.vibrate) navigator.vibrate(12);
  if (searched) $("b-q").focus();
}

function tapItem(r) {
  if (!r) return;
  let idx = CART.findIndex((x) => x.service === r.service && x.garment === r.garment);
  if (idx >= 0) {
    // wahi kapda dobara: ginti badhti hai, badla hua rate waisa hi rehta hai.
    // KG par dobara tap se wazan nahi badhta — wazan likhna hota hai.
    if (r.unit !== "kg") CART[idx].qty = Math.round((CART[idx].qty + 1) * 100) / 100;
  } else {
    CART.push({ service: r.service, garment: r.garment, qty: 1, rate: r.rate, card: r.rate, unit: r.unit || "pc", pieces: [] });
    idx = CART.length - 1;
  }
  // search se joda tha to khaana saaf karke wahin focus — agla kapda seedha
  // likho. Bina search ke tap kiya ho to keyboard zabardasti nahi kholte.
  const searched = $("b-q") && $("b-q").value.trim();
  if (searched) $("b-q").value = "";
  renderCart();
  if (navigator.vibrate) navigator.vibrate(12);   // haath ko pata chale ki juda
  if (r.unit === "kg") {
    const inp = $("b-cart").querySelector(`[data-qty="${idx}"]`);
    if (inp) { inp.focus(); inp.select(); }
  } else if (searched) {
    $("b-q").focus();
  }
  toast(`${r.garment || r.service} added`, false, 1200);
}

/* Grahak aisa kapda laaya jo rate card par nahi. Bill chhod kar owner ko
   phone karne ke bajaye yahin jod do — rate card mein bhi judta hai, taaki
   agla bill wahi daam le. Server har jod audit karta hai.
   showNewBill() dobara NAHI chalate: wo naam/number ke bhare khaane mita
   deta. Sirf dono dropdown naye sire se bharte hain aur item cart mein. */
function newRateModal(service, itemName = "") {
  const services = [...new Set(RATES.map((r) => r.service))];
  openModal(`<h3>Add to rate card</h3>
    <p class="said">It is added to this bill and saved for the next bills too.</p>
    <label for="nr-svc">Service</label>
    <input id="nr-svc" list="nr-svcs" value="${esc(service || "")}" placeholder="e.g. Dry Clean" maxlength="60" autocomplete="off">
    <datalist id="nr-svcs">${services.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
    <div id="nr-itemwrap">
      <label for="nr-item">Item</label>
      <input id="nr-item" value="${esc(itemName)}" placeholder="e.g. Blazer" maxlength="60" autocomplete="off">
    </div>
    <div class="frow">
      <div>
        <label for="nr-unit">Charged per</label>
        <select id="nr-unit"><option value="pc">Piece</option><option value="kg">Kg</option></select>
      </div>
      <div>
        <label for="nr-rate">Rate (₹)</label>
        <input id="nr-rate" type="number" inputmode="decimal" min="0" step="0.5" placeholder="0">
      </div>
    </div>
    <div class="err" id="nr-err"></div>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Cancel</button>
      <button class="btn go" id="nr-save">Add to bill</button>
    </div>`);
  // KG service mein item nahi hota — poora bora tulta hai (dashboard jaisa)
  const paintUnit = () => { $("nr-itemwrap").hidden = $("nr-unit").value === "kg"; };
  $("nr-unit").onchange = paintUnit;
  const known = RATES.find((r) => r.service === service);
  if (known && known.unit === "kg") $("nr-unit").value = "kg";
  paintUnit();
  // jo pehle se pata hai (service, search ka naam) wo dobara nahi poochhte
  $(!service ? "nr-svc" : ($("nr-unit").value === "kg" || itemName) ? "nr-rate" : "nr-item").focus();
  $("nr-save").onclick = (e) => {
    const unit = $("nr-unit").value, rate = parseFloat($("nr-rate").value);
    const svc = $("nr-svc").value.trim(), item = unit === "kg" ? "" : $("nr-item").value.trim();
    const fail = (msg, id) => { $("nr-err").textContent = msg; $(id).focus(); };
    if (!svc) return fail("Write the service name.", "nr-svc");
    if (unit === "pc" && !item) return fail("Write the item name.", "nr-item");
    if (!(rate > 0)) return fail("Enter a rate above ₹0.", "nr-rate");
    $("nr-err").textContent = "";
    return busy(e.currentTarget, async () => {
      let row;
      try { row = await api("/rates", { method: "POST", body: { service: svc, garment: item, unit, rate } }); }
      catch (err) { $("nr-err").textContent = err.message; return; }
      RATES = RATES.filter((r) => !(r.service === row.service && r.garment === row.garment)).concat(row);
      closeModal();
      if (!$("b-items")) return;        // screen badal gayi — rate card to ban hi gaya
      PICK_SVC = row.service;
      tapItem(row);
      toast(`${row.garment || row.service} added — ${money(row.rate)}${row.unit === "kg" ? "/kg" : ""}`);
    });
  };
  $("modal-body").querySelectorAll("input").forEach((el) => {
    el.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); $("nr-save").click(); } });
  });
}

/* ─── kharcha ───────────────────────────────────────────────────────── */
/* Petrol, detergent, chai-paani — jo paisa dukaan ke liye nikla, wahi
   likh do. Owner ke dashboard mein naam ke saath dikhta hai. Yahan sirf
   apna daala hua dikhta hai; galti ho to owner hatata hai. */
async function showExpenses(opts = {}) {
  $("chips").innerHTML = "";
  if (!opts.quiet) $("list").innerHTML = `<div class="empty">Loading…</div>`;
  let d;
  try { d = await api("/expenses"); }
  catch (e) { $("list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (NAV !== "exp") return;
  const today = new Date();
  const iso = (dt) => new Date(dt.getTime() - dt.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  const min = new Date(today); min.setDate(min.getDate() - d.backdate_days);
  $("list").innerHTML = `
    <div class="card">
      <h3>Add expense</h3>
      <label for="x-cat">Category</label>
      <select id="x-cat"><option value="">Pick one…</option>${d.categories.map((c) => `<option>${esc(c)}</option>`).join("")}</select>
      <div class="frow">
        <div>
          <label for="x-amt">Amount (₹)</label>
          <input id="x-amt" type="number" inputmode="decimal" min="1" step="1" placeholder="0">
        </div>
        <div>
          <label for="x-date">Date</label>
          <input id="x-date" type="date" value="${iso(today)}" min="${iso(min)}" max="${iso(today)}">
        </div>
      </div>
      <label for="x-desc">What for? (optional)</label>
      <input id="x-desc" maxlength="300" placeholder="e.g. petrol for pickups">
      <div class="err" id="x-err"></div>
      <button class="btn go wide" id="x-save">Save expense</button>
      <p class="hint">The owner sees it with your name. Older than ${d.backdate_days} days? Ask the owner.</p>
    </div>
    <div class="card">
      <h3>My expenses <small class="said">last 30 days · ${money(d.total)}</small></h3>
      ${d.expenses.length
        ? d.expenses.map((x) => `
          <div class="kv"><span>${esc(whenParts(x.spent_on).top)} · ${esc(x.category)}${x.description ? ` — ${esc(x.description)}` : ""}</span><b>${money(x.amount)}</b></div>`).join("")
        : `<p class="hint mt0">Nothing yet.</p>`}
    </div>`;
  $("x-save").onclick = (e) => {
    const category = $("x-cat").value, amount = parseFloat($("x-amt").value);
    const fail = (msg, id) => { $("x-err").textContent = msg; $(id).focus(); };
    if (!category) return fail("Pick a category.", "x-cat");
    if (!(amount > 0)) return fail("Enter the amount.", "x-amt");
    $("x-err").textContent = "";
    return busy(e.currentTarget, async () => {
      try {
        await api("/expenses", { method: "POST", body: {
          category, amount, spent_on: $("x-date").value || null, description: $("x-desc").value.trim(),
        }});
      } catch (err) { $("x-err").textContent = err.message; return; }
      toast(`${money(amount)} ${category} saved ✅`);
      showExpenses({ quiet: true });
    });
  };
}

/* Ek hisaab, ek jagah. UI aur server dono yahi kram lagate hain:
   gross -> chhoot -> total -> advance -> is bill ka due -> + purana. */
function billMath() {
  const gross = CART.reduce((s, i) => s + i.qty * i.rate, 0);
  const raw = DISC.mode === "pct" ? (gross * DISC.value) / 100 : DISC.value;
  const discount = Math.min(Math.round(raw * 100) / 100, gross);
  // ⚡ Urgent charge chhoot ke baad — server (services/urgent.py) ka hi niyam:
  // default = items ka % (ya fixed ₹), poore rupaye mein
  let urg = 0;
  if (URG.on) {
    const c = ME.urgent || { type: "percent", value: 50 };
    urg = URG.manual ? URG.amt : Math.max(0, Math.round(c.type === "flat" ? c.value : (gross * c.value) / 100));
  }
  const total = Math.round((gross - discount + urg) * 100) / 100;
  const adv = Math.min(Math.max(0, parseFloat(($("b-adv") || {}).value) || 0), total);
  const due = Math.round((total - adv) * 100) / 100;
  return { gross, discount, urg, total, adv, due, grand: Math.round((due + PREV_DUE) * 100) / 100 };
}

let CART_PAINTING = false;
let EDIT_RATE = -1;   // kis line ka daam-khaana khula hai (-1 = koi nahi)
function renderCart() {
  const box = $("b-cart");
  if (!box) return;
  // Focus wala qty/wazan ka khaana hatate hi browser uska blur/change
  // chalata hai, jo phir renderCart bulata — aadhe bane DOM par dobara
  // innerHTML ("node to be removed is no longer a child"). Ek baar mein ek.
  if (CART_PAINTING) return;
  CART_PAINTING = true;
  try { paintCart(box); } finally { CART_PAINTING = false; }
}
function paintCart(box) {
  renderPicker();   // tiles par ginti ka badge cart ke saath chale
  const empty = CART.length === 0;
  $("b-pay").hidden = empty;
  $("b-bar").hidden = empty;
  if (empty) { box.innerHTML = ""; return; }

  const m = billMath();
  // Har line ka daam badla ja sakta hai. Rate card ka daam saath dikhta
  // rehta hai — bina uske do din baad kisi ko yaad nahi rehta ki chhoot
  // di gayi thi ya galti hui thi.
  // Ek kapda = ek line: naam, − qty +, rakam, ✕. Daam ka khaana tabhi
  // khulta hai jab "₹63 each ✎" dabaya jaye — mol-bhav kabhi-kabhi hota
  // hai, aur har line par bada rate box cart ko do guna lamba karta tha.
  box.innerHTML = `<div class="cart">${CART.map((i, n) => {
    const kg = i.unit === "kg";
    const changed = i.rate !== i.card;
    return `
    <div class="cartrow">
      <div class="ci-line">
        <div class="ci-name">${kg
          ? `${esc(i.garment || i.service)} <small>${i.garment ? esc(i.service) + " · " : ""}kg</small>`
          : `${esc(i.garment)} <small>${esc(i.service)}</small>`}
          <button type="button" class="ci-rate${changed ? " changed" : ""}" data-editrate="${n}">${
            money(i.rate)}${kg ? "/kg" : " each"}${changed ? ` · card ${money(i.card)}` : ""} ✎</button>
        </div>
        <div class="stepper sm">
          <button type="button" class="step" data-q="${n}" data-by="${kg ? -0.5 : -1}" aria-label="${kg ? "Half kg less" : "One less"}">−</button>
          <input type="number" inputmode="decimal" data-qty="${n}" value="${i.qty}" min="0.1" step="${kg ? 0.1 : 1}" aria-label="${kg ? "Weight in kg" : "How many"}">
          <button type="button" class="step" data-q="${n}" data-by="${kg ? 0.5 : 1}" aria-label="${kg ? "Half kg more" : "One more"}">+</button>
        </div>
        <b class="ci-amt">${money(i.qty * i.rate)}</b>
        <button class="rm" data-rm="${n}" aria-label="Remove">✕</button>
      </div>
      <div class="ratebox" ${EDIT_RATE === n ? "" : "hidden"}>
        <span class="cur">₹</span>
        <input type="number" inputmode="decimal" data-rate="${n}" value="${i.rate}" min="0" aria-label="Rate">
        <small>${changed ? "card ₹" + i.card : "rate card"}</small>
      </div>
      ${i.unit === "kg" ? `<div class="ci-pieces">
        <button type="button" class="linkbtn" data-pieces="${n}">👕 ${piecesCount(i.pieces) ? "Edit clothes" : "+ Add clothes (count only)"}</button>
        ${piecesCount(i.pieces) ? `<small>${esc(piecesLabel(i.pieces))}</small>` : ""}
      </div>` : ""}
    </div>`;
  }).join("")}
    <div class="sums">
      <div class="s-row"><span>Subtotal</span><span>${money(m.gross)}</span></div>
      ${m.discount > 0 ? `<div class="s-row off"><span>Discount${
        DISC.mode === "pct" ? ` (${DISC.value}%)` : ""
      }</span><span>−${money(m.discount)}</span></div>` : ""}
      ${m.urg > 0 ? `<div class="s-row urg"><span>⚡ Urgent charge</span><span>+${money(m.urg)}</span></div>` : ""}
      <div class="s-row big"><span>Total</span><span>${money(m.total)}</span></div>
      ${m.adv > 0 ? `<div class="s-row"><span>Received now</span><span>−${money(m.adv)}</span></div>` : ""}
      ${PREV_DUE > 0 ? `<div class="s-row warn"><span>Pichhla baaki (${PREV_BILLS})</span><span>${money(PREV_DUE)}</span></div>` : ""}
    </div></div>`;

  box.querySelectorAll("[data-rm]").forEach((b) => {
    b.onclick = () => { CART.splice(parseInt(b.dataset.rm, 10), 1); EDIT_RATE = -1; renderCart(); };
  });
  box.querySelectorAll("[data-pieces]").forEach((b) => {
    b.onclick = () => piecesModal(parseInt(b.dataset.pieces, 10));
  });
  box.querySelectorAll("[data-q]").forEach((b) => {
    b.onclick = () => {
      const n = parseInt(b.dataset.q, 10);
      const i = CART[n];
      const next = Math.round((i.qty + parseFloat(b.dataset.by)) * 10) / 10;
      // kapda 1 se − kiya = hata do (ek tap mein galti theek). kg 0.5 se neeche nahi.
      if (i.unit !== "kg" && next < 1) { CART.splice(n, 1); EDIT_RATE = -1; }
      else i.qty = Math.max(i.unit === "kg" ? 0.5 : 1, next);
      renderCart();
    };
  });
  box.querySelectorAll("[data-editrate]").forEach((b) => {
    b.onclick = () => {
      const n = parseInt(b.dataset.editrate, 10);
      EDIT_RATE = EDIT_RATE === n ? -1 : n;
      renderCart();
      const inp = box.querySelector(`[data-rate="${n}"]`);
      if (EDIT_RATE === n && inp) { inp.focus(); inp.select(); }
    };
  });
  // input par re-render nahi karte: har akshar par DOM badalne se cursor
  // field se kood jaata hai aur "40" likhna namumkin ho jaata hai. Sirf
  // sums update karte hain; poora cart change (blur) par.
  box.querySelectorAll("[data-qty],[data-rate]").forEach((inp) => {
    const idx = parseInt(inp.dataset.qty ?? inp.dataset.rate, 10);
    const isRate = inp.dataset.rate !== undefined;
    inp.oninput = () => {
      const v = parseFloat(inp.value);
      if (!(v >= 0)) return;
      CART[idx][isRate ? "rate" : "qty"] = v;
      paintBar();
    };
    inp.onchange = () => {
      if (!(CART[idx].qty > 0)) CART[idx].qty = 0.5;
      renderCart();
    };
  });
  paintBar();
}

/* ─── KG line ke kapde: sirf ginti ─────────────────────────────────── */
/* Bill wazan se (3.5 kg x ₹60). Par bore mein kya hai ye likhna zaroori
   hai — washerman ko ginti milani hai, aur wapas dete waqt "ek kurta kam
   hai" ka jawab yahi list hai. Daam kisi kapde ka nahi lagta. */
const piecesCount = (p) => (p || []).reduce((s, x) => s + (parseInt(x.qty, 10) || 0), 0);
const piecesLabel = (p) => (p || []).map((x) => `${x.type} ${x.qty}`).join(", ") + ` — ${piecesCount(p)} pcs`;

function piecesModal(idx) {
  const line = CART[idx];
  if (!line) return;
  let draft = (line.pieces || []).map((x) => ({ ...x }));
  if (!draft.length) draft.push({ type: "", qty: 1 });
  const names = [...new Set((RATES || []).map((r) => (r.garment || "").trim()).filter(Boolean))].sort();
  openModal(`<h3>Clothes in ${esc(line.garment || line.service)}</h3>
    <p class="said">Count only — the bill stays by weight.</p>
    <datalist id="pc-names">${names.map((x) => `<option value="${esc(x)}">`).join("")}</datalist>
    <div id="pc-rows"></div>
    <button type="button" class="btn ghost wide" id="pc-add">+ Add another cloth</button>
    <div class="s-row big"><span>Total pieces</span><span id="pc-total">0</span></div>
    <div class="btnrow">
      <button class="btn ghost" data-act="close">Cancel</button>
      <button class="btn go" id="pc-save">Save clothes</button>
    </div>`);
  const total = () => {
    $("pc-total").textContent = piecesCount(draft.filter((x) => (x.type || "").trim()));
  };
  const paint = (focusLast) => {
    $("pc-rows").innerHTML = draft.map((x, n) => `
      <div class="frow pc-row">
        <input list="pc-names" data-pc-name="${n}" value="${esc(x.type)}" placeholder="Cloth, e.g. Shirt" maxlength="60" aria-label="Cloth name">
        <div class="stepper sm">
          <button type="button" class="step" data-pc-step="${n}" data-by="-1" aria-label="One less">−</button>
          <input type="number" inputmode="numeric" data-pc-qty="${n}" value="${x.qty}" min="1" step="1" aria-label="How many">
          <button type="button" class="step" data-pc-step="${n}" data-by="1" aria-label="One more">+</button>
          <button type="button" class="rm" data-pc-rm="${n}" aria-label="Remove cloth">✕</button>
        </div>
      </div>`).join("");
    const rows = $("pc-rows");
    rows.querySelectorAll("[data-pc-name]").forEach((el) => {
      el.oninput = () => { draft[+el.dataset.pcName].type = el.value; total(); };
      el.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); $("pc-add").click(); } };
    });
    rows.querySelectorAll("[data-pc-qty]").forEach((el) => {
      el.oninput = () => { draft[+el.dataset.pcQty].qty = parseInt(el.value, 10) || 0; total(); };
    });
    rows.querySelectorAll("[data-pc-step]").forEach((b) => {
      b.onclick = () => {
        const x = draft[+b.dataset.pcStep];
        x.qty = Math.max(1, (parseInt(x.qty, 10) || 0) + parseInt(b.dataset.by, 10));
        paint();
      };
    });
    rows.querySelectorAll("[data-pc-rm]").forEach((b) => {
      b.onclick = () => {
        draft.splice(+b.dataset.pcRm, 1);
        if (!draft.length) draft.push({ type: "", qty: 1 });
        paint();
      };
    });
    total();
    if (focusLast) {
      const names = rows.querySelectorAll("[data-pc-name]");
      names[names.length - 1].focus();
    }
  };
  paint(true);
  $("pc-add").onclick = () => { draft.push({ type: "", qty: 1 }); paint(true); };
  $("pc-save").onclick = () => {
    // ek hi kapda do baar likha ho to jod do — "Shirt 2" + "shirt 3" = Shirt 5
    const merged = [];
    draft.forEach((x) => {
      const type = (x.type || "").trim().replace(/\s+/g, " ");
      const qty = parseInt(x.qty, 10) || 0;
      if (!type || qty < 1) return;
      const same = merged.find((m) => m.type.toLowerCase() === type.toLowerCase());
      if (same) same.qty += qty; else merged.push({ type, qty });
    });
    line.pieces = merged;
    closeModal();
    renderCart();
  };
}

/* Neeche chipki hui patti — kul rakam aur ek button. */
function paintBar() {
  const bar = $("b-barsum");
  if (!bar) return;
  const m = billMath();
  if ($("b-urgamt") && URG.on) {
    // default rakam khaane mein dikhti rahe — jab tak haath se na badli ho
    if (!URG.manual && document.activeElement !== $("b-urgamt")) $("b-urgamt").value = String(m.urg);
    $("b-urgnote").textContent = m.urg > 0 ? `${money(m.urg)} added to the bill` : "No urgent charge — still marked urgent";
  }
  if ($("b-discsum")) {
    $("b-discsum").textContent = m.discount > 0
      ? `−${money(m.discount)}${DISC.mode === "pct" ? ` (${DISC.value}%)` : ""}` : "none";
  }
  bar.innerHTML = PREV_DUE > 0
    ? `<b>${money(m.grand)}</b><span>${money(m.due)} is bill ka + ${money(PREV_DUE)} purana</span>`
    : `<b>${money(m.due)}</b><span>${CART.length} item${CART.length > 1 ? "s" : ""}${
        m.discount > 0 ? " · " + money(m.discount) + " chhoot" : ""}${URG.on ? " · ⚡ urgent" : ""}</span>`;
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
    PICKED = ""; PREV_DUE = 0; PREV_BILLS = 0; REWARDS = []; REWARD_CODE = ""; paintReward();
    $("b-picked").hidden = true; $("b-phone").disabled = false;
    renderCart();
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
        ? hits.map((c, i) => `<div data-pick="${i}">${esc(c.name || "No name")} · ${esc(c.phone_masked)}${
            c.due > 0 ? `<span class="acdue">${money(c.due)} baaki</span>` : ""}</div>`).join("")
        : `<div class="none">New customer — enter the number below</div>`;
      box.querySelectorAll("[data-pick]").forEach((row) => {
        row.onclick = () => {
          const c = AC_HITS[parseInt(row.dataset.pick, 10)];
          if (!c) return;
          PICKED = c.ref;
          PREV_DUE = c.due || 0;
          PREV_BILLS = c.due_bills || 0;
          input.value = c.name || "";
          box.innerHTML = "";
          $("b-phone").value = ""; $("b-phone").disabled = true;
          $("b-picked").hidden = false;
          // Purana udhaar yahin, is pal — grahak abhi saamne khada hai.
          $("b-picked").className = PREV_DUE > 0 ? "picked owes" : "picked";
          $("b-picked").textContent = PREV_DUE > 0
            ? `⚠ ${c.name || "Customer"} · ${money(PREV_DUE)} pichhla baaki (${PREV_BILLS} bill)`
            : `✓ ${c.name || "Customer"} · ${c.phone_masked}`;
          renderCart();
          loadRewards(c.ref);
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

/* 🎁 Reward: grahak chunte hi server se — hai to chip, Apply ek tap, ✕ se hata do */
async function loadRewards(ref) {
  REWARDS = []; REWARD_CODE = "";
  try { REWARDS = (await api(`/customers/${encodeURIComponent(ref)}/rewards`)).available || []; }
  catch (e) { REWARDS = []; }
  paintReward();
}
function paintReward() {
  const box = $("b-reward");
  if (!box) return;
  if (!REWARDS.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = REWARDS.map((r) => r.code === REWARD_CODE
    ? `<div class="rwchip on">🎁 <b>${esc(r.reward)}</b> applied · ${esc(r.code)}<button type="button" class="btn ghost sm" data-rw="">✕ Remove</button></div>`
    : `<div class="rwchip">🎁 Reward: <b>${esc(r.reward)}</b>${r.expires_at ? ` · till ${esc(r.expires_at.slice(0, 10))}` : ""}<button type="button" class="btn go sm" data-rw="${esc(r.code)}">Apply</button></div>`).join("");
  box.querySelectorAll("[data-rw]").forEach((b) => { b.onclick = () => { REWARD_CODE = b.dataset.rw; paintReward(); }; });
}

async function saveBill(btn) {
  const phone = $("b-phone").value.trim();
  if (!PICKED && phone.replace(/\D/g, "").length < 10) return toast("Enter the full number", true);
  if (!CART.length) return toast("Add items first", true);
  const m = billMath();
  await busy(btn, async () => {
    const r = await api("/bills", {
      method: "POST",
      body: {
        customer_ref: PICKED,
        customer_phone: PICKED ? "" : phone,
        customer_name: $("b-name").value.trim(),
        advance: m.adv,
        // Server dono nahi maanta — jo mode chuna hai wahi bhejte hain,
        // doosra hamesha 0. Warna "kaunsa laga" ka jawab do jagah se aata.
        discount_amount: DISC.mode === "amt" ? DISC.value : 0,
        discount_percent: DISC.mode === "pct" ? DISC.value : 0,
        coupon_code: REWARD_CODE || "",
        needs_pickup: NEEDS_PICKUP,
        pickup_task_code: PICKUP_BILL_CONTEXT ? PICKUP_BILL_CONTEXT.task_code : "",
        // ⚡ jo rakam screen par dikh rahi thi wahi jaati hai (0 = maaf)
        urgent: URG.on,
        urgent_charge: URG.on ? m.urg : null,
        bill_seconds: BILL_STARTED ? Math.round((Date.now() - BILL_STARTED) / 1000) : null,
        items: CART.map((i) => ({
          service: i.service, garment: i.garment, qty: i.qty,
          // Card ka daam hi hai to rate bhejte hi nahi — server card se
          // lagayega. Sirf sach mein badla hua daam override banta hai.
          ...(i.rate !== i.card ? { rate: i.rate } : {}),
          ...(i.unit === "kg" && piecesCount(i.pieces) ? { pieces: i.pieces } : {}),
        })),
      },
    });
    const pickupCompleted = !!r.pickup_completed;
    CART = []; PICKED = ""; PREV_DUE = 0; PREV_BILLS = 0; BILL_STARTED = 0; REWARDS = []; REWARD_CODE = "";
    PICKUP_BILL_CONTEXT = null;
    DISC = { mode: "amt", value: 0 };
    URG = { on: false, manual: false, amt: 0 };
    toast(
      pickupCompleted
        ? `${r.order_number} — bill created and pickup completed ✅`
        : r.previous_due > 0
        ? `${r.order_number} — ${money(r.grand_total)} lena hai (${money(r.previous_due)} purana)`
        : `${r.order_number} created — ${money(r.total)}`,
      false, 5000,
    );
    showNewBill();
    loadToday();
    if (pickupCompleted) loadWork({ quiet: true });
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
