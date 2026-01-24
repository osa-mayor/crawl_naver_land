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
import argparse
import sys
from land_selectors import NaverLandSelectors

# ==========================================
# [Configuration]
# ==========================================
# Target Regions (Search by Name using naver_region_codes.json)
TARGET_REGIONS = [
    "경기도"
]

# (Optional) RAW URLs override or addition
TARGET_URLS = []

# Filtering Options
MIN_HOUSEHOLDS = 0
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

class DataProcessor:
    @staticmethod
    def is_low_floor(floor_info: str) -> bool:
        if not floor_info:
            return False
        target_floors = ["1", "2", "3", "저"]
        floor_str = floor_info.split("/")[0].strip()
        if floor_str in target_floors:
            return True
        if floor_str.isdigit() and int(floor_str) <= 3:
            return True
        return False

    @staticmethod
    def format_price(num):
        if num == 0:
            return "-"
        eok = num // 100000000
        remainder = num % 100000000
        man = remainder // 10000
        if man > 0:
            return f"{eok}억 {man:,}"
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
            "is_mobile": False,
            "has_touch": False,
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

                    # Stealth scripts
                    page = await context.new_page()
                    await page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """)

                    try:
                        logging.info(f"🚀 Processing: {dong_name} ({url})")
                        await asyncio.sleep(random.uniform(0.5, 1.5))  # Random start delay
                        await self.process_region_tab(page, url, dong_name)
                    except Exception as e:
                        logging.error(f"❌ Failed processing {url}: {e}")
                    finally:
                        await context.close()

            tasks = [worker(item) for item in target_urls]
            if not tasks:
                logging.warning("No URLs to crawl.")
            else:
                await asyncio.gather(*tasks)

            # --- SAVE PHASE ---
            logging.info("💾 Processing and Saving Collected Data...")

            # 1) Save complexes metadata even if there are NO articles
            try:
                save_complexes_metadata_only(self.complexes)
            except Exception as e:
                logging.error(f"❌ Failed to save complexes metadata: {e}")

            # 2) Save prices only if we have articles
            try:
                df = self.process_data()
                if not df.empty:
                    save_to_db(df)
                    logging.info(f"✅ Saved {len(df)} rows to DB.")
                else:
                    logging.warning("⚠️ No data rows generated (no article/prices).")
            except Exception as e:
                logging.error(f"❌ Failed to save DB: {e}")

            await browser.close()

    async def process_region_tab(self, page, target_url, dong_name):
        """Logic for processing a single region tab"""
        try:
            # 1. Go to Region
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

            # Robust Wait: Wait for API response that loads the list
            try:
                async with page.expect_response(lambda response: "complex" in response.url and response.status == 200, timeout=10000):
                    pass
            except:
                pass  # Proceed

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
                    await page.wait_for_timeout(random.uniform(300, 700))
                else:
                    break
            except:
                break

        # 3. Extract Complexes
        try:
            await page.wait_for_selector(NaverLandSelectors.COMPLEX_ITEM, state="attached", timeout=5000)
        except:
            logging.warning("Timeout waiting for complex list (or empty).")

        complex_items = await page.query_selector_all(NaverLandSelectors.COMPLEX_ITEM)
        logging.info(f"🔍 Raw Complex Items Found: {len(complex_items)//6}")

        filtered_cids = []

        for item in complex_items:
            try:
                # Link & CID
                link_el = await item.query_selector(NaverLandSelectors.COMPLEX_LINK)
                if not link_el:
                    continue
                href = await link_el.get_attribute("href")
                match = re.search(r"/complexes/(\d+)", href)
                if not match:
                    continue
                cid = match.group(1)

                # Name
                name_el = await item.query_selector(NaverLandSelectors.COMPLEX_NAME)
                name = await name_el.inner_text() if name_el else f"Complex_{cid}"

                # Filters (Badge)
                is_apt = False
                badge_el = await item.query_selector(NaverLandSelectors.COMPLEX_BADGE)
                if badge_el:
                    badge_text = await badge_el.inner_text()
                    if ("아파트" in badge_text or "분양권" in badge_text) and "오피스텔" not in badge_text:
                        is_apt = True

                if not is_apt:
                    continue

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

                if households < MIN_HOUSEHOLDS:
                    logging.info(f"Skipping {name}: Households {households} < {MIN_HOUSEHOLDS}")
                    continue

                # ✅ IMPORTANT: store dong_name (now full path 가능)
                self.complexes[cid] = {
                    "name": name,
                    "households": households,
                    "_dong_name": dong_name
                }
                filtered_cids.append(cid)
            except Exception:
                continue

        logging.info(f"Target Count in Region: {len(filtered_cids)}")
        if not filtered_cids:
            await page.screenshot(path=f"debug_crawler_fail_{dong_name}.png")
            logging.error(f"Saved debug screenshot to debug_crawler_fail_{dong_name}.png")
            return

        # ========================================================
        # [OPTIMIZATION] Parallel Fetch of Details (Complex & Pyeong)
        # ========================================================
        async def fetch_one_complex(cid):
            # 1. Complex Detail
            if cid not in self.complexes or "totalHouseholdNumber" not in self.complexes[cid]:
                try:
                    api_url = f"https://fin.land.naver.com/front-api/v1/complex?complexNumber={cid}"
                    api_res = await page.request.get(api_url)
                    if api_res.status == 200:
                        data = await api_res.json()
                        if "result" in data:
                            new_data = data["result"]
                            # Preserve custom fields from list
                            if cid in self.complexes and isinstance(self.complexes[cid], dict):
                                new_data["_dong_name"] = self.complexes[cid].get("_dong_name")
                            self.complexes[cid] = new_data
                except:
                    pass

            # 2. Pyeong List
            if cid in self.complexes and "pyeongs" not in self.complexes[cid]:
                try:
                    pyeong_url = f"https://fin.land.naver.com/front-api/v1/complex/pyeongList?complexNumber={cid}"
                    p_res = await page.request.get(pyeong_url)
                    if p_res.status == 200:
                        p_data = await p_res.json()
                        if "result" in p_data:
                            self.complexes[cid]["pyeongs"] = p_data["result"]
                except:
                    pass

        logging.info(f"⚡ Pre-fetching details for {len(filtered_cids)} complexes concurrently...")

        sem_api = asyncio.Semaphore(20)

        async def sem_task(cid):
            async with sem_api:
                await fetch_one_complex(cid)

        await asyncio.gather(*[sem_task(c) for c in filtered_cids])
        logging.info("✅ Pre-fetch complete.")
        # ========================================================

        # 4. API Interception Setup
        async def handle_response(response):
            try:
                url = response.url
                if "front-api/v1" in url and response.status == 200:
                    # Filter out the detail APIs we just called manually to avoid noise
                    if "pyeongList" in url or "/complex?" in url:
                        return

                    data = await response.json()

                    items = []
                    if "result" in data:
                        res = data["result"]
                        if isinstance(res, list):
                            items = res
                        elif isinstance(res, dict) and "list" in res:
                            items = res["list"]

                    if items:
                        found_cid = None
                        match = re.search(r"complexNumber=(\d+)", url)
                        if match:
                            found_cid = match.group(1)

                        if not found_cid:
                            try:
                                post = response.request.post_data_json
                                if post and "complexNumber" in post:
                                    found_cid = str(post["complexNumber"])
                            except:
                                pass

                        if found_cid:
                            if found_cid not in self.captured_articles:
                                self.captured_articles[found_cid] = []
                            self.captured_articles[found_cid].extend(items)
            except:
                pass

        page.on("response", handle_response)

        # 5. Visit Details (Trigger article list)
        for cid in filtered_cids:
            for t_type in ["A1", "B1"]:  # Trade, Jeonse
                detail_url = f"https://fin.land.naver.com/complexes/{cid}?tab=article&tradeType={t_type}&articleTradeTypes={t_type}&articleSortingType=PRICE_ASC"

                try:
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(random.uniform(500, 1000))

                    last_height = await page.evaluate("document.body.scrollHeight")
                    no_change = 0
                    for _ in range(30):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(random.uniform(300, 600))
                        new_height = await page.evaluate("document.body.scrollHeight")
                        if new_height == last_height:
                            no_change += 1
                            if no_change >= 2:
                                break
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
                if "articleInfoList" in item:
                    flat_articles.extend(item["articleInfoList"])
                elif "representativeArticleInfo" in item:
                    flat_articles.append(item["representativeArticleInfo"])
                else:
                    flat_articles.append(item)

            if not flat_articles:
                continue

            # Group by Pyeong
            groups = {}
            for art in flat_articles:
                space = art.get("spaceInfo", {})
                if not space and "supplySpaceName" in art:
                    space = art
                s_name = space.get("supplySpaceName", str(space.get("supplySpace", "")))
                e_name = space.get("exclusiveSpaceName", str(space.get("exclusiveSpace", "")))
                ptp_key = f"{s_name}_{e_name}"

                if ptp_key not in groups:
                    groups[ptp_key] = {"trade": [], "rent": [], "info": art}

                t_type = art.get("tradeType", "")

                floor_str = "-"
                floor_info = art.get("floorDetailInfo")
                if not floor_info:
                    detail = art.get("articleDetail", {})
                    floor_info = detail.get("floorDetailInfo")
                    if not floor_info:
                        floor_str = detail.get("floorInfo", "-")

                if floor_info:
                    floor_str = f"{floor_info.get('targetFloor','')}/{floor_info.get('totalFloor','')}"

                art["_mapped_price"] = 0

                price_info = art.get("priceInfo", {})
                price = 0
                if t_type in ["A1", "매매"]:
                    price = price_info.get("dealPrice", 0) if price_info else art.get("dealPrice", 0)
                elif t_type in ["B1", "전세"]:
                    price = price_info.get("warrantyPrice", 0) if price_info else art.get("warrantyPrice", 0)

                art["_mapped_price"] = price
                art["_mapped_floor"] = floor_str

                if t_type in ["A1", "매매"]:
                    groups[ptp_key]["trade"].append(art)
                elif t_type in ["B1", "전세"]:
                    groups[ptp_key]["rent"].append(art)

            def get_stats(items, is_trade=True):
                if not items:
                    return 0, 0, 0, 0, 0, 0
                items.sort(key=lambda x: int(x.get("_mapped_price", 0)))

                std = [x for x in items if not processor.is_low_floor(x.get("_mapped_floor", ""))]
                spc = [x for x in items if processor.is_low_floor(x.get("_mapped_floor", ""))]

                min_std = std[0]["_mapped_price"] if std else 0
                min_spc = spc[0]["_mapped_price"] if spc else 0
                min_total = items[0]["_mapped_price"]
                max_val = items[-1]["_mapped_price"]
                avg = sum(x["_mapped_price"] for x in items) / len(items)
                return min_std, min_spc, min_total, max_val, avg, len(items)

            for ptp_key, g in groups.items():
                if not g["trade"] and not g["rent"]:
                    continue

                tm_std, tm_spc, tm_min_total, tm_max, tm_avg, tm_cnt = get_stats(g["trade"])
                _, _, rm_min_total, rm_max, rm_avg, rm_cnt = get_stats(g["rent"])
                rm_min = rm_min_total

                base_price = tm_std if tm_std > 0 else tm_spc

                gap = ""
                jeonse_ratio = ""
                if base_price > 0 and rm_min > 0:
                    gap_val = base_price - rm_min
                    gap = gap_val
                    jeonse_ratio = (rm_min / base_price * 100)

                info = g["info"]
                space = info.get("spaceInfo", {}) or info

                coords = complex_info.get("coordinates") or {}
                lat = coords.get("yCoordinate") or ""
                long = coords.get("xCoordinate") or ""

                pkg = complex_info.get("parkingInfo") or {}
                pkg_cnt_hh = pkg.get("parkingCountPerHousehold") or ""

                heat = complex_info.get("heatingAndCoolingInfo") or {}
                heat_method = heat.get("heatingAndCoolingSystemType") or ""
                heat_fuel = heat.get("heatingEnergyType") or ""

                hallway_type = ""
                room_bath_str = ""

                target_space = float(space.get("supplySpace", 0))

                matched_pyeong = None
                pyeongs = complex_info.get("pyeongs", [])

                for p in pyeongs:
                    p_name = p.get("name", "")
                    target_name = space.get("supplySpaceName", "")
                    if p_name and target_name and p_name == target_name:
                        matched_pyeong = p
                        break

                if not matched_pyeong:
                    for p in pyeongs:
                        p_supply = float(p.get("supplyArea", 0))
                        p_exclusive = float(p.get("exclusiveArea", 0))
                        target_exclusive = float(space.get("exclusiveSpace", 0))
                        if abs(p_supply - target_space) < 0.1 and abs(p_exclusive - target_exclusive) < 0.1:
                            matched_pyeong = p
                            break

                if not matched_pyeong:
                    for p in pyeongs:
                        p_supply = float(p.get("supplyArea", 0))
                        if abs(p_supply - target_space) < 0.1:
                            matched_pyeong = p
                            break

                if matched_pyeong:
                    e_type = str(matched_pyeong.get("entranceType", ""))
                    if e_type == "10":
                        hallway_type = "계단식"
                    elif e_type == "20":
                        hallway_type = "복도식"
                    elif e_type == "30":
                        hallway_type = "복합식"
                    else:
                        hallway_type = e_type

                    r = matched_pyeong.get("roomCount", "")
                    b = matched_pyeong.get("bathRoomCount", "")
                    if r and b:
                        room_bath_str = f"{r}/{b}개"

                b_ratio_info = complex_info.get("buildingRatioInfo") or {}
                far = b_ratio_info.get("floorAreaRatio") or ""
                bcr = b_ratio_info.get("buildingCoverageRatio") or ""

                const_co = complex_info.get("constructionCompany", "")

                def fmt(val):
                    if not val and val != 0:
                        return ""
                    if val == 0:
                        return ""
                    if isinstance(val, str):
                        return val
                    return f"{int(val / 10000):,}"

                addr = complex_info.get("address", {})

                sido = ""
                gungu = ""

                # ✅ dong_name이 "서울시 강남구 신사동"으로 들어오더라도,
                # 엑셀/df에서는 읍/면/동은 마지막 토큰(신사동)만 유지
                dong = complex_info.get("_dong_name", "")
                if dong and isinstance(dong, str) and " " in dong:
                    dong = dong.split()[-1]
                if not dong:
                    dong = complex_info.get("bjdName", "")

                if isinstance(addr, dict):
                    sido = addr.get("region1DepthName", "")
                    gungu = addr.get("region2DepthName", "")
                    if not dong:
                        dong = addr.get("region3DepthName", "")
                elif isinstance(addr, str):
                    parts = addr.split()
                    if len(parts) >= 1:
                        sido = parts[0]
                    if len(parts) >= 2:
                        gungu = parts[1]
                    if len(parts) >= 3 and not dong:
                        dong = parts[2]

                if not sido and hasattr(self, "region_name"):
                    parts = self.region_name.split()
                    if len(parts) >= 1:
                        sido = parts[0]
                    if len(parts) >= 2:
                        gungu = parts[1]
                    if len(parts) >= 3 and not dong:
                        dong = parts[2]

                approval_date = complex_info.get("useApprovalDate", "")

                results.append({
                    "시/도": sido,
                    "시/군/구": gungu,
                    "읍/면/동": dong,
                    "아파트명": cname,
                    "준공일": approval_date,
                    "총세대수": complex_info.get("totalHouseholdNumber", 0),
                    "타입": space.get("supplySpaceName", "Unknown"),
                    "공급면적": float(space.get("supplySpace", 0)),
                    "전용면적": float(space.get("exclusiveSpace", 0)),
                    "현관구조": hallway_type,
                    "방/욕실": room_bath_str,
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
                    "총동수": complex_info.get("dongCount", 0),
                    "건설사": const_co,
                    "난방방식": heat_method,
                    "난방연료": heat_fuel,
                    "세대당주차대수": pkg_cnt_hh,
                    "용적률": far,
                    "건폐율": bcr,
                    "위도": lat,
                    "경도": long,
                    "수집일": datetime.now().strftime("%Y-%m-%d"),
                    "complex_id": cid
                })

        return pd.DataFrame(results)


# Helper Functions
def get_all_leaf_items(node, current_name=""):
    # Optimization: Skip if validated as empty (and flag exists)
    if node.get("has_complexes") is False:
        return []

    items = []
    if "children" in node and node["children"]:
        for k, v in node["children"].items():
            # ✅ FIX: keep full path
            next_name = f"{current_name} {k}".strip()
            items.extend(get_all_leaf_items(v, next_name))
    elif "url" in node:
        # Leaf node
        items.append((current_name, node["url"]))
    return items


def get_region_urls(region_list):
    json_path = "naver_region_codes.json"
    if not os.path.exists(json_path):
        print(f"❌ {json_path} not found.")
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        return []

    final_items = []  # List of (name, url)

    def find_node_recursive(node, target_key):
        if target_key in node:
            return node[target_key], target_key

        for k in node:
            if target_key in k:
                return node[k], k

        for k, v in node.items():
            if "children" in v:
                found, found_key = find_node_recursive(v["children"], target_key)
                if found:
                    return found, found_key
        return None, None

    for query in region_list:
        parts = query.split()
        if not parts:
            continue

        root_part = parts[0]
        start_node = None
        start_node_key = ""

        for k in data:
            if root_part in k:
                start_node = data[k]
                start_node_key = k
                break

        if not start_node:
            start_node, start_node_key = find_node_recursive(data, root_part)

        if not start_node:
            print(f"⚠️ Region not found: {root_part}")
            continue

        curr = start_node
        valid_path = True

        idx = 1
        while idx < len(parts):
            part = parts[idx]

            if start_node_key and part in start_node_key:
                idx += 1
                continue

            if "children" in curr:
                children = curr["children"]
                matched_child = None

                if idx + 1 < len(parts):
                    next_part = parts[idx + 1]
                    combined = f"{part} {next_part}"
                    if combined in children:
                        matched_child = children[combined]
                        idx += 2
                        curr = matched_child
                        continue

                if part in children:
                    matched_child = children[part]
                    idx += 1
                    curr = matched_child
                    continue

                for ck in children:
                    if part in ck:
                        matched_child = children[ck]
                        break

                if matched_child:
                    curr = matched_child
                    idx += 1
                else:
                    print(f"❌ Sub-region '{part}' not found in current context")
                    valid_path = False
                    break
            else:
                print(f"❌ Current node has no sub-regions, cannot find '{part}'")
                valid_path = False
                break

        if valid_path:
            # ✅ FIX: pass full query string, not only last token
            if "url" in curr and "children" not in curr:
                final_items.append((query, curr["url"]))
            elif "children" in curr:
                final_items.extend(get_all_leaf_items(curr, query))
            elif "url" in curr:
                final_items.append((query, curr["url"]))

    return final_items


def get_subregions(region_name):
    """Get list of immediate child regions (e.g., '서울시' -> ['강남구', ...])"""
    json_path = "naver_region_codes.json"
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if region_name in data:
            return list(data[region_name].get("children", {}).keys())
    except:
        pass
    return []


def init_db():
    """Initialize Database with Normalized Schema"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS complexes (
        complex_no INTEGER PRIMARY KEY,
        name TEXT,
        region_depth1 TEXT,
        region_depth2 TEXT,
        region_depth3 TEXT,
        total_households INTEGER,
        total_dongs INTEGER,
        completion_date TEXT,
        construction_company TEXT,
        heating_method TEXT,
        heating_fuel TEXT,
        parking_per_household REAL,
        far REAL,
        bcr REAL,
        latitude REAL,
        longitude REAL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complex_no INTEGER,
        date TEXT,
        pyeong_type TEXT,
        supply_area REAL,
        exclusive_area REAL,
        hallway_type TEXT,
        room_bath TEXT,
        trade_min_std INTEGER,
        trade_min_low INTEGER,
        trade_max INTEGER,
        trade_avg INTEGER,
        trade_count INTEGER,
        rent_min INTEGER,
        rent_max INTEGER,
        rent_avg INTEGER,
        rent_count INTEGER,
        gap INTEGER,
        jeonse_ratio REAL,
        FOREIGN KEY (complex_no) REFERENCES complexes (complex_no)
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_prices_complex_date ON prices (complex_no, date)")

    conn.commit()
    conn.close()


def save_to_db(df, table_name="real_estate"):
    """Save DataFrame to Normalized SQLite Database"""
    if df.empty:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    count_complex = 0
    count_prices = 0

    try:
        def parse_price(val):
            if not val:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            clean = str(val).replace(",", "")
            return int(clean) if clean.isdigit() else 0

        complex_groups = df.groupby("complex_id")

        for cid, group in complex_groups:
            first = group.iloc[0]

            cur.execute("""
            INSERT OR REPLACE INTO complexes (
                complex_no, name, region_depth1, region_depth2, region_depth3,
                total_households, total_dongs, completion_date, construction_company,
                heating_method, heating_fuel, parking_per_household,
                far, bcr, latitude, longitude, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(cid),
                first["아파트명"],
                first["시/도"],
                first["시/군/구"],
                first["읍/면/동"],
                int(first.get("총세대수", 0) or 0),
                int(first.get("총동수", 0) or 0),
                first.get("준공일", ""),
                first.get("건설사", ""),
                first.get("난방방식", ""),
                first.get("난방연료", ""),
                float(str(first.get("세대당주차대수", 0)).replace("대", "") or 0),
                float(str(first.get("용적률", "0")).replace("%", "") or 0),
                float(str(first.get("건폐율", "0")).replace("%", "") or 0),
                float(first.get("위도", 0) or 0),
                float(first.get("경도", 0) or 0),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            count_complex += 1

            for _, row in group.iterrows():
                cur.execute("""
                INSERT INTO prices (
                    complex_no, date, pyeong_type, supply_area, exclusive_area,
                    hallway_type, room_bath,
                    trade_min_std, trade_min_low, trade_max, trade_avg, trade_count,
                    rent_min, rent_max, rent_avg, rent_count,
                    gap, jeonse_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(cid),
                    row["수집일"],
                    row["타입"],
                    float(row["공급면적"] or 0),
                    float(row["전용면적"] or 0),
                    row.get("현관구조", ""),
                    row.get("방/욕실", ""),
                    parse_price(row["매매 최저가 (일반)"]),
                    parse_price(row["매매 최저가 (저층)"]),
                    parse_price(row["매매 최고가"]),
                    parse_price(row["매매 평균가"]),
                    int(row["매매 매물수 (전체)"] or 0),
                    parse_price(row["전세 최저가"]),
                    parse_price(row["전세 최고가"]),
                    parse_price(row["전세 평균가"]),
                    int(row["전세 매물수"] or 0),
                    parse_price(row.get("갭", 0)),
                    float(str(row.get("전세가율", "0")).replace("%", "") or 0)
                ))
                count_prices += 1

        conn.commit()
        print(f"💾 Database Updated: {count_complex} complexes updated, {count_prices} price records added.")

    except Exception as e:
        print(f"❌ Database Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


def save_complexes_metadata_only(complexes_dict):
    """
    Save complexes metadata into 'complexes' table even if there are no articles/prices.
    ✅ FIX: if address(dict) is missing, fallback parse from _dong_name full path like "서울시 강남구 신사동"
    """
    if not complexes_dict:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    try:
        for cid, info in complexes_dict.items():
            if not isinstance(info, dict):
                continue

            name = info.get("name", "") or info.get("complexName", "") or f"Complex_{cid}"

            addr = info.get("address", {})
            sido = ""
            gungu = ""

            dong_path = info.get("_dong_name", "") or info.get("bjdName", "") or ""
            dong = dong_path  # will normalize later

            if isinstance(addr, dict) and addr:
                sido = addr.get("region1DepthName", "") or ""
                gungu = addr.get("region2DepthName", "") or ""
                if not dong:
                    dong = addr.get("region3DepthName", "") or ""
            elif isinstance(addr, str) and addr.strip():
                parts = addr.split()
                if len(parts) >= 1:
                    sido = parts[0]
                if len(parts) >= 2:
                    gungu = parts[1]
                if len(parts) >= 3 and not dong:
                    dong = parts[2]

            # ✅ Fallback: parse from full path stored in _dong_name
            # Example: "서울시 강남구 신사동"
            if (not sido or not gungu) and dong_path and isinstance(dong_path, str) and " " in dong_path:
                tokens = dong_path.split()
                if len(tokens) >= 3:
                    sido = sido or tokens[0]
                    gungu = gungu or tokens[1]
                    dong = tokens[-1]

            # Normalize dong to last token if it still has spaces
            if dong and isinstance(dong, str) and " " in dong:
                dong = dong.split()[-1]

            coords = info.get("coordinates") or {}
            lat = coords.get("yCoordinate") or 0
            lon = coords.get("xCoordinate") or 0

            pkg = info.get("parkingInfo") or {}
            parking_per_hh = pkg.get("parkingCountPerHousehold") or 0

            heat = info.get("heatingAndCoolingInfo") or {}
            heating_method = heat.get("heatingAndCoolingSystemType") or ""
            heating_fuel = heat.get("heatingEnergyType") or ""

            b_ratio = info.get("buildingRatioInfo") or {}
            far = b_ratio.get("floorAreaRatio") or 0
            bcr = b_ratio.get("buildingCoverageRatio") or 0

            total_households = info.get("totalHouseholdNumber", 0) or info.get("households", 0)
            total_dongs = info.get("dongCount", 0) or 0
            completion_date = info.get("useApprovalDate", "") or ""

            construction_company = info.get("constructionCompany", "") or ""

            cur.execute("""
            INSERT OR REPLACE INTO complexes (
                complex_no, name, region_depth1, region_depth2, region_depth3,
                total_households, total_dongs, completion_date, construction_company,
                heating_method, heating_fuel, parking_per_household,
                far, bcr, latitude, longitude, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(cid),
                name,
                sido,
                gungu,
                dong,
                int(total_households or 0),
                int(total_dongs or 0),
                completion_date,
                construction_company,
                heating_method,
                heating_fuel,
                float(parking_per_hh or 0),
                float(far or 0),
                float(bcr or 0),
                float(lat or 0),
                float(lon or 0),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))

        conn.commit()
        print(f"💾 complexes metadata only saved/updated: {len(complexes_dict)} candidates processed.")

    except Exception as e:
        print(f"❌ complexes metadata save error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


# 분산 처리(Sharding)를 위한 헬퍼 함수
def get_sharded_targets(shard_index, shard_total):
    json_path = "naver_region_codes.json"
    if not os.path.exists(json_path):
        print(f"❌ {json_path} not found.")
        return []

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_targets = []

    def collect(node, path_name):
        is_valid = node.get("has_complexes", False)

        if "url" in node and is_valid:
            all_targets.append({
                "name": path_name,
                "url": node["url"]
            })

        if "children" in node:
            for k, v in node["children"].items():
                collect(v, f"{path_name} {k}".strip())

    for k, v in data.items():
        collect(v, k)

    all_targets.sort(key=lambda x: x["name"])

    total_count = len(all_targets)
    if total_count == 0:
        return []

    sharded = all_targets[shard_index::shard_total]
    print(f"🧩 샤드 {shard_index}/{shard_total} (스트라이프 방식): 총 {len(sharded)}개 지역 처리 예정")

    return sharded


async def main():
    parser = argparse.ArgumentParser(description="Naver Land Crawler")
    parser.add_argument("--regions", nargs="+", help="List of regions to crawl (overrides config)")
    parser.add_argument("--db-path", default="real_estate.db", help="Path to output SQLite DB")
    parser.add_argument("--shard-index", type=int, default=None, help="Shard Index (0-based)")
    parser.add_argument("--shard-total", type=int, default=None, help="Total Shards")
    args = parser.parse_args()

    global DB_PATH
    if args.db_path:
        DB_PATH = args.db_path
        print(f"🔧 Config Override: DB Path set to {DB_PATH}")

    init_db()

    regions_to_process = []
    direct_targets = []

    # Case A: Sharding Mode
    if args.shard_index is not None and args.shard_total is not None:
        print(f"⚡ Sharding Enabled: Index {args.shard_index} / Total {args.shard_total}")
        direct_targets = get_sharded_targets(args.shard_index, args.shard_total)

    # Case B: Manual Region List
    elif args.regions:
        target_regions_config = args.regions
        print(f"🔧 Config Override: Target Regions set to {target_regions_config}")

        for target in target_regions_config:
            subregions = get_subregions(target)
            if subregions:
                for sub in subregions:
                    regions_to_process.append(f"{target} {sub}")
            else:
                regions_to_process.append(target)

    # Case C: Default Config
    else:
        target_regions_config = TARGET_REGIONS
        for target in target_regions_config:
            subregions = get_subregions(target)
            if subregions:
                for sub in subregions:
                    regions_to_process.append(f"{target} {sub}")
            else:
                regions_to_process.append(target)

    # Execution
    if direct_targets:
        print(f"🚀 Starting Sharded Crawl with {len(direct_targets)} locations...")
        crawler = NaverLandPlaywright()
        urls = [(t["name"], t["url"]) for t in direct_targets]
        await crawler.run_test(urls, headless=HEADLESS_MODE)

    else:
        if not regions_to_process and TARGET_URLS:
            regions_to_process = ["UNKNOWN_REGION"]

        if not regions_to_process:
            print("❌ No items to process.")
            return

        print(f"📋 Processing Queue: {len(regions_to_process)} regions.")
        for region_name in regions_to_process:
            if region_name == "UNKNOWN_REGION":
                current_urls = TARGET_URLS[:]
            else:
                print(f"\n Target: {region_name} ...")
                current_urls = get_region_urls([region_name])

            if not current_urls:
                print(f"⚠️ No URLs found for {region_name}")
                continue

            crawler = NaverLandPlaywright()
            crawler.region_name = region_name
            print(f"🚀 Crawling {region_name} (URLs: {len(current_urls)})...")
            await crawler.run_test(current_urls, headless=HEADLESS_MODE)


if __name__ == "__main__":
    asyncio.run(main())
