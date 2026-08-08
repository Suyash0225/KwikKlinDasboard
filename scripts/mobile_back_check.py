"""Owner ki batayi dikkat ko dohrao: "Inbox kholte hi pehla chat khul jaata
hai aur back ka option kho jaata hai."

Shak: URL mein `#inbox/<phone>` bacha reh jaata hai (kyunki chat kholne par
hum wahi likhte hain). Agli baar page us hash ke saath load hota hai, chat
seedha khul jaata hai, aur history mein list ka koi kadam hota hi nahi —
isliye phone ka BACK button site se hi bahar phenk deta hai.
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from scripts.mobile_check import BASE, EMAIL, PASSWORD, PHONE, SHOTS


async def main(headed: bool) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=not headed)
        ctx = await browser.new_context(**PHONE)
        page = await ctx.new_page()

        await page.goto(f"{BASE}/#login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        await page.fill("#l-email", EMAIL)
        await page.fill("#l-pass", PASSWORD)
        await page.click("#l-go")
        await page.wait_for_url("**/admin*", timeout=20000)
        await page.wait_for_timeout(2000)

        async def st():
            return await page.evaluate("""() => ({
              chat_open: document.body.classList.contains('chat-open'),
              hash: location.hash,
              history_len: history.length,
            })""")

        # 1. Inbox -> ek chat kholo (asli user jaisa)
        await page.evaluate("go('inbox', false)")
        await page.wait_for_timeout(1800)
        await page.click(".thread-item")
        await page.wait_for_timeout(1500)
        print("  chat khola      :", await st())

        # 2. Ab page RELOAD karo — jaise agli baar aayenge
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        after = await st()
        print("  reload ke baad  :", after)
        await page.screenshot(path=str(SHOTS / "back-1-reload.png"))

        # 3. Phone ka BACK button
        await page.go_back()
        await page.wait_for_timeout(1500)
        back = await st()
        print("  BACK dabane par :", back, "| url:", page.url)
        await page.screenshot(path=str(SHOTS / "back-2-afterback.png"))

        print()
        print("  NATIJA:")
        if after["chat_open"]:
            print("   X reload par chat apne aap khul gaya (list nahi dikhi)")
        else:
            print("   OK reload par list dikhi")
        if "admin" not in page.url:
            print("   X back dabate hi site se bahar chale gaye")
        elif back["chat_open"]:
            print("   X back dabane par bhi chat khula hi raha")
        else:
            print("   OK back se list par wapas aa gaye")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main("--headed" in sys.argv))
