import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        urls = [
            "https://fin.land.naver.com/regions?si=1100000000",
            "https://fin.land.naver.com/regions?si=5100000000&gun=5115000000",
        ]

        for url in urls:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = resp.status if resp else "No response"
                title = await page.title()
                print(f"OK {url} -> Status: {status}, Title: {title}")
            except Exception as e:
                print(f"FAIL {url} -> Error: {e}")

        await browser.close()

asyncio.run(test())
