/* Customer bill page: pick the right UPI deep link for the device.
   Android -> intent:// (opens that exact app), iPhone -> app scheme,
   desktop -> no app to open, so show the QR to scan with a phone. */
(function () {
  var ua = navigator.userAgent || "";
  var android = /Android/i.test(ua);
  var ios = /iPhone|iPad|iPod/i.test(ua);
  if (!android && !ios) document.body.classList.add("is-desktop");
  document.querySelectorAll("[data-android]").forEach(function (a) {
    if (android) a.href = a.getAttribute("data-android");
    else if (ios) a.href = a.getAttribute("data-ios");
  });
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
