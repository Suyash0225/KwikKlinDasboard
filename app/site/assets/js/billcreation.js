/* /billcreation — grahak ka apna "New bill".

   Staff panel (app/static/staff.js → showNewBill) ka hi dhaancha: service
   chip, item search, patli list (tap = jud gaya, − n +), neeche cart aur
   chipki hui patti mein kul rakam. Farq sirf itna:
     - daam badalna, apni chhoot, advance, delivery date — grahak ke kaam
       nahi, hata diye. Coupon chalta hai (server jaanchta hai);
     - rakam ANDAAZA hai: pickup par kapde gin kar asli bill banta hai;
     - number: AI ke link (?t=) mein token se aata hai, likhna nahi padta.
       Saade link par WhatsApp khulta hai — wahi message grahak ke number
       ki pehchaan hai (app/services/self_order.py). */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");
const Q = new URLSearchParams(location.search);
const TOKEN = Q.get("t") || "";
const SLUG = Q.get("s") || "";

let FORM = null, RATES = [], CART = [], PICK_SVC = "", PICKUP = "";
// Coupon: code + chhoot. Daam/chhoot server tay karta hai — kapde badle to dobara poochte hain.
let COUPON = { code: "", discount: 0 }, COUPON_TIMER = null;

function toast(msg, bad) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " err" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* khaali jawab */ }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : "Something went wrong. Please try again.");
  }
  return data;
}

const isKg = (svc) => {
  const rows = RATES.filter((r) => r.service === svc);
  return rows.length > 0 && rows.every((r) => r.unit === "kg");
};
const services = () => [...new Set(RATES.map((r) => r.service))].sort((a, b) => isKg(a) - isKg(b));
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function addDays(isoDay, n) {
  const [y, m, d] = isoDay.split("-").map(Number);
  return iso(new Date(y, m - 1, d + n));
}

async function boot() {
  const qs = new URLSearchParams();
  if (TOKEN) qs.set("t", TOKEN);
  if (SLUG) qs.set("s", SLUG);
  try {
    FORM = await api(`/billcreation/api/form?${qs}`);
  } catch (e) {
    $("list").innerHTML = `<div class="empty"><b>Link not working</b>${esc(e.message)}</div>`;
    return;
  }
  RATES = FORM.rates || [];
  $("shop").textContent = FORM.shop || "Laundry";
  document.title = `${FORM.shop || "Laundry"} — place your order`;
  if (!RATES.length) {
    $("list").innerHTML = `<div class="empty"><b>Rate list is not ready</b>Please message us on WhatsApp to book a pickup.</div>`;
    return;
  }
  PICKUP = addDays(FORM.today, 1);
  render();
}

function render() {
  const c = FORM.customer;
  const today = FORM.today;
  const days = [[today, "Today"], [addDays(today, 1), "Tomorrow"], [addDays(today, 2), "Day after"]];
  $("list").innerHTML = `
    <div class="billsteps" aria-hidden="true">
      <span class="on" id="bs-1">1 · Clothes</span><span id="bs-2">2 · Pickup</span><span id="bs-3">3 · Confirm</span>
    </div>

    <div class="card">
      <h3><i class="stepn">1</i>Clothes</h3>
      <div class="svcchips" id="b-svcchips" role="tablist" aria-label="Service"></div>
      <label for="b-q" id="b-qlabel">Item</label>
      <input id="b-q" type="search" placeholder="Search item — e.g. shirt" autocomplete="off" enterkeyhint="done">
      <div class="item-list" id="b-items" aria-live="polite"></div>
      <div id="b-cart"></div>
      <details class="fold bc-gap" id="b-cpfold">
        <summary>Coupon code <small id="b-cpsum">optional</small></summary>
        <div class="discrow">
          <input id="b-coupon" type="text" maxlength="30" autocomplete="off" autocapitalize="characters" placeholder="e.g. WELCOME50">
          <button type="button" class="btn ghost sm" id="b-cpgo">Apply</button>
        </div>
        <div class="bc-err" id="b-cperr" role="alert"></div>
      </details>
    </div>

    <div class="card">
      <h3><i class="stepn">2</i>Pickup</h3>
      <label>Pickup day</label>
      <div class="bc-days" id="b-days">
        ${days.map(([d, l]) => `<button type="button" class="pickbtn${d === PICKUP ? " on" : ""}" data-day="${d}">${l}</button>`).join("")}
        <input id="b-date" type="date" min="${today}" max="${addDays(today, FORM.max_days_ahead || 14)}" value="${PICKUP}" aria-label="Pickup date">
      </div>
      <label for="b-name">Your name</label>
      <input id="b-name" type="text" maxlength="60" autocomplete="name" value="${esc(c ? c.name : "")}" placeholder="Name">
      <label for="b-addr">Pickup address</label>
      <textarea id="b-addr" rows="2" maxlength="300" autocomplete="street-address" placeholder="House no., street, landmark">${esc(c ? c.address : "")}</textarea>
      <label>WhatsApp number</label>
      ${c
        ? `<div class="bc-lock">📱 ${esc(c.phone_masked)} <small>from your WhatsApp</small></div>`
        : `<p class="bc-hint">No need to type it — the next step opens WhatsApp and your order is confirmed from your own number.</p>`}
      <details class="fold bc-gap">
        <summary>Note for us <small>optional</small></summary>
        <textarea id="b-notes" rows="2" maxlength="300" placeholder="e.g. call before coming, starch on shirts"></textarea>
      </details>
      <div class="bc-err" id="b-err" role="alert"></div>
    </div>

    <p class="bc-hint bc-note">Prices are from our rate list. The final bill is made after we count the clothes at pickup.
      Delivery about ${FORM.turnaround_days} day${FORM.turnaround_days === 1 ? "" : "s"} after pickup.</p>

    <div class="paybar" id="b-bar" hidden>
      <div class="paybar-sum" id="b-barsum"></div>
      <button class="btn go" id="b-save">${c ? "Place order" : "Continue on WhatsApp"}</button>
    </div>`;

  $("b-q").oninput = renderPicker;
  $("b-q").onkeydown = (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const first = $("b-items").querySelector("[data-tile]");
    if (first) first.click();
  };
  $("b-days").querySelectorAll("[data-day]").forEach((b) => { b.onclick = () => setDay(b.dataset.day); });
  $("b-date").onchange = () => { if ($("b-date").value) setDay($("b-date").value); };
  $("b-save").onclick = (e) => save(e.currentTarget);
  $("b-cpgo").onclick = () => applyCoupon($("b-coupon").value);
  $("b-coupon").onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); applyCoupon($("b-coupon").value); } };
  renderCart();
}

function setDay(d) {
  PICKUP = d;
  $("b-date").value = d;
  $("b-days").querySelectorAll("[data-day]").forEach((x) => x.classList.toggle("on", x.dataset.day === d));
  $("bs-2").classList.add("on");
}

/* ─── kapde jodna — staff.js renderPicker jaisa ─────────────────────── */
function renderPicker() {
  const box = $("b-items");
  if (!box) return;
  const svcs = services();
  if (!svcs.includes(PICK_SVC)) PICK_SVC = svcs[0] || "";
  $("b-svcchips").innerHTML = svcs.map((sv) => {
    const n = CART.filter((x) => x.service === sv).reduce((a, x) => a + (x.unit === "kg" ? 1 : x.qty), 0);
    return `<button type="button" role="tab" aria-selected="${sv === PICK_SVC}" class="${sv === PICK_SVC ? "on" : ""}" data-svc="${esc(sv)}">${esc(sv)}${n ? `<i>${n}</i>` : ""}</button>`;
  }).join("");
  $("b-svcchips").querySelectorAll("[data-svc]").forEach((b) => {
    b.onclick = () => { PICK_SVC = b.dataset.svc; $("b-q").value = ""; renderPicker(); };
  });

  const kg = isKg(PICK_SVC);
  $("b-q").hidden = kg;
  $("b-qlabel").hidden = kg;
  const raw = kg ? "" : ($("b-q").value || "").trim().replace(/\s+/g, " ");
  const q = raw.toLowerCase();
  const hit = (r) => !q || (r.garment || "by weight").toLowerCase().includes(q);
  const here = RATES.filter((r) => r.service === PICK_SVC && hit(r));
  const elsewhere = q ? RATES.filter((r) => r.service !== PICK_SVC && hit(r)).slice(0, 8) : [];
  const rows = here.concat(elsewhere);
  const line = (r, n, showSvc) => {
    const inCart = CART.find((x) => x.service === r.service && x.garment === r.garment);
    const step = inCart && r.unit !== "kg"
      ? `<span class="ir-step"><span class="ir-minus" data-minus="1" role="button" aria-label="One less">−</span><b>${inCart.qty}</b><span class="ir-plus" aria-hidden="true">＋</span></span>`
      : `<span class="ir-add${inCart ? " n" : ""}">${inCart ? `${inCart.qty} kg` : "＋"}</span>`;
    return `<button type="button" class="item-row${inCart ? " in" : ""}${r.unit === "kg" ? " kgrow" : ""}" data-tile="${n}">
      <span class="ir-name">${r.unit === "kg" ? "⚖️ " : ""}${esc(r.garment || "By weight")}${showSvc ? ` <small>${esc(r.service)}</small>` : ""}${
        r.unit === "kg" ? ` <small>approx. weight is fine — we weigh it at pickup</small>` : ""}</span>
      <span class="ir-price">${money(r.rate)}${r.unit === "kg" ? "/kg" : ""}</span>
      ${step}
    </button>`;
  };
  let html = here.map((r, n) => line(r, n, false)).join("");
  if (q && !here.length) html += `<p class="ir-none">No “${esc(raw)}” in ${esc(PICK_SVC)}.</p>`;
  if (elsewhere.length) {
    html += `<div class="ir-head">In other services</div>` + elsewhere.map((r, k) => line(r, here.length + k, true)).join("");
  }
  if (q && !rows.length) html += `<p class="ir-none">Not on the list? Add it in “Note for us” below.</p>`;
  box.innerHTML = html;
  box.querySelectorAll("[data-tile]").forEach((b) => {
    b.onclick = (ev) => {
      const r = rows[parseInt(b.dataset.tile, 10)];
      if (ev.target.closest("[data-minus]")) return untap(r);
      tap(r);
    };
  });
}

function tap(r) {
  recheckCoupon();
  const i = CART.find((x) => x.service === r.service && x.garment === r.garment);
  if (i) { if (r.unit !== "kg") i.qty += 1; }
  else CART.push({ service: r.service, garment: r.garment, qty: r.unit === "kg" ? 2 : 1, rate: r.rate, unit: r.unit || "pc" });
  if (navigator.vibrate) navigator.vibrate(12);
  renderCart();
}

function untap(r) {
  recheckCoupon();
  const idx = CART.findIndex((x) => x.service === r.service && x.garment === r.garment);
  if (idx < 0) return;
  if (CART[idx].qty <= 1) CART.splice(idx, 1);
  else CART[idx].qty -= 1;
  renderCart();
}

const gross = () => Math.round(CART.reduce((s, i) => s + i.qty * i.rate, 0) * 100) / 100;
const net = () => Math.max(0, gross() - (COUPON.code ? COUPON.discount : 0));
const cartBody = () => CART.map((i) => ({ service: i.service, garment: i.garment, qty: i.qty }));

/* Coupon — % wala coupon kapdon par chalta hai, isliye kapde badalte hi
   server se dobara (min order bhi tab hi pata chalta hai). */
async function applyCoupon(code, quiet) {
  code = String(code || "").trim().toUpperCase();
  $("b-cperr").textContent = "";
  if (!code) return;
  if (!CART.length) { $("b-cperr").textContent = "Add clothes first, then apply the coupon."; return; }
  try {
    const r = await api("/billcreation/api/coupon", { method: "POST", body: { t: TOKEN, s: SLUG, code, items: cartBody() } });
    COUPON = { code: r.code, discount: r.discount };
    if (!quiet) toast(`Coupon applied — ${money(r.discount)} off`);
  } catch (e) {
    COUPON = { code: "", discount: 0 };
    $("b-cperr").textContent = e.message;
  }
  renderCart();
}
function recheckCoupon() {
  if (!COUPON.code) return;
  clearTimeout(COUPON_TIMER);
  const code = COUPON.code;
  COUPON_TIMER = setTimeout(() => applyCoupon(code, true), 400);
}

function renderCart() {
  renderPicker();
  const box = $("b-cart");
  const empty = CART.length === 0;
  $("b-bar").hidden = empty;
  $("bs-2").classList.toggle("on", !empty && !!PICKUP);
  if (empty) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="cart">${CART.map((i, n) => {
    const kg = i.unit === "kg";
    return `<div class="cartrow"><div class="ci-line">
      <div class="ci-name">${esc(i.garment || i.service)} <small>${i.garment ? esc(i.service) : "kg"}</small>
        <span class="ci-rate">${money(i.rate)}${kg ? "/kg" : " each"}</span></div>
      <div class="stepper sm">
        <button type="button" class="step" data-q="${n}" data-by="${kg ? -0.5 : -1}" aria-label="${kg ? "Half kg less" : "One less"}">−</button>
        <input type="number" inputmode="decimal" data-qty="${n}" value="${i.qty}" min="${kg ? 0.5 : 1}" step="${kg ? 0.5 : 1}" aria-label="${kg ? "Weight in kg" : "How many"}">
        <button type="button" class="step" data-q="${n}" data-by="${kg ? 0.5 : 1}" aria-label="${kg ? "Half kg more" : "One more"}">+</button>
      </div>
      <b class="ci-amt">${money(i.qty * i.rate)}</b>
      <button class="rm" data-rm="${n}" aria-label="Remove">✕</button>
    </div></div>`;
  }).join("")}
    <div class="sums">
      ${COUPON.code ? `<div class="s-row"><span>Subtotal</span><span>${money(gross())}</span></div>
      <div class="s-row off"><span>Coupon ${esc(COUPON.code)} <button type="button" class="linkbtn" id="b-cprm">Remove</button></span><span>−${money(COUPON.discount)}</span></div>` : ""}
      <div class="s-row big"><span>Estimated total</span><span>${money(net())}</span></div></div></div>`;
  const rm = $("b-cprm");
  if (rm) rm.onclick = () => { COUPON = { code: "", discount: 0 }; $("b-coupon").value = ""; renderCart(); };
  box.querySelectorAll("[data-rm]").forEach((b) => { b.onclick = () => { CART.splice(parseInt(b.dataset.rm, 10), 1); recheckCoupon(); renderCart(); }; });
  box.querySelectorAll("[data-q]").forEach((b) => {
    b.onclick = () => {
      const n = parseInt(b.dataset.q, 10), i = CART[n];
      const next = Math.round((i.qty + parseFloat(b.dataset.by)) * 10) / 10;
      if (i.unit !== "kg" && next < 1) CART.splice(n, 1);
      else i.qty = Math.min(i.unit === "kg" ? 50 : 200, Math.max(i.unit === "kg" ? 0.5 : 1, next));
      recheckCoupon();
      renderCart();
    };
  });
  box.querySelectorAll("[data-qty]").forEach((inp) => {
    const n = parseInt(inp.dataset.qty, 10);
    inp.onchange = () => {
      const i = CART[n], v = parseFloat(inp.value);
      if (i.unit === "kg") i.qty = Math.min(50, Math.max(0.5, Math.round((v || 0.5) * 10) / 10));
      else i.qty = Math.min(200, Math.max(1, Math.round(v || 1)));
      recheckCoupon();
      renderCart();
    };
  });
  const n = CART.reduce((a, x) => a + (x.unit === "kg" ? 1 : x.qty), 0);
  $("b-barsum").innerHTML = `<b>${money(net())}</b><span>${n} item${n > 1 ? "s" : ""} · estimate${COUPON.code ? " · 🎟 " + esc(COUPON.code) : ""}</span>`;
  $("b-cpsum").textContent = COUPON.code ? `${COUPON.code} −${money(COUPON.discount)}` : "optional";
}

/* ─── order ─────────────────────────────────────────────────────────── */
async function save(btn) {
  const err = $("b-err");
  err.textContent = "";
  const name = $("b-name").value.trim(), address = $("b-addr").value.trim();
  if (!CART.length) { toast("Add at least one item", true); return; }
  if (!PICKUP) { err.textContent = "Pick the pickup day."; return; }
  if (!name) { err.textContent = "Please enter your name."; $("b-name").focus(); return; }
  if (address.length < 6) { err.textContent = "Please enter the full pickup address with a landmark."; $("b-addr").focus(); return; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Please wait…";
  try {
    const out = await api("/billcreation/api/order", {
      method: "POST",
      body: {
        t: TOKEN, s: SLUG, name, address, pickup_date: PICKUP,
        notes: ($("b-notes").value || "").trim(),
        coupon: COUPON.code,
        items: cartBody(),
      },
    });
    if (out.status === "confirm_on_whatsapp") return showWhatsApp(out);
    showPlaced(out);
  } catch (e) {
    err.textContent = e.message;
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function showPlaced(o) {
  CART = [];
  window.scrollTo(0, 0);
  $("list").innerHTML = `<div class="card bc-done">
    <div class="big">✅</div>
    <h2>Order placed</h2>
    <p class="num">${esc(o.order_number)}</p>
    <p>Estimated bill ${money(o.estimate)} · Pickup ${esc(o.pickup)}</p>
    <p>We've sent the details on WhatsApp. Our delivery partner will collect the clothes and make the final bill.</p>
  </div>`;
}

function showWhatsApp(o) {
  const href = `https://wa.me/${encodeURIComponent(o.whatsapp)}?text=${encodeURIComponent(o.text)}`;
  window.scrollTo(0, 0);
  $("list").innerHTML = `<div class="card bc-done">
    <div class="big">📲</div>
    <h2>One last step</h2>
    <p>Send this message on WhatsApp — your order is confirmed from your own number.</p>
    <p>Order code <b>${esc(o.code)}</b> · Estimate ${money(o.estimate)}</p>
    <a class="btn wa wide" href="${esc(href)}" rel="noopener">Send on WhatsApp</a>
    <p class="bc-hint">Don't change the message — the code links it to this order.</p>
  </div>`;
  // Khud redirect nahi: page chhoot jaata (desktop par WhatsApp Web khulta,
  // Back = khaali form). Bada button, ek tap.
}

boot();
