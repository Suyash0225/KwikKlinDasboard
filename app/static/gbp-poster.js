/* Google Business Profile poster: upload, publish, and a record of what went out.
 *
 * Opened from the dashboard nav (openGbpPoster() clicks the hidden launcher).
 * Every open starts a clean form — a half-finished poster from last time used
 * to reappear — unless a publish is still running. "Recent posts" lists every
 * attempt from the server (/history), so the owner can see later what was
 * published where and what failed.
 */
(() => {
  "use strict";
  const API = "/admin/api/google-business/poster";
  const DEFAULT_CAPTION = "Laundry and dry-cleaning care you can trust. Visit our website to learn more.";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (iso) => {
    try { return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); }
    catch (e) { return ""; }
  };

  const CSS = `
    #kk-gbp-poster-launch{display:none}
    #kk-gbp-poster-modal{display:none;position:fixed;inset:0;z-index:9999;background:rgba(15,23,42,.5);backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:16px}
    #kk-gbp-poster-modal.open{display:flex}
    .gp-card{background:#fff;color:#0f172a;width:min(640px,100%);max-height:92dvh;display:flex;flex-direction:column;border-radius:20px;box-shadow:0 30px 80px -20px rgba(15,23,42,.45);overflow:hidden;font-family:var(--font,system-ui)}
    .gp-head{display:flex;align-items:flex-start;gap:12px;padding:18px 20px 0}
    .gp-logo{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:#e8f0fe;color:#1a73e8;flex:0 0 auto}
    .gp-logo svg{width:22px;height:22px}
    .gp-head h2{margin:0;font-size:18px;letter-spacing:-.02em}
    .gp-head p{margin:2px 0 0;color:#64748b;font-size:13px}
    .gp-x{margin-left:auto;border:0;background:#f1f5f9;border-radius:10px;width:36px;height:36px;cursor:pointer;font-size:15px;color:#475569}
    .gp-tabs{display:flex;gap:4px;margin:14px 20px 0;padding:4px;background:#eef1f5;border-radius:12px}
    .gp-tabs button{flex:1;border:0;background:transparent;border-radius:9px;padding:8px;font:inherit;font-weight:600;font-size:13.5px;color:#475569;cursor:pointer}
    .gp-tabs button.on{background:#fff;color:#0f172a;box-shadow:0 1px 3px rgba(15,23,42,.12)}
    .gp-body{padding:16px 20px 20px;overflow:auto}
    .gp-label{display:block;font-weight:650;font-size:13px;margin:14px 0 7px;color:#0f172a}
    .gp-label small{font-weight:500;color:#64748b}
    .gp-drop{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;min-height:150px;border:1.5px dashed #cbd5e1;border-radius:16px;background:#f8fafc;cursor:pointer;text-align:center;padding:16px;transition:border-color .15s,background .15s}
    .gp-drop:hover,.gp-drop.over{border-color:#ea580c;background:#fff7ed}
    .gp-drop input{position:absolute;inset:0;opacity:0;cursor:pointer}
    .gp-drop b{font-size:14.5px}
    .gp-drop span{font-size:12.5px;color:#64748b}
    .gp-drop .gp-ico{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;background:#fff;border:1px solid #e2e8f0;color:#ea580c}
    .gp-preview{display:none;gap:12px;align-items:center;border:1px solid #e2e8f0;border-radius:16px;padding:10px}
    .gp-preview.on{display:flex}
    .gp-preview img{width:96px;height:96px;object-fit:cover;border-radius:12px;background:#f1f5f9}
    .gp-preview div{flex:1;min-width:0}
    .gp-preview b{display:block;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .gp-preview span{font-size:12px;color:#64748b}
    .gp-preview .gp-row{display:flex;gap:6px;margin-top:8px}
    .gp-btn{border:1px solid #e2e8f0;background:#fff;border-radius:10px;padding:7px 12px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;color:#334155}
    .gp-text{width:100%;box-sizing:border-box;font:inherit;font-size:14.5px;border:1px solid #e2e8f0;border-radius:12px;padding:11px 12px;min-height:96px;resize:vertical;color:inherit}
    .gp-text:focus{outline:none;border-color:#ea580c;box-shadow:0 0 0 3px rgba(234,88,12,.15)}
    .gp-count{text-align:right;font-size:11.5px;color:#94a3b8;margin-top:4px}
    .gp-cta{display:grid;grid-template-columns:1fr 1fr;gap:8px}
    .gp-cta label{display:flex;gap:10px;align-items:flex-start;border:1px solid #e2e8f0;border-radius:14px;padding:11px 12px;cursor:pointer;font-size:13px;color:#475569}
    .gp-cta label b{display:block;color:#0f172a;font-size:14px}
    .gp-cta input{margin-top:2px;accent-color:#ea580c;width:18px;height:18px;flex:0 0 auto}
    .gp-cta label:has(input:checked){border-color:#ea580c;background:#fff7ed}
    .gp-go{margin-top:16px;width:100%;border:0;border-radius:14px;min-height:50px;font:inherit;font-size:15.5px;font-weight:700;color:#fff;cursor:pointer;background:linear-gradient(145deg,#f97316,#c2410c);box-shadow:0 10px 22px -10px rgba(194,65,12,.7)}
    .gp-go:disabled{opacity:.65;cursor:wait}
    .gp-steps{display:none;margin-top:14px;border:1px solid #e2e8f0;border-radius:14px;padding:4px 12px}
    .gp-steps.on{display:block}
    .gp-step{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #f1f5f9;font-size:13.5px}
    .gp-step:last-child{border-bottom:0}
    .gp-step i{font-style:normal;width:20px;flex:0 0 auto;text-align:center}
    .gp-step small{display:block;color:#64748b;font-size:12px;margin-top:2px;overflow-wrap:anywhere}
    .gp-hist{display:flex;flex-direction:column;gap:10px}
    .gp-item{display:flex;gap:12px;border:1px solid #e2e8f0;border-radius:14px;padding:10px}
    .gp-item img{width:64px;height:64px;object-fit:cover;border-radius:10px;background:#f1f5f9;flex:0 0 auto}
    .gp-item div{flex:1;min-width:0}
    .gp-item p{margin:2px 0 6px;font-size:13px;color:#334155;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .gp-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:#64748b}
    .gp-pill{font-size:11.5px;font-weight:650;border-radius:999px;padding:2px 9px}
    .gp-pill.published{background:#e8f8ef;color:#15803d}.gp-pill.partial{background:#fef6e4;color:#b45309}.gp-pill.failed{background:#fdecef;color:#be123c}
    .gp-empty{text-align:center;color:#64748b;font-size:13.5px;padding:30px 10px}
    .gp-help{font-size:12px;color:#64748b;line-height:1.5;margin:8px 0 0}
    @media(max-width:560px){
      #kk-gbp-poster-modal{padding:0;align-items:flex-end}
      .gp-card{border-radius:22px 22px 0 0;max-height:94dvh}
      .gp-cta{grid-template-columns:1fr}
    }`;

  const ICON_G = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="m21 16-5-5-8 9"/></svg>';
  const ICON_UP = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>';

  const boot = () => {
    if (!document.body || document.getElementById("kk-gbp-poster-launch")) return;
    const style = document.createElement("style");
    style.id = "kk-gbp-poster-style";
    style.textContent = CSS;
    document.head.appendChild(style);

    const launch = document.createElement("button");
    launch.id = "kk-gbp-poster-launch";
    launch.type = "button";
    launch.textContent = "Google Business Poster";

    const modal = document.createElement("div");
    modal.id = "kk-gbp-poster-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "kk-gbp-poster-title");
    modal.innerHTML = `
      <section class="gp-card">
        <header class="gp-head">
          <span class="gp-logo">${ICON_G}</span>
          <div><h2 id="kk-gbp-poster-title">Google Business poster</h2>
            <p>Post a photo with a caption to every location on your Google listing.</p></div>
          <button type="button" class="gp-x" data-gp="close" aria-label="Close">✕</button>
        </header>
        <div class="gp-tabs" role="tablist">
          <button type="button" role="tab" data-gp-tab="new" class="on">New post</button>
          <button type="button" role="tab" data-gp-tab="history">Recent posts</button>
        </div>
        <div class="gp-body" data-gp-pane="new">
          <span class="gp-label">Poster image <small>· JPG, PNG or WEBP, up to 5 MB</small></span>
          <label class="gp-drop" id="gp-drop">
            <input id="kk-gbp-image" type="file" accept="image/png,image/jpeg,image/webp" aria-label="Choose poster image">
            <span class="gp-ico">${ICON_UP}</span>
            <b>Tap to choose a poster</b>
            <span>or drag an image here</span>
          </label>
          <div class="gp-preview" id="gp-preview">
            <img id="kk-gbp-poster-preview" alt="Selected poster">
            <div><b id="gp-fname"></b><span id="gp-fsize"></span>
              <div class="gp-row"><button type="button" class="gp-btn" data-gp="change">Change</button><button type="button" class="gp-btn" data-gp="remove">Remove</button></div></div>
          </div>
          <label class="gp-label" for="kk-gbp-caption">Caption <small>· optional</small></label>
          <textarea id="kk-gbp-caption" class="gp-text" maxlength="1200" placeholder="e.g. Diwali is close — get your silk sarees dry-cleaned this week."></textarea>
          <div class="gp-count"><span id="gp-count">0</span> / 1200</div>
          <span class="gp-label">Button on the post</span>
          <div class="gp-cta" role="radiogroup" aria-label="Button on the post">
            <label><input type="radio" name="gp-cta" value="WEBSITE" checked><span><b>Learn more</b>Opens your website</span></label>
            <label><input type="radio" name="gp-cta" value="CALL"><span><b>Call now</b>Calls your Google listing number</span></label>
          </div>
          <p class="gp-help">Google does not offer a WhatsApp button on posts. Blank caption = a short, safe default.</p>
          <div class="gp-steps" id="gp-steps" aria-live="polite"></div>
          <button type="button" class="gp-go" id="kk-gbp-poster-publish">Publish to Google</button>
        </div>
        <div class="gp-body" data-gp-pane="history" hidden>
          <div class="gp-hist" id="gp-hist"><div class="gp-empty">Loading…</div></div>
        </div>
      </section>`;
    document.body.append(launch, modal);

    const $ = (sel) => modal.querySelector(sel);
    const file = $("#kk-gbp-image"), drop = $("#gp-drop"), prev = $("#gp-preview"), img = $("#kk-gbp-poster-preview");
    const caption = $("#kk-gbp-caption"), go = $("#kk-gbp-poster-publish"), steps = $("#gp-steps");
    let previewUrl = "", chosen = null, busy = false;

    const tab = (name) => {
      modal.querySelectorAll("[data-gp-tab]").forEach((b) => b.classList.toggle("on", b.dataset.gpTab === name));
      modal.querySelectorAll("[data-gp-pane]").forEach((p) => { p.hidden = p.dataset.gpPane !== name; });
      if (name === "history") loadHistory();
    };
    const setFile = (f) => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = ""; chosen = null;
      if (!f) { prev.classList.remove("on"); drop.style.display = ""; img.removeAttribute("src"); file.value = ""; return; }
      if (!/^image\/(png|jpeg|webp)$/.test(f.type) || f.size > 5 * 1024 * 1024) {
        showSteps([["⚠️", "That file can't be used", "Choose a JPG, PNG or WEBP image up to 5 MB."]]);
        file.value = "";
        return;
      }
      chosen = f;
      previewUrl = URL.createObjectURL(f);
      img.src = previewUrl;
      $("#gp-fname").textContent = f.name;
      $("#gp-fsize").textContent = (f.size / 1048576).toFixed(1) + " MB";
      prev.classList.add("on");
      drop.style.display = "none";
      steps.classList.remove("on");
    };
    const reset = () => {
      setFile(null);
      caption.value = "";
      $("#gp-count").textContent = "0";
      modal.querySelector('input[name="gp-cta"][value="WEBSITE"]').checked = true;
      steps.classList.remove("on");
      steps.innerHTML = "";
    };
    const showSteps = (rows) => {
      steps.innerHTML = rows.map(([i, t, s]) => `<div class="gp-step"><i>${i}</i><div>${esc(t)}${s ? `<small>${esc(s)}</small>` : ""}</div></div>`).join("");
      steps.classList.add("on");
    };
    const open = () => {
      // A fresh form every time — unless a publish is still running.
      if (!busy) reset();
      tab("new");
      modal.classList.add("open");
    };
    const close = () => modal.classList.remove("open");

    async function loadHistory() {
      const box = $("#gp-hist");
      box.innerHTML = '<div class="gp-empty">Loading…</div>';
      try {
        const r = await fetch(API + "/history", { credentials: "same-origin" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Could not load");
        const items = d.items || [];
        box.innerHTML = items.length ? items.map((x) => `
          <div class="gp-item">
            ${x.poster_url ? `<img src="${esc(x.poster_url)}" alt="" loading="lazy">` : ""}
            <div>
              <div class="gp-meta"><span class="gp-pill ${esc(x.status)}">${x.status === "published" ? "Published" : x.status === "partial" ? "Partly published" : "Failed"}</span><span>${esc(when(x.at))}</span>${x.action ? `<span>· ${x.action === "CALL" ? "Call now" : "Learn more"}</span>` : ""}</div>
              <p>${esc(x.caption || "")}</p>
              <div class="gp-meta">${(x.published || []).length ? `✅ ${esc(x.published.join(", "))}` : ""}${(x.failed || []).length ? ` ⚠️ ${esc(x.failed.join("; "))}` : ""}</div>
            </div>
          </div>`).join("") : '<div class="gp-empty">No posts yet — your first one will show here with where it went out.</div>';
      } catch (e) {
        box.innerHTML = `<div class="gp-empty">Could not load history: ${esc(e.message)}</div>`;
      }
    }

    launch.addEventListener("click", open);
    modal.addEventListener("click", (e) => {
      const a = e.target.closest("[data-gp]");
      if (e.target === modal || (a && a.dataset.gp === "close")) close();
      else if (a && a.dataset.gp === "remove") setFile(null);
      else if (a && a.dataset.gp === "change") file.click();
      const t = e.target.closest("[data-gp-tab]");
      if (t) tab(t.dataset.gpTab);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modal.classList.contains("open")) close(); });
    file.addEventListener("change", () => setFile(file.files && file.files[0]));
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files && e.dataTransfer.files[0]));
    caption.addEventListener("input", () => { $("#gp-count").textContent = String(caption.value.length); });

    go.addEventListener("click", async () => {
      if (!chosen) { showSteps([["📷", "Choose a poster first", "Tap the box above to pick an image."]]); return; }
      const cta = modal.querySelector('input[name="gp-cta"]:checked').value;
      const text = caption.value.trim() || DEFAULT_CAPTION;
      busy = true; go.disabled = true; go.textContent = "Publishing…";
      showSteps([["⏳", "Uploading poster…"], ["○", "Publish to your locations"]]);
      try {
        const up = new FormData();
        up.append("image", chosen);
        const a = await fetch(API + "/upload", { method: "POST", body: up, credentials: "same-origin" });
        const u = await a.json();
        if (!a.ok) throw new Error(u.detail || "Poster upload failed");
        showSteps([["✅", "Poster uploaded"], ["⏳", "Publishing to your locations…"]]);
        const data = new FormData();
        data.append("caption", (cta === "CALL" ? "CALL NOW: " : "WEBSITE: ") + text);
        const b = await fetch(API + "/publish", { method: "POST", body: data, credentials: "same-origin" });
        const p = await b.json();
        if (!b.ok) throw new Error(p.detail || "Publishing failed");
        const ok = p.published || [], bad = p.failed || [];
        showSteps([
          ["✅", "Poster uploaded"],
          [bad.length ? (ok.length ? "⚠️" : "❌") : "✅",
           ok.length ? `Published to ${ok.length} location${ok.length > 1 ? "s" : ""}` : "Not published",
           [ok.length ? ok.join(", ") : "", bad.length ? "Failed: " + bad.join("; ") : ""].filter(Boolean).join(" · ")],
        ]);
        if (ok.length && !bad.length) { setFile(null); caption.value = ""; $("#gp-count").textContent = "0"; }
      } catch (e) {
        showSteps([["❌", "Not published", e && e.message ? e.message : "Request failed"], ["ℹ️", "Your poster is still selected — fix the issue and try again."]]);
      } finally {
        busy = false; go.disabled = false; go.textContent = "Publish to Google";
      }
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
})();
