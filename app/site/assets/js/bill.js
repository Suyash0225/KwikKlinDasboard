/* Customer bill page: pick the right UPI deep link for the device.
   Android -> intent:// (opens that exact app), iPhone -> app scheme,
   desktop -> no app to open, so show the QR to scan with a phone. */
(function () {
  var offerTimer = null;
  function money(v) { return "₹" + Number(v || 0).toLocaleString("en-IN", {maximumFractionDigits: 2}); }
  function setAmountInLink(href, amount) {
    if (!href) return href;
    return href.replace(/([?&])am=[^&#]*/i, "$1am=" + Number(amount).toFixed(2));
  }
  function applyOffer(data) {
    var pay = document.querySelector(".card.pay");
    if (!pay || !data || data.paid || data.expired && !data.active) return;
    var due = pay.querySelector(".amount");
    var label = pay.querySelector(".due");
    var note = pay.querySelector(".note");
    var timer = document.getElementById("payment-offer-timer");
    var save = document.getElementById("payment-offer-save");
    if (data.active) {
      pay.classList.add("limited-offer");
      if (label) label.innerHTML = "⚡ Pay now & save " + money(data.discount_amount);
      if (due) due.innerHTML = money(data.offer_amount);
      if (save) save.textContent = "Regular " + money(data.original_amount) + " · You save " + money(data.discount_amount);
      if (!timer) {
        timer = document.createElement("div"); timer.id = "payment-offer-timer"; timer.className = "offer-timer";
        pay.insertBefore(timer, pay.querySelector(".apps") || pay.firstChild);
      }
      document.querySelectorAll(".card.pay a[href], .card.pay a[data-android], .card.pay a[data-ios]").forEach(function (a) {
        if (a.hasAttribute("href")) a.href = setAmountInLink(a.getAttribute("href"), data.offer_amount);
        if (a.hasAttribute("data-android")) a.setAttribute("data-android", setAmountInLink(a.getAttribute("data-android"), data.offer_amount));
        if (a.hasAttribute("data-ios")) a.setAttribute("data-ios", setAmountInLink(a.getAttribute("data-ios"), data.offer_amount));
      });
      var left = Number(data.remaining_seconds || 0);
      clearInterval(offerTimer);
      var tick = function () {
        if (left <= 0) { clearInterval(offerTimer); offerTimer = null; location.reload(); return; }
        var mm = String(Math.floor(left / 60)).padStart(2, "0"), ss = String(left % 60).padStart(2, "0");
        timer.textContent = "⏱️ Offer expires in " + mm + ":" + ss;
        left -= 1;
      };
      tick(); offerTimer = setInterval(tick, 1000);
    } else if (data.expired) {
      pay.classList.remove("limited-offer");
      if (label) label.textContent = "Amount due";
      if (due) due.textContent = money(data.original_amount);
      if (save) save.textContent = "Offer expired · regular amount applies";
      if (timer) { timer.textContent = "⏰ Offer expired"; timer.classList.add("expired"); }
      if (note) note.textContent = "The 15-minute special offer has ended. Regular bill amount applies.";
    }
  }
  function startPaymentOffer() {
    var qs = new URLSearchParams(location.search), offer = qs.get("o") || qs.get("offer");
    if (!offer || !window.fetch) return;
    var token = location.pathname.split("/b/")[1] || "";
    if (!token || token.indexOf("/") >= 0) return;
    fetch("/payment-offers/open?bill=" + encodeURIComponent(token) + "&o=" + encodeURIComponent(offer), {cache:"no-store", credentials:"omit"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var pay = document.querySelector(".card.pay");
        if (pay && !document.getElementById("payment-offer-save")) {
          var s = document.createElement("div"); s.id = "payment-offer-save"; s.className = "offer-save";
          pay.insertBefore(s, pay.querySelector(".apps") || pay.firstChild);
        }
        applyOffer(data);
      }).catch(function () {});
  }

  var ua = navigator.userAgent || "";
  var android = /Android/i.test(ua);
  var ios = /iPhone|iPad|iPod/i.test(ua);
  if (!android && !ios) document.body.classList.add("is-desktop");
  document.querySelectorAll("[data-android]").forEach(function (a) {
    if (android) a.href = a.getAttribute("data-android");
    else if (ios) a.href = a.getAttribute("data-ios");
  });
  startPaymentOffer();
  var btn = document.getElementById("copy-vpa");
  if (btn && navigator.clipboard) {
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(btn.getAttribute("data-vpa")).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      });
    });
  } else if (btn) {
    btn.hidden = true;
  }

  /* Live: the shop moves the order (picked up, washing, ready, delivered)
     or records a payment -> this open page refreshes itself. Tiny poll,
     only while the tab is visible; also re-checks when the customer
     comes back to the tab. */
  var body = document.body, live = body.getAttribute("data-live"), v = body.getAttribute("data-v");
  if (live && v && window.fetch) {
    var busy = false;
    var check = function () {
      if (busy || document.hidden) return;
      busy = true;
      fetch(live, { cache: "no-store", credentials: "omit" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j && j.v && j.v !== v) location.reload(); })
        .catch(function () {})
        .then(function () { busy = false; });
    };
    setInterval(check, 15000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
  }
})();
