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

async def check_url(context, url):
    """
    Visit URL and check if complex items exist.
    """
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        
        # Wait for either complex item or no result
        # We try to find the item first. 
        try:
            # If we find a complex item within timeout, it's valid
            await page.wait_for_selector(SELECTOR_COMPLEX_ITEM, timeout=5000, state="attached")
            return True
        except:
            # If timeout, it means no complex item found quickly.
            # Double check if "No Result" text is present or just empty.
            # For robustness, we assume False if selector not found.
            return False
            
    except Exception as e:
        # logging.error(f"Error checking {url}: {e}")
        return False
    finally:
        await page.close()

def flatten_nodes(data):
    """Recursively collect leaf nodes with URLs."""
    tasks = []
    
    def traverse(d, path):
        if "children" in d:
            for k, v in d["children"].items():
                traverse(v, path + [k])
        elif "url" in d:
            tasks.append((path, d))
            
    for k, v in data.items():
        traverse(v, [k])
        
    return tasks

async def main():
    print("📂 Loading region codes...")
    with open(REGION_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_tasks = flatten_nodes(data)
    # Optimization: Filter out already FALSE regions? No, we validate monthly so check everything.
    # But for debugging speed, maybe we only check known likely ones?
    # User wants FULL sync.
    
    print(f"🚀 Found {len(all_tasks)} regions to validate.")
    
    results = {}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Create a single context (or multiple if needed, but context per batch is safer)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
