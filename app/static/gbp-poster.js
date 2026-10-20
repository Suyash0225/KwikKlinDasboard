/* Owner poster upload and publish UI. Loaded separately so dashboard changes stay isolated. */
(() => {
  "use strict";
  const boot = () => {
    if (!document.body || document.getElementById("kk-gbp-poster-launch")) return;
    const btn = document.createElement("button");
    btn.id = "kk-gbp-poster-launch";
    btn.type = "button";
    btn.textContent = "Google Business Poster";
    Object.assign(btn.style, {position:"fixed",right:"18px",bottom:"18px",zIndex:"9998",background:"#0b6fcb",color:"#fff",border:"0",borderRadius:"12px",padding:"12px 16px",fontWeight:"700",boxShadow:"0 4px 18px #0003",cursor:"pointer"});
    const modal = document.createElement("div");
    modal.id = "kk-gbp-poster-modal";
    Object.assign(modal.style,{display:"none",position:"fixed",inset:"0",zIndex:"9999",background:"#0008",alignItems:"center",justifyContent:"center",padding:"16px"});
    modal.innerHTML = '<section style="background:var(--card,#fff);color:var(--text,#172033);width:min(560px,100%);max-height:90vh;overflow:auto;border-radius:16px;padding:20px;box-shadow:0 12px 40px #0004"><div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><h2 style="margin:0">Google Business Profile Poster</h2><button type="button" id="kk-gbp-close" aria-label="Close">✕</button></div><p>Upload your poster, add a caption, then publish to all configured locations.</p><label for="kk-gbp-image">Poster image (max 5 MB)</label><input id="kk-gbp-image" type="file" accept="image/*" style="display:block;margin:8px 0 14px;width:100%"><img id="kk-gbp-preview" alt="Poster preview" style="display:none;max-width:100%;max-height:260px;object-fit:contain;border-radius:8px;margin-bottom:12px"><label for="kk-gbp-caption">Post caption</label><textarea id="kk-gbp-caption" rows="4" maxlength="1200" placeholder="Write what you want to post…" style="display:block;width:100%;box-sizing:border-box;margin:8px 0 14px;padding:10px"></textarea><p id="kk-gbp-result" role="status" aria-live="polite"></p><button id="kk-gbp-publish" type="button" style="background:#0b6fcb;color:#fff;border:0;border-radius:9px;padding:11px 16px;font-weight:700">Upload & Publish to all locations</button></section>';
    document.body.append(btn,modal);
    const close=()=>{modal.style.display="none";};
    btn.addEventListener("click",()=>{modal.style.display="flex";});
    modal.querySelector("#kk-gbp-close").addEventListener("click",close);
    modal.addEventListener("click",e=>{if(e.target===modal)close();});
    const file=modal.querySelector("#kk-gbp-image"), preview=modal.querySelector("#kk-gbp-preview"), result=modal.querySelector("#kk-gbp-result"), publish=modal.querySelector("#kk-gbp-publish");
    file.addEventListener("change",()=>{const f=file.files&&file.files[0];if(!f){preview.style.display="none";return;} preview.src=URL.createObjectURL(f);preview.style.display="block";});
    publish.addEventListener("click",async()=>{
      const f=file.files&&file.files[0], caption=modal.querySelector("#kk-gbp-caption").value.trim();
      if(!f){result.textContent="Choose a poster image first.";return;}
      if(!caption){result.textContent="Add a caption first.";return;}
      publish.disabled=true; result.textContent="Uploading and validating poster…";
      try{
        const upload=new FormData();upload.append("image",f);
        const a=await fetch("/admin/api/google-business/poster/upload",{method:"POST",body:upload,credentials:"same-origin"});
        const u=await a.json();if(!a.ok)throw new Error(u.detail||"Poster upload failed.");
        result.textContent="Poster saved. Publishing to configured locations…";
        const data=new FormData();data.append("caption",caption);
        const b=await fetch("/admin/api/google-business/poster/publish",{method:"POST",body:data,credentials:"same-origin"});
        const p=await b.json();if(!b.ok)throw new Error(p.detail||"Publishing failed.");
        const ok=(p.published||[]),bad=(p.failed||[]);
        result.textContent="Published: "+(ok.length?ok.join(", "):"none")+(bad.length?" | Failed: "+bad.join("; "):"")+".";
      }catch(e){result.textContent="Error: "+(e&&e.message?e.message:"Request failed");}
      finally{publish.disabled=false;}
    });
  };
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot,{once:true});else boot();
})();
