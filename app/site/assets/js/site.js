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
const attributionText = () => {
  const params = new URLSearchParams(window.location.search);
  const allowed = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
  const values = allowed.map((key) => [key, (params.get(key) || "").trim().slice(0, 100)])
    .filter((pair) => pair[1]);
  return ["Lead source: website", ...values.map(([key, value]) => key + ": " + value)].join("\n");
};
const trackLeadEvent = (name, params = {}) => {
  if (typeof window.gtag === "function") window.gtag("event", name, params);
};
const WA_TEXT = {
  hello: "Hello Kwik Klin, I have a question.",
  pickup: "Hello Kwik Klin, I would like to book a pickup.",
  track: "Hello Kwik Klin, I would like to track my order.",
  franchise: "Hello Kwik Klin, I am interested in a franchise. Please share the details.",
  crm: "Hello Kwik Klin, I run a laundry and would like a demo of the CRM.",
};
document.querySelectorAll("[data-wa]").forEach((a) => {
  a.href = wa(WA_TEXT[a.dataset.wa] + "\n" + attributionText());
  a.target = "_blank"; a.rel = "noopener";
  a.addEventListener("click", () => trackLeadEvent("whatsapp_click", {cta_type: a.dataset.wa || "unknown"}));
});
document.getElementById("yr").textContent = new Date().getFullYear();

/* mobile menu: closes after a tap on a link, a tap anywhere outside,
   Escape, or scrolling — not only on the ✕ */
const menu = document.querySelector(".menu");
if (menu) {
  const closeMenu = () => menu.removeAttribute("open");
  menu.querySelectorAll("a").forEach((a) => a.addEventListener("click", closeMenu));
  document.addEventListener("pointerdown", (e) => { if (menu.hasAttribute("open") && !menu.contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  window.addEventListener("scroll", () => { if (menu.hasAttribute("open")) closeMenu(); }, { passive: true });
  window.addEventListener("resize", closeMenu);
}

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
    "\nService: " + v("b-service") + "\nPreferred pickup: " + v("b-when") + "\n" + attributionText();
  trackLeadEvent("generate_lead", {lead_source: "website", service_type: v("b-service")});
  trackLeadEvent("whatsapp_click", {cta_type: "booking_form"});
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

/* ---------- compact Google review cards + accessible full-review modal ---------- */
const reviewModal = document.getElementById("review-modal");
const reviewDialog = reviewModal?.querySelector(".review-modal__dialog");
const reviewModalStars = document.getElementById("review-modal-stars");
const reviewModalTitle = document.getElementById("review-modal-title");
const reviewModalText = document.getElementById("review-modal-text");
const reviewModalReply = document.getElementById("review-modal-reply");
const reviewModalReplyText = reviewModalReply?.querySelector("p");
let reviewModalReturnFocus = null;

function openReviewModal(card, trigger) {
  if (!reviewModal || !reviewDialog) return;
  reviewModalReturnFocus = trigger || null;

  const rating = Number(card.dataset.rating || 0);
  const name = card.dataset.reviewName || card.querySelector(".who b, .who .author")?.textContent?.trim() || "Google reviewer";
  const fullText = card.dataset.fullText || card.querySelector(".review-text, p")?.textContent?.trim() || "";
  const fullReply = card.dataset.fullReply || card.querySelector(".reply p")?.textContent?.trim() || "";

  reviewModalStars.textContent = starStr(rating);
  reviewModalStars.setAttribute("aria-label", rating + " out of 5 stars");
  reviewModalTitle.textContent = name;
  reviewModalText.textContent = fullText;

  if (fullReply) {
    reviewModalReply.hidden = false;
    reviewModalReplyText.textContent = fullReply;
  } else {
    reviewModalReply.hidden = true;
    reviewModalReplyText.textContent = "";
  }

  reviewModal.hidden = false;
  reviewModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("review-modal-open");
  requestAnimationFrame(() => reviewDialog.focus());
}

function closeReviewModal() {
  if (!reviewModal || reviewModal.hidden) return;
  reviewModal.hidden = true;
  reviewModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("review-modal-open");
  if (reviewModalReturnFocus?.isConnected) reviewModalReturnFocus.focus();
  reviewModalReturnFocus = null;
}

function enhanceServerReviewCards() {
  document.querySelectorAll(".review").forEach((card) => {
    if (card.dataset.reviewEnhanced === "true") return;
    const textEl = card.querySelector(".review-text, p");
    if (!textEl) return;

    card.dataset.reviewEnhanced = "true";
    card.dataset.rating = card.querySelector(".stars")?.getAttribute("aria-label")?.match(/\d+(?:\.\d+)?/)?.[0] || "";
    card.dataset.reviewName = card.querySelector(".who b, .who .author")?.textContent?.trim() || "Google reviewer";
    card.dataset.fullText = textEl.textContent?.trim() || "";
    const replyText = card.querySelector(".reply")?.textContent?.replace(/^Response from the owner\s*/i, "").trim() || "";
    card.dataset.fullReply = replyText;

    const needsMore = card.dataset.fullText.length > 180 || replyText.length > 100;
    if (needsMore && !card.querySelector(".review-read-more")) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "review-read-more";
      button.textContent = "Read more →";
      textEl.insertAdjacentElement("afterend", button);
    }
  });
}

enhanceServerReviewCards();

function initReviewCarousel() {
  const reviews = document.querySelector(".reviews[data-gbp], .reviews");
  if (!reviews || reviews.dataset.carouselReady === "true") return;
  const cards = [...reviews.querySelectorAll(".review")];
  if (!cards.length) return;

  cards.forEach((card) => { card.hidden = false; card.removeAttribute("data-more"); });
  document.querySelector(".more-wrap")?.remove();

  const viewport = document.createElement("div");
  viewport.className = "reviews-viewport";
  reviews.parentNode.insertBefore(viewport, reviews);
  viewport.appendChild(reviews);

  const controls = document.createElement("div");
  controls.className = "review-carousel-controls";
  controls.setAttribute("aria-label", "Customer reviews carousel");

  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "review-carousel-btn";
  prev.setAttribute("aria-label", "Previous reviews");
  prev.textContent = "‹";

  const next = document.createElement("button");
  next.type = "button";
  next.className = "review-carousel-btn";
  next.setAttribute("aria-label", "Next reviews");
  next.textContent = "›";

  const dots = document.createElement("div");
  dots.style.display = "flex";
  dots.style.gap = "7px";
  dots.style.alignItems = "center";

  controls.append(prev, dots, next);
  viewport.insertAdjacentElement("afterend", controls);

  let page = 0;
  let visible = 3;
  let timer;

  const getVisible = () => window.matchMedia("(max-width:640px)").matches ? 1
    : window.matchMedia("(max-width:980px)").matches ? 2 : 3;
  const getPages = () => Math.max(1, Math.ceil(cards.length / visible));

  function renderDots() {
    dots.replaceChildren();
    for (let i = 0; i < getPages(); i++) {
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "review-carousel-dot" + (i === page ? " active" : "");
      dot.setAttribute("aria-label", "Show review group " + (i + 1));
      dot.onclick = () => { page = i; render(); restart(); };
      dots.appendChild(dot);
    }
  }

  function render() {
    visible = getVisible();
    const pages = getPages();
    page = Math.min(page, pages - 1);

    const cardWidth = cards[0].getBoundingClientRect().width;
    const gap = visible === 1 ? 14 : 18;
    const step = (cardWidth + gap) * visible;

    reviews.style.transform = "translateX(-" + (page * step) + "px)";
    [...dots.children].forEach((d, i) => d.classList.toggle("active", i === page));
    prev.disabled = page === 0;
    next.disabled = page === pages - 1;
  }

  function restart() {
    clearInterval(timer);
    if (getPages() > 1) {
      timer = setInterval(() => {
        page = page >= getPages() - 1 ? 0 : page + 1;
        render();
      }, 5500);
    }
  }

  prev.onclick = () => { page = Math.max(0, page - 1); render(); restart(); };
  next.onclick = () => { page = Math.min(getPages() - 1, page + 1); render(); restart(); };

  reviews.dataset.carouselReady = "true";
  renderDots();
  requestAnimationFrame(() => { render(); restart(); });
  window.addEventListener("resize", () => { renderDots(); render(); restart(); }, { passive: true });
}
initReviewCarousel();

document.addEventListener("click", (e) => {
  const trigger = e.target.closest(".review-read-more");
  if (trigger) {
    e.preventDefault();
    const card = trigger.closest(".review");
    if (card) openReviewModal(card, trigger);
    return;
  }
  if (e.target.closest("[data-review-close]")) closeReviewModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && reviewModal && !reviewModal.hidden) closeReviewModal();
});

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
    const card = document.createElement("article");
    card.className = "review";
    card.dataset.rating = r.rating;
    card.dataset.reviewName = r.author || "Google reviewer";
    card.dataset.fullText = r.text || "";

    const s = document.createElement("div");
    s.className = "stars";
    s.textContent = starStr(r.rating);
    s.setAttribute("aria-label", r.rating + " out of 5 stars");

    const p = document.createElement("p");
    p.className = "review-text";
    p.textContent = r.text || "";

    const readMore = document.createElement("button");
    readMore.type = "button";
    readMore.className = "review-read-more";
    readMore.textContent = "Read more →";

    const who = document.createElement("div");
    who.className = "who";
    let av;
    if (r.author_photo) {
      av = document.createElement("img");
      av.src = r.author_photo;
      av.alt = "";
      av.loading = "lazy";
      av.referrerPolicy = "no-referrer";
      av.width = 40;
      av.height = 40;
    } else {
      av = document.createElement("span");
      av.className = "av";
      av.textContent = (r.author || "G")[0].toUpperCase();
    }

    const meta = document.createElement("div");
    const name = document.createElement(r.author_url ? "a" : "b");
    name.textContent = r.author;
    if (r.author_url) {
      name.href = r.author_url;
      name.target = "_blank";
      name.rel = "noopener";
      name.className = "author";
    }
    const when = document.createElement("small");
    when.textContent = (r.when ? r.when + " · " : "") + "Google Review";
    meta.append(name, when);
    who.append(av, meta, gl.cloneNode(true));
    card.append(s, p, readMore, who);
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
