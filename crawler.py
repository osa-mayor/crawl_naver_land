import asyncio
import re
import json
import random
import pandas as pd
from playwright.async_api import async_playwright
import logging
from datetime import datetime
import os
import sqlite3
from land_selectors import NaverLandSelectors

# ==========================================
# [Configuration]
# ==========================================
# Target Regions (Search by Name using naver_region_codes.json)
TARGET_REGIONS = [
    "서울시", "경기도", "인천시", "부산시", "대구시", "광주시", "대전시", "울산시", "세종시",
    "강원도", "충청북도", "충청남도", "전라북도", "전라남도", "경상북도", "경상남도", "제주도"
]

# (Optional) RAW URLs override or addition
TARGET_URLS = []

# Filtering Options
MIN_HOUSEHOLDS = 100
EXCLUDE_LOW_FLOORS = False

# [System Config]
MAX_CONCURRENT_PAGES = int(os.getenv("MAX_CONCURRENT_PAGES", 3))  # Configurable via Env Var
HEADLESS_MODE = True      # Set to False to watch process
DB_PATH = "real_estate.db" # SQLite Database File

# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("crawling_db.log", encoding="utf-8")
    ]
)

# User-Agent List for Stealth
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
]

class DataProcessor:
    @staticmethod
    def is_low_floor(floor_info: str) -> bool:
        if not floor_info: return False
        target_floors = ["1", "2", "3", "저"]
        floor_str = floor_info.split("/")[0].strip()
        if floor_str in target_floors: return True
        if floor_str.isdigit() and int(floor_str) <= 3: return True
        return False

    @staticmethod
    def format_price(num):
        if num == 0: return "-"
        eok = num // 100000000
        remainder = num % 100000000
        man = remainder // 10000
        if man > 0: return f"{eok}억 {man:,}"
        return f"{eok}억"

class NaverLandPlaywright:
    def __init__(self):
        self.complexes = {}
        self.captured_articles = {}
        
    def get_context_options(self):
        ua = random.choice(USER_AGENTS)
        return {
            "user_agent": ua,
            "viewport": {"width": 1280, "height": 720},
            "extra_http_headers": {
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
            }
        }

    async def run_test(self, target_urls, headless=True):
        """Main execution with Parallelism"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
            
            # Semaphore for limiting concurrency
            sem = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
            
            async def worker(item):
                dong_name, url = item
                async with sem:
                    # New Context per URL (Isolated cookies, Random UA)
                    context = await browser.new_context(
                        **self.get_context_options()
                    )
                    
                    # Stealh scripts
                    page = await context.new_page()
                    await page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """)
                    
                    try:
                        logging.info(f"🚀 Processing: {dong_name} ({url})")
                        await asyncio.sleep(random.uniform(0.5, 1.5)) # Random start delay
                        await self.process_region_tab(page, url, dong_name)
                    except Exception as e:
                        logging.error(f"❌ Failed processing {url}: {e}")
                    finally:
                        await context.close()

            tasks = [worker(item) for item in target_urls]
            if tasks:
                await asyncio.gather(*tasks)
            else:
                logging.warning("No URLs to crawl.")

            await browser.close()

    async def process_region_tab(self, page, target_url, dong_name):
        """Logic for processing a single region tab (was inside the loop previously)"""
        try:
            # 1. Go to Region
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(random.uniform(2000, 3000))
        except Exception as e:
            logging.error(f"Failed to load region page {target_url}: {e}")
            return

        # 2. Load All Complexes (Pagination)
        logging.info("Loading full list...")
        while True:
            try:
                more_btn = page.locator(NaverLandSelectors.MORE_BUTTON)
                if await more_btn.is_visible(timeout=2000):
                    await more_btn.click()
                    await page.wait_for_timeout(random.uniform(300, 700)) # Random click interval
                else:
                    break
            except:
                break

        # 3. Extract Complexes
        try:
            await page.wait_for_selector(NaverLandSelectors.COMPLEX_ITEM, timeout=10000)
        except:
            logging.warning("Timeout waiting for complex list (or empty).")

        complex_items = await page.query_selector_all(NaverLandSelectors.COMPLEX_ITEM)
        filtered_cids = []

        for item in complex_items:
            try:
                # Link & CID
                link_el = await item.query_selector(NaverLandSelectors.COMPLEX_LINK)
                if not link_el: continue
                href = await link_el.get_attribute("href")
                match = re.search(r"/complexes/(\d+)", href)
                if not match: continue
                cid = match.group(1)

                # Name
                name_el = await item.query_selector(NaverLandSelectors.COMPLEX_NAME)
                name = await name_el.inner_text() if name_el else f"Complex_{cid}"

                # Filters
                is_apt = False
                badge_el = await item.query_selector(NaverLandSelectors.COMPLEX_BADGE)
                if badge_el:
                    badge_text = await badge_el.inner_text()
                    if "아파트" in badge_text and "오피스텔" not in badge_text:
                        is_apt = True
                
                if not is_apt: continue

                # Households
                households = 0
                info_items = await item.query_selector_all(NaverLandSelectors.COMPLEX_INFO)
                for info in info_items:
                    text = await info.inner_text()
                    if "세대" in text:
                        h_match = re.search(r"(\d[\d,]*)\s*세대", text)
                        if h_match:
                            households = int(h_match.group(1).replace(",", ""))
                            break
                
                if households < MIN_HOUSEHOLDS: continue

                self.complexes[cid] = {
                    "name": name, 
                    "households": households,
                    "_dong_name": dong_name
                }
                filtered_cids.append(cid)
            except Exception as e:
                continue
        
        logging.info(f"Target Count in Region: {len(filtered_cids)}")
        if not filtered_cids: return

        # ========================================================
        # [OPTIMIZATION] Parallel Fetch of Details (Complex & Pyeong)
        # ========================================================
        async def fetch_one_complex(cid):
            # 1. Complex Detail
            if cid not in self.complexes or "totalHouseholdNumber" not in self.complexes[cid]:
                try:
                    api_url = f"https://fin.land.naver.com/front-api/v1/complex?complexNumber={cid}"
                    # Use page.request for sharing context/cookies
                    api_res = await page.request.get(api_url)
                    if api_res.status == 200:
                        data = await api_res.json()
                        if "result" in data:
                            new_data = data["result"]
                            # Preserve custom fields from loop
                            if cid in self.complexes and isinstance(self.complexes[cid], dict):
                                new_data["_dong_name"] = self.complexes[cid].get("_dong_name")
                            self.complexes[cid] = new_data
                except: pass

            # 2. Pyeong List
            if cid in self.complexes and "pyeongs" not in self.complexes[cid]:
                try:
                    pyeong_url = f"https://fin.land.naver.com/front-api/v1/complex/pyeongList?complexNumber={cid}"
                    p_res = await page.request.get(pyeong_url)
                    if p_res.status == 200:
                        p_data = await p_res.json()
                        if "result" in p_data:
                            self.complexes[cid]["pyeongs"] = p_data["result"]
                except: pass

        logging.info(f"⚡ Pre-fetching details for {len(filtered_cids)} complexes concurrently...")
        
        # Limit concurrency to 20 to be polite/safe
        sem_api = asyncio.Semaphore(20)
        
        async def sem_task(cid):
            async with sem_api:
                await fetch_one_complex(cid)

        await asyncio.gather(*[sem_task(c) for c in filtered_cids])
        logging.info("✅ Pre-fetch complete.")
        # ========================================================

        # 4. API Interception Setup (Context-specific)
        async def handle_response(response):
            try:
                url = response.url
                if "front-api/v1" in url and response.status == 200:
                    # Filter out the detail APIs we just called manually to avoid noise
                    if "pyeongList" in url or "/complex?" in url: return

                    # DEBUG_API: {url}
                    data = await response.json()
                    
                    # 2. Article List API
                    items = []
                    if "result" in data:
                        res = data["result"]
                        if isinstance(res, list): items = res
                        elif isinstance(res, dict) and "list" in res: items = res["list"]
                    
                    if items:
                        # Extract CID from URL or POST data
                        found_cid = None
                        match = re.search(r"complexNumber=(\d+)", url)
                        if match: found_cid = match.group(1)
                        
                        if not found_cid:
                            try:
                                post = response.request.post_data_json
                                if post and "complexNumber" in post: found_cid = str(post["complexNumber"])
                            except: pass

                        if found_cid:
                            if found_cid not in self.captured_articles: self.captured_articles[found_cid] = []
                            self.captured_articles[found_cid].extend(items)
            except: pass

        page.on("response", handle_response)

        # 5. Visit Details (Now faster because details are cached)
        # We still visit to trigger Article List interception
        for cid in filtered_cids:
            # Check if we already have detailed info (we should)
            # Just logs progress
            
            for t_type in ["A1", "B1"]: # Trade, Jeonse
                detail_url = f"https://fin.land.naver.com/complexes/{cid}?tab=article&tradeType={t_type}&articleTradeTypes={t_type}&articleSortingType=PRICE_ASC"
                
                try:
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
                    
                    # Wait less time now since we don't need to fetch complex info
                    await page.wait_for_timeout(random.uniform(500, 1000))

                    # Scroll
                    last_height = await page.evaluate("document.body.scrollHeight")
                    no_change = 0
                    for _ in range(30): # Reduced scroll max
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(random.uniform(300, 600))
                        new_height = await page.evaluate("document.body.scrollHeight")
                        if new_height == last_height:
                            no_change += 1
                            if no_change >= 2: break
                        else:
                            no_change = 0
                        last_height = new_height
                except Exception as e:
                    logging.warning(f"Nav error {cid}: {e}")

    def process_data(self):
        processor = DataProcessor()
        results = []

        logging.info(f"Aggregating data for {len(self.captured_articles)} complexes...")

        for cid, articles_or_groups in self.captured_articles.items():
            complex_info = self.complexes.get(cid, "")
            
            cname = str(cid)
            household_count_from_list = 0
            
            if isinstance(complex_info, dict):
                cname = complex_info.get("name", str(cid))
                household_count_from_list = complex_info.get("households", 0)
            elif complex_info:
                cname = str(complex_info)
            
            # Flatten
            flat_articles = []
            for item in articles_or_groups:
                if "articleInfoList" in item: flat_articles.extend(item["articleInfoList"])
                elif "representativeArticleInfo" in item: flat_articles.append(item["representativeArticleInfo"])
                else: flat_articles.append(item)
            
            if not flat_articles: continue
            
            # Group by Pyeong
            groups = {}
            for art in flat_articles:
                # [Same filtering logic as before...]
                space = art.get("spaceInfo", {})
                if not space and "supplySpaceName" in art: space = art
                s_name = space.get("supplySpaceName", str(space.get("supplySpace", "")))
                e_name = space.get("exclusiveSpaceName", str(space.get("exclusiveSpace", "")))
                ptp_key = f"{s_name}_{e_name}"
                
                if ptp_key not in groups: groups[ptp_key] = {"trade": [], "rent": [], "info": art}
                
                t_type = art.get("tradeType", "")
                
                # Floor extraction (Simplified Copy)
                floor_str = "-"
                floor_info = art.get("floorDetailInfo")
                if not floor_info:
                     detail = art.get("articleDetail", {})
                     floor_info = detail.get("floorDetailInfo")
                     if not floor_info: floor_str = detail.get("floorInfo", "-")
                
                if floor_info:
                    floor_str = f"{floor_info.get('targetFloor','')}/{floor_info.get('totalFloor','')}"

                art["_mapped_price"] = 0
                # Price extraction logic
                price_info = art.get("priceInfo", {})
                price = 0
                if t_type in ["A1", "매매"]:
                     price = price_info.get("dealPrice", 0) if price_info else art.get("dealPrice", 0)
                elif t_type in ["B1", "전세"]:
                     price = price_info.get("warrantyPrice", 0) if price_info else art.get("warrantyPrice", 0)
                
                art["_mapped_price"] = price
                art["_mapped_floor"] = floor_str
                
                # Append to processed group
                if t_type in ["A1", "매매"]: groups[ptp_key]["trade"].append(art)
                elif t_type in ["B1", "전세"]: groups[ptp_key]["rent"].append(art)
            
            def get_stats(items, is_trade=True):
                if not items: return 0, 0, 0, 0, 0, 0 # min_std, min_spc, min_total, max, avg, count
                items.sort(key=lambda x: int(x.get("_mapped_price", 0)))
                
                std = [x for x in items if not processor.is_low_floor(x.get("_mapped_floor", ""))]
                spc = [x for x in items if processor.is_low_floor(x.get("_mapped_floor", ""))]
                
                min_std = std[0]["_mapped_price"] if std else 0
                min_spc = spc[0]["_mapped_price"] if spc else 0
                min_total = items[0]["_mapped_price"] # Absolute min
                max_val = items[-1]["_mapped_price"]
                avg = sum(x["_mapped_price"] for x in items) / len(items)
                return min_std, min_spc, min_total, max_val, avg, len(items)

            for ptp_key, g in groups.items():
                    if not g["trade"] and not g["rent"]: continue
                    
                    tm_std, tm_spc, tm_min_total, tm_max, tm_avg, tm_cnt = get_stats(g["trade"])
                    # Rent (Jeonse): Use min_total for the single "Jeonse Min" column
                    _, _, rm_min_total, rm_max, rm_avg, rm_cnt = get_stats(g["rent"])
                    rm_min = rm_min_total
                    
                    # Gap & Ratio Logic
                    base_price = tm_std if tm_std > 0 else tm_spc
                    
                    gap = ""
                    jeonse_ratio = ""
                    
                    if base_price > 0 and rm_min > 0:
                        gap_val = base_price - rm_min
                        gap = gap_val # Will be formatted later
                        jeonse_ratio = (rm_min / base_price * 100)
                    
                    # Use 'complex_info' (Full Detail) for complex data
                    # Use 'g["info"]' (Article) for type/space data
                    info = g["info"]
                    space = info.get("spaceInfo", {}) or info
                    
                    # Correct Keys for Mobile API (fin.land)
                    # Coordinates
                    coords = complex_info.get("coordinates") or {}
                    lat = coords.get("yCoordinate") or ""
                    long = coords.get("xCoordinate") or ""

                    # Parking
                    pkg = complex_info.get("parkingInfo") or {}
                    pkg_cnt_hh = pkg.get("parkingCountPerHousehold") or ""
                    
                    # Heating
                    heat = complex_info.get("heatingAndCoolingInfo") or {}
                    heat_method = heat.get("heatingAndCoolingSystemType") or "" 
                    heat_fuel = heat.get("heatingEnergyType") or "" 
                    
                    # Additional Space Info (Hallway, Room/Bath)
                    # Match 'space' (from article) with 'areas' (from API)
                    hallway_type = ""
                    room_bath_str = ""
                    
                    target_space = float(space.get("supplySpace", 0))
                    # Old 'areas' block removed
                    

                    # MATCHING LOGIC (Using Pyeongs)
                    matched_pyeong = None
                    pyeongs = complex_info.get("pyeongs", [])
                    
                    for p in pyeongs:
                        p_name = p.get("name", "")
                        target_name = space.get("supplySpaceName", "")
                        
                        if p_name and target_name and p_name == target_name:
                            matched_pyeong = p
                            break
                    
                    # 2. Strict Area Match (Priority 2)
                    if not matched_pyeong:
                        for p in pyeongs:
                            p_supply = float(p.get("supplyArea", 0))
                            p_exclusive = float(p.get("exclusiveArea", 0))
                            target_exclusive = float(space.get("exclusiveSpace", 0))
                            if abs(p_supply - target_space) < 0.1 and abs(p_exclusive - target_exclusive) < 0.1:
                                matched_pyeong = p
                                break

                    # 3. Loose Area Match (Priority 3 - Fallback)
                    if not matched_pyeong:
                        for p in pyeongs:
                            p_supply = float(p.get("supplyArea", 0)) # Corrected Key
                            if abs(p_supply - target_space) < 0.1:
                                matched_pyeong = p
                                break
                    
                    if matched_pyeong:
                        # Hallway Type: 10=Stairs, 20=Corridor, 30=Mixed
                        e_type = str(matched_pyeong.get("entranceType", ""))
                        if e_type == "10": hallway_type = "계단식"
                        elif e_type == "20": hallway_type = "복도식"
                        elif e_type == "30": hallway_type = "복합식"
                        else: hallway_type = e_type
                        
                        r = matched_pyeong.get("roomCount", "")
                        b = matched_pyeong.get("bathRoomCount", "")
                        if r and b: room_bath_str = f"{r}/{b}개"

                    # FAR/BCR
                    b_ratio_info = complex_info.get("buildingRatioInfo") or {}
                    far = b_ratio_info.get("floorAreaRatio") or "" 
                    bcr = b_ratio_info.get("buildingCoverageRatio") or "" 
                    
                    # Constructor
                    const_co = complex_info.get("constructionCompany", "")

                    # Helper for Man-won formatting (Empty if 0)
                    def fmt(val):
                        if not val and val != 0: return "" # None/Empty
                        if val == 0: return "" # User requested empty for 0
                        if isinstance(val, str): return val
                        return f"{int(val / 10000):,}"

                    # Address Logic
                    # API 'address' might be a string or dict.
                    # If it's a string, we can't get region1DepthName.
                    # We utilize the 'region_name' passed from main if available.
                    addr = complex_info.get("address", {})
                    
                    sido = ""
                    gungu = ""
                    # Use the _dong_name we stored from the URL list!
                    # This is the 100% correct source from naver_region_codes.json
                    dong = complex_info.get("_dong_name", "") 
                    if not dong: dong = complex_info.get("bjdName", "")
                    
                    if isinstance(addr, dict):
                        sido = addr.get("region1DepthName", "")
                        gungu = addr.get("region2DepthName", "")
                        if not dong: dong = addr.get("region3DepthName", "")
                    elif isinstance(addr, str):
                        # Simple parsing if it's a string like "서울시 강남구 개포동 123"
                        parts = addr.split()
                        if len(parts) >= 1: sido = parts[0]
                        if len(parts) >= 2: gungu = parts[1]
                        if len(parts) >= 3 and not dong: dong = parts[2]

                    # Fallback to crawler's current region if empty
                    if not sido and hasattr(self, 'region_name'):
                        parts = self.region_name.split()
                        if len(parts) >= 1: sido = parts[0]
                        if len(parts) >= 2: gungu = parts[1]
                        if len(parts) >= 3 and not dong: dong = parts[2]

                    # Approval Date Check
                    approval_date = complex_info.get("useApprovalDate", "") # Correct Key Found
                    
                    results.append({
                        "시/도": sido, 
                        "시/군/구": gungu,
                        "읍/면/동": dong,
                        "아파트명": cname,
                        "준공일": approval_date, 
                        "총세대수": complex_info.get("totalHouseholdNumber", 0),
                        "타입": space.get('supplySpaceName', 'Unknown'),
                        "공급면적": float(space.get("supplySpace", 0)),
                        "전용면적": float(space.get("exclusiveSpace", 0)),
                        "현관구조": hallway_type,   # New
                        "방/욕실": room_bath_str,   # New
                        "매매 최저가 (일반)": fmt(tm_std) if tm_cnt > 0 else "",
                        "매매 최저가 (저층)": fmt(tm_spc) if tm_cnt > 0 else "",
                        "매매 최고가": fmt(tm_max) if tm_cnt > 0 else "",
                        "매매 평균가": fmt(int(tm_avg)) if tm_cnt > 0 else "",
                        "매매 매물수 (전체)": tm_cnt if tm_cnt > 0 else "",
                        "전세 최저가": fmt(rm_min) if rm_cnt > 0 else "",
                        "전세 최고가": fmt(rm_max) if rm_cnt > 0 else "",
                        "전세 평균가": fmt(int(rm_avg)) if rm_cnt > 0 else "",
                        "전세 매물수": rm_cnt if rm_cnt > 0 else "",
                        "갭": fmt(gap) if gap != "" else "",
                        "전세가율": f"{jeonse_ratio:.1f}%" if jeonse_ratio != "" else "",
                        "링크": f'=HYPERLINK("https://fin.land.naver.com/complexes/{cid}", "바로가기")',
                        # Moved to End as requested
                        "총동수": complex_info.get("dongCount", 0), 
                        "건설사": const_co,
                        "난방방식": heat_method,
                        "난방연료": heat_fuel,
                        "세대당주차대수": pkg_cnt_hh,
                        "용적률": far,
                        "건폐율": bcr,
                        "위도": lat,
                        "경도": long,
                        # For DB tracking
                        "수집일": datetime.now().strftime("%Y-%m-%d"),
                        "complex_id": cid # Keep raw ID
                    })

        return pd.DataFrame(results)

# Helper Functions
def get_all_leaf_items(node, current_name=""):
    items = []
    if "children" in node and node["children"]:
        for k, v in node["children"].items():
            # If current_name is empty, just use k. If not, maybe append? 
            # Actually we just want the LEAF name (Dong name).
            # The structure is usually City -> Gu -> Dong. 
            # Keep k as the name if it's a leaf.
            items.extend(get_all_leaf_items(v, k))
    else:
        if "url" in node:
            # It's a leaf. current_name should be the Dong name passed from parent loop.
            items.append((current_name, node["url"]))
    return items

def get_region_urls(region_list):
    json_path = "naver_region_codes.json"
    if not os.path.exists(json_path):
        print(f"❌ {json_path} not found.")
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f: data = json.load(f)
    except: return []

    final_items = [] # List of (name, url)
    for query in region_list:
        parts = query.split()
        curr = data
        found = True
        last_key = ""
        for part in parts:
            if part in curr:
                last_key = part
                curr = curr[part].get("children", {}) if "children" in curr[part] else curr[part]
            else:
                match = next((k for k in curr if part in k), None)
                if match:
                    last_key = match
                    if "children" in curr[match]: curr = curr[match]["children"]
                    # Else it's leaf?
                else:
                    found = False; break
        
        if found:
            # curr is now either a dict of children (if we stopped at Gu) or a Leaf Node (if we specified Dong)
            # If it has "url" and no "children", it's a leaf.
            if "url" in curr and "children" not in curr:
                 final_items.append((last_key, curr["url"]))
            else:
                 # It's a dict of children (e.g. Gu node's children dict)
                 # curr keys are Dong names
                 for k, v in curr.items():
                     final_items.extend(get_all_leaf_items(v, k))
                 
    return final_items # Returns list of (dong_name, url)

def get_subregions(region_name):
    """Get list of immediate child regions (e.g., '서울시' -> ['강남구', '강동구', ...])"""
    json_path = "naver_region_codes.json"
    try:
        with open(json_path, "r", encoding="utf-8") as f: data = json.load(f)
        if region_name in data:
            return list(data[region_name].get("children", {}).keys())
    except: pass
    return []

def save_to_db(df, table_name="real_estate"):
    """Save DataFrame to SQLite Database"""
    if df.empty: return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        # Using 'append' to add new data every run
        # Ideally we should handle duplicates, but for pure history tracking, append is safer.
        # Queries can filter by '수집일' later.
        df.to_sql(table_name, conn, if_exists="append", index=False)
        conn.close()
        print(f"💾 Database Updated: Added {len(df)} rows to '{table_name}' table.")
    except Exception as e:
        print(f"❌ Database Error: {e}")

async def main():
    
    # 1. Identify Processing Plan
    regions_to_process = []
    
    for target in TARGET_REGIONS:
        subregions = get_subregions(target)
        if subregions:
            print(f"🏙️ '{target}' Detected! Splitting into {len(subregions)} sub-regions...")
            for sub in subregions:
                regions_to_process.append(f"{target} {sub}")
        else:
            regions_to_process.append(target)
    
    # If no regions but URLs exist (Legacy mode)
    if not regions_to_process and TARGET_URLS:
        regions_to_process = ["UNKNOWN_REGION"]

    if not regions_to_process:
        print("❌ No items to process.")
        return

    print(f"📋 Processing Queue: {len(regions_to_process)} regions.")

    # 2. Process Each Region/Gu
    for region_name in regions_to_process:
        # Resolve URLs for this specific region
        current_urls = []
        if region_name == "UNKNOWN_REGION":
            current_urls = TARGET_URLS[:]
        else:
            print(f"\n Target: {region_name} ...")
            current_urls = get_region_urls([region_name])
        
        if not current_urls:
            print(f"⚠️ No URLs found for {region_name}")
            continue
            
        # New Crawler Instance per Gu
        crawler = NaverLandPlaywright()
        crawler.region_name = region_name # Pass region name for fallback
        print(f"🚀 Crawling {region_name} (URLs: {len(current_urls)})...")
        await crawler.run_test(current_urls, headless=HEADLESS_MODE)
        
        # Process & Save
        df = crawler.process_data()
        
        # Output to DB Logic
        if not df.empty:
            # Clean up columns for DB (Remove complex formulas like hyperlinks for raw data?)
            # Actually, keep them as text.
            # But maybe sanitize types.
            save_to_db(df)
        else:
            print(f"⚠️ No data for {region_name}")

if __name__ == "__main__":
    asyncio.run(main())
