
import json
import requests
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

REGION_FILE = "naver_region_codes.json"
MAX_WORKERS = 10  # Moderate concurrency to be polite
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get_complex_count(url):
    """
    Fetches the region page and mimics the API call or parses initial state
    to determine if there are any complexes.
    Actually, Naver Land uses a separate API for the list.
    Let's extract the region codes from URL and call the API directly for speed.
    URL: https://fin.land.naver.com/regions?si=...&gun=...&eup=...
    API: https://fin.land.naver.com/api/regions/complexes?CortarNo={cortar_no}&RealEstateType=APT%3AABYG%3AJGC%3APRE&TradeType=A1
    
    We need 'CortarNo'.
    The 'eup' param in URL usually corresponds to the specific region code (CortarNo).
    Let's try to infer CortarNo from URL params.
    """
    try:
        # Parse params
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        
        # Determine CortarNo (most specific one)
        cortar_no = None
        if 'eup' in qs: cortar_no = qs['eup'][0]
        elif 'gun' in qs: cortar_no = qs['gun'][0]
        elif 'si' in qs: cortar_no = qs['si'][0]
        
        if not cortar_no: return False
        
        # Construct API URL
        # We need to include 'PRE' (Presale) as we added it to filter.
        # Types: APT (Apt), ABYG (Bunyangkwon - wait, code might be different), 
        # Actually, let's just check 'APT' and 'ABYG' (Bunyangkwon) and 'JGC' (Reconstruction) and 'PRE' (Pre-sale?).
        # Safest is to check the general count.
        
        api_url = f"https://fin.land.naver.com/api/regions/complexes?CortarNo={cortar_no}&RealEstateType=APT:ABYG:JGC&TradeType=A1"
        
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": url
        }
        
        resp = requests.get(api_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # The API returns a list of complexes.
            # complexList
            complexes = data.get("complexList", [])
            return len(complexes) > 0
        return False
    except Exception as e:
        print(f"Error checking {url}: {e}")
        return False

def check_node(node_key, node_data):
    """
    Recursive check.
    Returns (updated_node, has_complexes)
    """
    has_complexes = False
    
    # If leaf
    if "url" in node_data and "children" not in node_data:
        has_complexes = get_complex_count(node_data["url"])
        output_node = node_data.copy()
        output_node["has_complexes"] = has_complexes
        print(f"Checked {node_key}: {'✅' if has_complexes else '❌'}")
        # Sleep slightly to avoid strict rate limit if single thread, but we are parallelizing.
        return output_node, has_complexes

    # If non-leaf (has children)
    if "children" in node_data:
        new_children = {}
        # We can parallelize processing children if there are many
        # For simplicity in recursion, let's process sequentially here or use a helper
        # But actually, we want to iterate the whole tree.
        
        # Let's verify children
        child_has_any = False
        for k, v in node_data["children"].items():
            updated_child, child_ok = check_node(k, v)
            new_children[k] = updated_child
            if child_ok: child_has_any = True
        
        output_node = node_data.copy()
        output_node["children"] = new_children
        output_node["has_complexes"] = child_has_any # Propagate up?
        # Actually user wants to flag the LEAVES primarily.
        # But flagging intermediate nodes helps us skip entire branches.
        return output_node, child_has_any
        
    return node_data, False

def flatten_nodes(data):
    """
    Yields (path_tuple, node_data) for all leaves to be processed.
    path_tuple = ('경기도', '성남시', '분당구', '구미동')
    node_return needs to act as a pointer? 
    It's hard to update the massive JSON inplace with threads.
    
    Strategy:
    1. Collect all LEAF tasks.
    2. Run them in parallel.
    3. Update the main dict.
    4. Then propagate status up.
    """
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

def main():
    print("📂 Loading region codes...")
    with open(REGION_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    tasks = flatten_nodes(data)
    print(f"🚀 Found {len(tasks)} regions to validate.")
    
    results = {} # path_tuple -> bool
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(get_complex_count, item[1]["url"]): item[0] for item in tasks}
        
        count = 0
        for future in as_completed(future_map):
            path = future_map[future]
            try:
                exists = future.result()
                results[tuple(path)] = exists
                count += 1
                if count % 100 == 0:
                    print(f"Progress: {count}/{len(tasks)} ({count/len(tasks)*100:.1f}%)")
            except Exception as e:
                print(f"Failed {path}: {e}")
                results[tuple(path)] = False # Default to false on error? Or Keep?

    # Update Data Structure
    print("💾 Updating JSON structure...")
    
    def update_recursive(d, path_stack):
        # We are at a node.
        # If it's a leaf (check if path_stack is fully consumed if we track by path?)
        # Better: traverse 'data' again, recreate flags.
        
        # But wait, we need to lookup result by path.
        # It's better to construct a new data or modify in place.
        pass

    # Re-traverse to update
    def apply_updates(node, current_path):
        is_leaf = "children" not in node
        
        if is_leaf:
            flag = results.get(tuple(current_path), False)
            node["has_complexes"] = flag
            return flag
        else:
            # Intermediate
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
    
    print("✅ Validation Complete!")

if __name__ == "__main__":
    main()
