/* Owner poster upload and publish UI. Responsive, isolated from dashboard rendering. */
(() => {
  "use strict";
  const boot = () => {
    if (!document.body || document.getElementById("kk-gbp-poster-launch")) return;
    const style = document.createElement("style");
    style.id = "kk-gbp-poster-style";
    style.textContent = `
      #kk-gbp-poster-launch{position:fixed;right:18px;bottom:18px;z-index:9998;background:#0877d1;color:#fff;border:0;border-radius:14px;padding:13px 17px;font-weight:750;box-shadow:0 8px 28px #102a4333;cursor:pointer}
      #kk-gbp-poster-modal{display:none;position:fixed;inset:0;z-index:9999;background:#0b1220a6;align-items:center;justify-content:center;padding:clamp(8px,3vw,24px);backdrop-filter:blur(5px)}
      #kk-gbp-poster-card{background:var(--card,#fff);color:var(--text,#172033);width:min(620px,100%);max-height:92dvh;overflow:auto;border:1px solid #cbd5e155;border-radius:22px;padding:clamp(16px,3vw,26px);box-shadow:0 24px 80px #0004}
      #kk-gbp-poster-card label{display:block;font-weight:700;font-size:13px;margin:14px 0 7px}
      #kk-gbp-poster-card input,#kk-gbp-poster-card textarea,#kk-gbp-poster-card select{font:inherit;color:inherit;background:var(--input-bg,var(--card,#fff));border:1px solid var(--border,#d7deea);border-radius:12px;padding:12px;width:100%;box-sizing:border-box}
      #kk-gbp-poster-card textarea{resize:vertical;min-height:104px}
      #kk-gbp-poster-preview{display:none;max-width:100%;max-height:34dvh;object-fit:contain;border-radius:14px;margin:12px auto 4px}
      #kk-gbp-poster-publish{background:#0877d1;color:white;border:0;border-radius:12px;padding:13px 16px;font-weight:800;width:100%;cursor:pointer}
      #kk-gbp-poster-publish:disabled{opacity:.6;cursor:wait}
      #kk-gbp-poster-result{font-size:14px;line-height:1.5;overflow-wrap:anywhere}
      @media(max-width:520px){#kk-gbp-poster-launch{right:10px;bottom:10px;padding:12px 13px}#kk-gbp-poster-modal{padding:8px;align-items:flex-end}#kk-gbp-poster-card{border-radius:20px 20px 12px 12px;max-height:94dvh}}
    `;
    document.head.appendChild(style);
    const btn = document.createElement("button");
    btn.id = "kk-gbp-poster-launch";
    btn.type = "button";
    btn.textContent = "Google Business Poster";
    const modal = document.createElement("div");
    modal.id = "kk-gbp-poster-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "kk-gbp-poster-title");
    modal.innerHTML = `
      <section id="kk-gbp-poster-card">
        <header style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
          <div><div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#0877d1;font-weight:800">KWIK KLIN · GROWTH</div>
          <h2 id="kk-gbp-poster-title" style="margin:5px 0;font-size:clamp(20px,4vw,26px);line-height:1.2">Google Business Profile</h2>
          <p style="margin:0;color:var(--muted,#64748b);font-size:14px">Poster + caption + CTA. Publish to all configured locations.</p></div>
          <button type="button" id="kk-gbp-close" aria-label="Close poster dialog" style="border:1px solid var(--border,#d7deea);background:transparent;border-radius:10px;padding:8px 11px;cursor:pointer">✕</button>
        </header>
        <label for="kk-gbp-image">Poster image <span style="font-weight:400;color:var(--muted,#64748b)">· PNG/JPG, max 5 MB</span></label>
        <input id="kk-gbp-image" type="file" accept="image/png,image/jpeg,image/webp,image/*">
        <img id="kk-gbp-poster-preview" alt="Selected poster preview">
        <label for="kk-gbp-caption">Post caption <span style="font-weight:400;color:var(--muted,#64748b)">· optional</span></label>
        <textarea id="kk-gbp-caption" maxlength="1200" placeholder="Write your caption, or leave blank for a short safe default."></textarea>
        <label for="kk-gbp-cta">Call-to-action button</label>
        <select id="kk-gbp-cta">
          <option value="WEBSITE">Learn more · Website</option>
          <option value="CALL">Call now</option>
        </select>
        <p style="font-size:12px;line-height:1.5;color:var(--muted,#64748b);margin:8px 0 14px">Website uses the configured website link. Call now uses the connected Business Profile phone number. WhatsApp CTA is not offered here because Google does not expose a WhatsApp post button in every profile.</p>
        <p id="kk-gbp-poster-result" role="status" aria-live="polite"></p>
        <button id="kk-gbp-poster-publish" type="button">Upload &amp; publish to all locations</button>
      </section>`;
    document.body.append(btn, modal);
    const close = () => { modal.style.display = "none"; };
    btn.addEventListener("click", () => { modal.style.display = "flex"; });
    modal.querySelector("#kk-gbp-close").addEventListener("click", close);
    modal.addEventListener("click", e => { if (e.target === modal) close(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && modal.style.display === "flex") close(); });
    const file = modal.querySelector("#kk-gbp-image");
    const preview = modal.querySelector("#kk-gbp-poster-preview");
    const result = modal.querySelector("#kk-gbp-poster-result");
    const publish = modal.querySelector("#kk-gbp-poster-publish");
    const captionField = modal.querySelector("#kk-gbp-caption");
    let previewUrl = "";
    const clearSelection = () => {
      file.value = "";
      captionField.value = "";
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = "";
      preview.removeAttribute("src");
      preview.style.display = "none";
    };
    file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = "";
      if (!f) { preview.style.display = "none"; preview.removeAttribute("src"); return; }
      if (!f.type.startsWith("image/") || f.size > 5 * 1024 * 1024) {
        clearSelection();
        result.textContent = "Please choose an image file up to 5 MB.";
        return;
      }
      previewUrl = URL.createObjectURL(f);
      preview.src = previewUrl;
      preview.style.display = "block";
      result.textContent = "";
    });
    publish.addEventListener("click", async () => {
      const f = file.files && file.files[0];
      const caption = captionField.value.trim() || "Kwik Klin laundry care in Varanasi. Visit our website to learn more.";
      const cta = modal.querySelector("#kk-gbp-cta").value;
      if (!f) { result.textContent = "Choose a poster image first."; file.focus(); return; }
      publish.disabled = true;
      publish.textContent = "Publishing…";
      result.textContent = "Uploading and validating poster…";
      try {
        const upload = new FormData();
        upload.append("image", f);
        const a = await fetch("/admin/api/google-business/poster/upload", {method:"POST", body:upload, credentials:"same-origin"});
        const u = await a.json();
        if (!a.ok) throw new Error(u.detail || "Poster upload failed.");
        result.textContent = "Poster uploaded. Publishing to configured locations…";
        const data = new FormData();
        data.append("caption", (cta === "CALL" ? "CALL NOW: " : "WEBSITE: ") + caption);
        const b = await fetch("/admin/api/google-business/poster/publish", {method:"POST", body:data, credentials:"same-origin"});
        const p = await b.json();
        if (!b.ok) throw new Error(p.detail || "Publishing failed.");
        const ok = p.published || [], bad = p.failed || [];
        result.textContent = "Published: " + (ok.length ? ok.join(", ") : "none") + (bad.length ? " · Failed: " + bad.join("; ") : "") + ".";
        if (ok.length) clearSelection();
      } catch (e) {
        result.textContent = "Not fully published: " + (e && e.message ? e.message : "Request failed");
      } finally {
        publish.disabled = false;
        publish.textContent = "Upload & publish to all locations";
      }
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once:true});
  else boot();
})();
