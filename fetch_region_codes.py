import asyncio
import json
import logging
from playwright.async_api import async_playwright

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

OUTPUT_FILE = "naver_region_codes.json"
BASE_URL = "https://fin.land.naver.com/regions"

# Selector for region buttons
REGION_BUTTON_SELECTOR = 'a[class*="RegionList_button"]'

async def main():
    async with async_playwright() as p:
        # Launch browser (Headless=False to see progress)
        # Use args to mimic real browser, set User-Agent
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        # Create context with explicit UA and Viewport
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()
        
        # 1. Get Si/Do List
        print("Fetching Si/Do list...")
        await page.goto(BASE_URL)
        
        # Reduced timeout but retry logic? 
        # Increase timeout to 10s
        try:
            # Wait for a known city to appear to confirm load
            await page.wait_for_selector("text=서울시", timeout=10000)
            # Find the container or buttons. 
            # Strategy: Get all elements that look like region buttons.
            # We will use a script to find elements with class containing 'RegionList_button'
        except:
             print("Error loading base page (Seoul not found). Taking screenshot...")
             await page.screenshot(path="debug_error.png")
             return

        si_links = await page.evaluate(f'''() => {{
            // Find all anchor tags or buttons that have 'RegionList_button' in class
            const allElements = Array.from(document.querySelectorAll('*'));
            const regionBtns = allElements.filter(el => el.className.includes && el.className.includes('RegionList_button'));
            
            return regionBtns.map(a => ({{name: a.textContent.trim(), url: a.href}}))
        }}''')
        
        full_data = {}
        
        for si in si_links:
            si_name = si['name']
            si_url = si['url']
            print(f"[{si_name}] Processing...")
            
            # Initialize Si Structure
            full_data[si_name] = {"url": si_url, "children": {}}
            
            # 2. Get Gun/Gu List for this Si
            await page.goto(si_url)
            try:
                await page.wait_for_selector(REGION_BUTTON_SELECTOR, timeout=2000)
                gun_links = await page.evaluate(f'''() => {{
                    return Array.from(document.querySelectorAll('{REGION_BUTTON_SELECTOR}')).map(a => ({{name: a.textContent, url: a.href}}))
                }}''')
            except:
                gun_links = []
                
            for gun in gun_links:
                gun_name = gun['name']
                gun_url = gun['url']
                # print(f"  > [{gun_name}]")
                
                full_data[si_name]["children"][gun_name] = {"url": gun_url, "children": {}}
                
                # 3. Get Dong List for this Gun
                await page.goto(gun_url)
                try:
                    await page.wait_for_selector(REGION_BUTTON_SELECTOR, timeout=2000)
                    dong_links = await page.evaluate(f'''() => {{
                        return Array.from(document.querySelectorAll('{REGION_BUTTON_SELECTOR}')).map(a => ({{name: a.textContent, url: a.href}}))
                    }}''')
                except:
                    dong_links = []
                    
                for dong in dong_links:
                    dong_name = dong['name']
                    dong_url = dong['url']
                    
                    # Store leaf node
                    full_data[si_name]["children"][gun_name]["children"][dong_name] = {"url": dong_url}
            
            # Save progress after each Si/Do completes
            print(f"  > Dictionary updated. Saving progress...")
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(full_data, f, ensure_ascii=False, indent=2)
                
        print(f"Done! Saved to {OUTPUT_FILE}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
