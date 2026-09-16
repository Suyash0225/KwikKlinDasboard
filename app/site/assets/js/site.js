/* Kwik Klin public website (/laundry) — behaviour.
   Loaded with `defer`; server data comes from <script id="site-data" type="application/json">. */
/* The real assistant is the WhatsApp agent inside the CRM (it answers from
   the live rate card and order data). This page routes people to it with a
   useful first message. */
const WA_NUMBER = "919696856069";
// Server-rendered data (CRM rate card) — a JSON tag, not code, so this file stays cacheable
const SITE_DATA = JSON.parse(document.getElementById("site-data").textContent || "{}");
const POPULAR = SITE_DATA.popular || [];
const wa = (text) => "https://wa.me/" + WA_NUMBER + "?text=" + encodeURIComponent(text);
const WA_TEXT = {
  hello: "Hello Kwik Klin, I have a question.",
  pickup: "Hello Kwik Klin, I would like to book a pickup.",
  track: "Hello Kwik Klin, I would like to track my order.",
  franchise: "Hello Kwik Klin, I am interested in a franchise. Please share the details.",
  crm: "Hello Kwik Klin, I run a laundry and would like a demo of the CRM.",
};
document.querySelectorAll("[data-wa]").forEach((a) => {
  a.href = wa(WA_TEXT[a.dataset.wa]);
  a.target = "_blank"; a.rel = "noopener";
});
document.getElementById("yr").textContent = new Date().getFullYear();

/* mobile menu closes after a tap */
const menu = document.querySelector(".menu");
menu.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => menu.removeAttribute("open")));

/* ---------- rate tabs (content is in the HTML for SEO) ---------- */
const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab, focus) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  if (focus) tab.focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t));
  t.addEventListener("keydown", (e) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (d) { e.preventDefault(); selectTab(tabs[(i + d + tabs.length) % tabs.length], true); }
  });
});

/* ---------- booking form -> WhatsApp ---------- */
document.getElementById("book").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = (id) => document.getElementById(id).value.trim();
  const err = document.getElementById("b-err");
  const phone = v("b-phone").replace(/\D/g, "").slice(-10);
  const fail = (msg, id) => { err.textContent = msg; document.getElementById(id).focus(); };
  if (!v("b-name")) return fail("Please enter your name.", "b-name");
  if (!/^[6-9]\d{9}$/.test(phone)) return fail("Please enter a valid 10-digit mobile number.", "b-phone");
  if (!v("b-area")) return fail("Please enter your locality in Varanasi.", "b-area");
  err.textContent = "";
  const msg = "Hello Kwik Klin, I would like to book a pickup.\n" +
    "Name: " + v("b-name") + "\nMobile: " + phone + "\nLocality: " + v("b-area") + ", Varanasi" +
    "\nService: " + v("b-service") + "\nPreferred pickup: " + v("b-when");
  window.open(wa(msg), "_blank", "noopener");
});

/* ---------- CRM prices stay in sync with /api/plans ---------- */
fetch("/api/plans").then((r) => r.ok ? r.json() : null).then((d) => {
  (d && d.plans || []).forEach((p) => {
    const el = document.querySelector('[data-plan="' + p.code + '"] [data-price]');
    if (el && p.price_inr) el.textContent = "₹" + Number(p.price_inr).toLocaleString("en-IN");
  });
}).catch(() => {});

/* ---------- live Google reviews (/api/public/reviews, cached server-side) ---------- */
const starStr = (n) => "★★★★★".slice(0, Math.round(n)) + "☆☆☆☆☆".slice(0, 5 - Math.round(n));
const moreBtn = document.getElementById("rev-more");
if (moreBtn) moreBtn.onclick = () => {
  document.querySelectorAll(".review[data-more]").forEach((r) => (r.hidden = false));
  moreBtn.parentElement.remove();
};
// Business Profile reviews already rendered by the server -> Places fallback not needed
if (!document.querySelector("[data-gbp]")) fetch("/api/public/reviews").then((r) => r.ok ? r.json() : null).then((d) => {
  if (!d || !d.configured) return;
  if (d.rating) {
    document.getElementById("gcard").hidden = false;
    document.getElementById("g-rating").textContent = Number(d.rating).toFixed(1);
    document.getElementById("g-stars").textContent = starStr(d.rating);
    document.getElementById("g-count").textContent =
      "Based on " + Number(d.count || 0).toLocaleString("en-IN") + " Google reviews";
  }
  const good = (d.reviews || []).filter((r) => r.rating >= 4).slice(0, 6);
  if (!good.length) return;
  const gl = document.querySelector(".glogo");
  document.getElementById("review-list").replaceChildren(...good.map((r) => {
    const card = document.createElement("article"); card.className = "review";
    const s = document.createElement("div"); s.className = "stars"; s.textContent = starStr(r.rating);
    s.setAttribute("aria-label", r.rating + " out of 5 stars");
    const p = document.createElement("p");
    p.textContent = r.text.length > 280 ? r.text.slice(0, 277).trimEnd() + "…" : r.text;
    const who = document.createElement("div"); who.className = "who";
    let av;
    if (r.author_photo) {
      av = document.createElement("img"); av.src = r.author_photo; av.alt = ""; av.loading = "lazy";
      av.referrerPolicy = "no-referrer"; av.width = 40; av.height = 40;
    } else {
      av = document.createElement("span"); av.className = "av"; av.textContent = (r.author || "G")[0].toUpperCase();
    }
    const meta = document.createElement("div");
    const name = document.createElement(r.author_url ? "a" : "b");
    name.textContent = r.author;
    if (r.author_url) { name.href = r.author_url; name.target = "_blank"; name.rel = "noopener"; name.className = "author"; }
    const when = document.createElement("small"); when.textContent = r.when + " · Google";
    meta.append(name, when);
    who.append(av, meta, gl.cloneNode(true));
    card.append(s, p, who);
    return card;
  }));
}).catch(() => {});

/* ---------- keep Google's rating card visible on narrow screens ---------- */
const mapBox = document.querySelector(".map");
function fitMap() {
  const w = mapBox.clientWidth;
  mapBox.classList.toggle("scaled", w > 0 && w < 400);
  mapBox.style.setProperty("--s", Math.min(1, w / 400));
}
fitMap();
if ("ResizeObserver" in window) new ResizeObserver(fitMap).observe(mapBox);
else addEventListener("resize", fitMap);

/* ---------- count-up on the stats strip ---------- */
const counters = document.querySelectorAll("[data-count]");
if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const co = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (!en.isIntersecting) return;
    co.unobserve(en.target);
    const el = en.target, end = +el.dataset.count, t0 = performance.now();
    const tick = (t) => {
      const k = Math.min(1, (t - t0) / 1400), eased = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(end * eased).toLocaleString("en-IN") + (k === 1 ? el.dataset.suffix : "");
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }), { threshold: .6 });
  counters.forEach((c) => co.observe(c));
}

/* ---------- reveal on scroll ---------- */
const revealEls = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
  }), { rootMargin: "0px 0px -8% 0px" });
  revealEls.forEach((el) => io.observe(el));
} else {
  revealEls.forEach((el) => el.classList.add("in"));
}

/* ---------- assistant widget ---------- */
const chat = document.getElementById("chat");
const fab = document.getElementById("fab");
const chatBody = document.getElementById("chat-body");
const chips = document.getElementById("chips");

function say(text, who, waKey, waLabel) {
  const m = document.createElement("div");
  m.className = "msg " + (who || "bot");
  m.textContent = text;
  if (waKey) {
    const a = document.createElement("a");
    a.className = "btn btn-wa"; a.target = "_blank"; a.rel = "noopener";
    a.href = wa(WA_TEXT[waKey]); a.textContent = waLabel || "Continue on WhatsApp";
    m.appendChild(a);
  }
  chatBody.appendChild(m);
  chatBody.scrollTop = chatBody.scrollHeight;
}
const TOPICS = {
  "Book a pickup": () => say("Happy to help! Share your name, locality and preferred time on WhatsApp and our assistant will confirm your pickup slot right away.", "bot", "pickup", "Book pickup on WhatsApp"),
  "Rate list": () => {
    say(POPULAR.length
      ? "Popular prices:\n• " + POPULAR.join("\n• ") + "\n\nThe full list is on this page."
      : "Our assistant shares the latest rates on WhatsApp — the rate list is also on this page.");
    toggleChat(false);
    document.getElementById("rates").scrollIntoView();
  },
  "Track my order": () => say("Message us on WhatsApp from the number you ordered with — the assistant will share your live order status instantly.", "bot", "track", "Track on WhatsApp"),
  "Service area": () => say("Our laundry pickup and delivery service is available in Varanasi only.\n\nOutside Varanasi? Franchises and our laundry CRM are open in every city."),
  "Franchise": () => say("Franchises are open in every city across India. Partners get the brand, operating processes, training and our CRM with the AI assistant built in.", "bot", "franchise", "Enquire on WhatsApp"),
  "CRM for my laundry": () => say("Our CRM starts at ₹999/month with a 7-day free trial — orders, bills, staff app and an AI assistant that answers your customers on WhatsApp 24×7.", "bot", "crm", "Book a free demo"),
  "Talk to our team": () => say("Our team is available on WhatsApp at +91 96968 56069.", "bot", "hello", "Open WhatsApp"),
};
Object.keys(TOPICS).forEach((label) => {
  const b = document.createElement("button");
  b.className = "chip"; b.type = "button"; b.textContent = label;
  b.onclick = () => { say(label, "me"); setTimeout(TOPICS[label], 350); };
  chips.appendChild(b);
});
let greeted = false;
function toggleChat(open) {
  chat.classList.toggle("open", open);
  chat.setAttribute("aria-hidden", !open);
  fab.setAttribute("aria-expanded", open);
  fab.classList.toggle("hide", open);
  if (open && !greeted) {
    greeted = true;
    say("Hi! 👋 Welcome to Kwik Klin, doorstep laundry in Varanasi.\nHow can I help you today?");
  }
}
fab.onclick = () => toggleChat(!chat.classList.contains("open"));
document.getElementById("mchat").onclick = () => toggleChat(!chat.classList.contains("open"));
document.querySelectorAll('.mbar a').forEach((a) => a.addEventListener("click", () => toggleChat(false)));
document.getElementById("chat-x").onclick = () => toggleChat(false);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleChat(false); });
