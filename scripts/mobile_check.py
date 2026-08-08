"""Mobile view khud chala kar dekho — guess mat karo.

Chrome ko phone ke size par chalata hai, login karta hai, har section
kholta hai, screenshot leta hai, aur jo toota hai wo likh deta hai.

Run:
    .venv\\Scripts\\python.exe -m scripts.mobile_check
    .venv\\Scripts\\python.exe -m scripts.mobile_check --headed   (aankhon se dekho)
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
EMAIL = "suyash@kwikklin.local"
PASSWORD = "kwikklin2026"
SHOTS = Path(__file__).resolve().parent.parent / "app" / "media" / "mobile-shots"

# Galaxy-class phone: wahi 360px jo aapke report mein tha
PHONE = {
    "viewport": {"width": 360, "height": 800},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
    ),
}

SECTIONS = [
    "dashboard", "inbox", "newbill", "bills", "customers",
    "expenses", "reports", "tasks", "agents", "settings",
]

# Har section ke baad yahi sawaal poochhe jaate hain
AUDIT_JS = """() => {
  const vw = innerWidth, vh = innerHeight, out = [];
  const name = (e) => e.tagName.toLowerCase()
    + (e.id ? '#' + e.id : '')
    + (e.className && typeof e.className === 'string'
        ? '.' + e.className.trim().split(/\\s+/).slice(0,2).join('.') : '');
  let coverBottom = 0;
  for (const f of document.querySelectorAll('*')) {
    const st = getComputedStyle(f);
    if (st.position !== 'fixed' || st.display === 'none') continue;
    const r = f.getBoundingClientRect();
    if (r.bottom >= vh - 2 && r.height > 0 && r.height < vh/2) coverBottom = Math.max(coverBottom, r.height);
  }
  for (const e of document.querySelectorAll('body *')) {
    const st = getComputedStyle(e);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right <= 0 || r.left >= vw) continue;      // chhupa hua drawer
    if (st.position === 'fixed') continue;
    const txt = (e.textContent || '').trim().slice(0, 45);
    if (r.right > vw + 1)
      out.push({why:'right', el:name(e), right:Math.round(r.right), w:Math.round(r.width), txt});
    else if (e.scrollWidth > e.clientWidth + 2 && e.clientWidth > 0
             && !['auto','scroll'].includes(st.overflowX) && txt
             && st.textOverflow !== 'ellipsis')
      out.push({why:'clipped', el:name(e), need:e.scrollWidth, has:e.clientWidth, txt});
    else if (coverBottom && r.top < vh && r.bottom > vh - coverBottom
             && r.height < 200 && txt && !e.children.length)
      out.push({why:'under_tabbar', el:name(e), bottom:Math.round(r.bottom), txt});
    else if (parseFloat(st.fontSize) < 11 && txt && !e.children.length)
      out.push({why:'tiny_text', el:name(e), font:st.fontSize, txt});
  }
  const taps = [];
  for (const e of document.querySelectorAll('button,a,input,select,.thread-item,.nav div,.tabbar div,.chip')) {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.height < 36 || r.width < 36)
      taps.push({el:name(e), w:Math.round(r.width), h:Math.round(r.height),
                 txt:(e.textContent||'').trim().slice(0,25)});
  }
  return {
    side_scroll: document.documentElement.scrollWidth > vw + 1,
    scrollW: document.documentElement.scrollWidth, vw,
    overflow: out.slice(0, 20),
    taps: taps.slice(0, 15),
  };
}"""


async def main(headed: bool) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    findings: dict = {}
    errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=not headed)
        ctx = await browser.new_context(**PHONE)
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"JS: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text[:120]}")
                if m.type == "error" else None)

        # login
        await page.goto(f"{BASE}/#login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        await page.fill("#l-email", EMAIL)
        await page.fill("#l-pass", PASSWORD)
        await page.click("#l-go")
        await page.wait_for_url("**/admin*", timeout=20000)
        await page.wait_for_timeout(2500)
        print(f"  login: {page.url}")

        for sec in SECTIONS:
            await page.evaluate(f"go('{sec}', false)")
            await page.wait_for_timeout(1600)
            res = await page.evaluate(AUDIT_JS)
            findings[sec] = res
            await page.screenshot(path=str(SHOTS / f"{sec}.png"), full_page=False)
            bits = {}
            for o in res["overflow"]:
                bits[o["why"]] = bits.get(o["why"], 0) + 1
            print(f"  {sec:10} side_scroll={res['side_scroll']!s:5} "
                  f"{bits or 'layout ok'} taps<36px={len(res['taps'])}")

        # --- owner ki batayi hui dikkat: Inbox kholte hi chat khul jaata hai ---
        await page.evaluate("go('dashboard', false)")
        await page.wait_for_timeout(800)
        await page.evaluate("go('inbox', false)")
        await page.wait_for_timeout(2500)
        state = await page.evaluate("""() => ({
          chat_open: document.body.classList.contains('chat-open'),
          open_phone: window.OPEN_PHONE || null,
          back_visible: !!document.querySelector('.chat-back')
              && getComputedStyle(document.querySelector('.chat-back')).display !== 'none',
          threads_visible: getComputedStyle(document.querySelector('.threads')).display !== 'none',
          chatpane_visible: getComputedStyle(document.querySelector('.chatpane')).display !== 'none',
          hash: location.hash,
        })""")
        findings["_inbox_entry"] = state
        await page.screenshot(path=str(SHOTS / "inbox-entry.png"))
        print("\n  INBOX kholte hi:", json.dumps(state))

        # ek chat kholo, phir back dabao
        try:
            await page.click(".thread-item", timeout=5000)
            await page.wait_for_timeout(1800)
            opened = await page.evaluate(
                "() => ({chat_open: document.body.classList.contains('chat-open'),"
                " back: !!document.querySelector('.chat-back')})"
            )
            await page.screenshot(path=str(SHOTS / "inbox-chat.png"))
            await page.click(".chat-back", timeout=5000)
            await page.wait_for_timeout(1200)
            back = await page.evaluate(
                "() => ({chat_open: document.body.classList.contains('chat-open'),"
                " threads: getComputedStyle(document.querySelector('.threads')).display})"
            )
            await page.screenshot(path=str(SHOTS / "inbox-after-back.png"))
            findings["_chat_open"] = opened
            findings["_after_back"] = back
            print("  chat khola   :", json.dumps(opened))
            print("  back ke baad :", json.dumps(back))
        except Exception as exc:
            findings["_chat_error"] = str(exc)[:200]
            print("  chat test fail:", str(exc)[:150])

        findings["_errors"] = errors[:15]
        await browser.close()

    (SHOTS / "findings.json").write_text(
        json.dumps(findings, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n  Screenshots + findings: {SHOTS}")
    if errors:
        print("  JS errors:", errors[:5])


if __name__ == "__main__":
    asyncio.run(main("--headed" in sys.argv))
