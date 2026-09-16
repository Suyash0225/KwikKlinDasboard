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
})();
