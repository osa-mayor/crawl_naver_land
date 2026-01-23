import asyncio
import json
import logging
from playwright.async_api import async_playwright
import os

# Configuration
REGION_FILE = "naver_region_codes.json"
MAX_CONCURRENT_TABS = 5  # Reduced concurrency to ensure stability
TIMEOUT_MS = 10000

# Selectors (Borrowed from land_selectors.py/crawler.py)
SELECTOR_COMPLEX_ITEM = "li[class*='ComplexItem']" 
SELECTOR_NO_RESULT = "div[class*='no_result'], div[class*='no_data']" # Approximate, check crawler logic if needed

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Emulate Desktop Browser (Exact match to crawler.py)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            is_mobile=False,
            has_touch=False,
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
            }
        )
        
        # Process in chunks
        chunk_size = MAX_CONCURRENT_TABS
        
        total = len(all_tasks)
        for i in range(0, total, chunk_size):
            chunk = all_tasks[i : i + chunk_size]
            
            # Create tasks
            aws = []
            for path, node in chunk:
                aws.append(check_url(context, node["url"]))
            
            # Run batch
            flags = await asyncio.gather(*aws)
            
            # Store results
            for (path, node), flag in zip(chunk, flags):
                results[tuple(path)] = flag
            
            if (i + chunk_size) % 100 < chunk_size:
                print(f"Progress: {min(i + chunk_size, total)}/{total} ({(min(i + chunk_size, total)/total)*100:.1f}%)")
        
        await browser.close()

async def check_url(context, url):
    """
    Visit URL and check if complex items exist.
    """
    page = await context.new_page()
    # Stealth
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    """)
    
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Robust Wait: Wait for API response that loads the list
        try:
            async with page.expect_response(lambda response: "complex" in response.url and response.status == 200, timeout=5000) as response_info:
                pass
        except:
            pass # Proceed to check selectors even if API verify fails

        # Fallback short wait
        if not page.is_closed():
            await page.wait_for_timeout(2000)
            
        # Check selectors
        selectors = [
            "li[class*='ComplexItem'][class*='article']", 
            "div[class*='list_complex']",
            "button[class*='complex_link']",
            "li[class*='ComplexItem']"
        ]
        
        for sel in selectors:
            try:
                if await page.query_selector(sel):
                    return True
            except: continue
            
        return False
            
    except Exception as e:
        return False
    finally:
        await page.close()

    # Update Data Structure
    print("💾 Updating JSON structure...")
    
    def apply_updates(node, current_path):
        is_leaf = "children" not in node
        
        if is_leaf:
            flag = results.get(tuple(current_path), False)
            node["has_complexes"] = flag
            return flag
        else:
            any_child_valid = False
            for k, v in node["children"].items():
                child_valid = apply_updates(v, current_path + [k])
                if child_valid: any_child_valid = True
            
            node["has_complexes"] = any_child_valid
            return any_child_valid

    for k, v in data.items():
        apply_updates(v, [k])

    print("📝 Saving to file...")
    with open(REGION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # Summary of True regions
    true_count = sum(1 for v in results.values() if v)
    print(f"✅ Validation Complete! Found {true_count} regions with complexes.")

if __name__ == "__main__":
    asyncio.run(main())
