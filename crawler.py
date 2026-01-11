import asyncio
import re
import json
import pandas as pd
from playwright.async_api import async_playwright
import logging
from datetime import datetime

# ==========================================
# [Configuration]
# ==========================================
# Target Regions (URL List)
# Add more URLs here to crawl multiple regions sequentially.
TARGET_URLS = [
    "https://fin.land.naver.com/regions?si=1100000000&gun=1174000000&eup=1174010600",  # Seoul Gangdong-gu Dunchon-dong
]

# Filtering Options
MIN_HOUSEHOLDS = 100  # Minimum number of households
EXCLUDE_LOW_FLOORS = False  # Set to False to collect ALL items, then classify in logic
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("crawler.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


class DataProcessor:
    @staticmethod
    def is_low_or_top_floor(floor_info: str) -> bool:
        if not floor_info:
            return False

        target_floors = ["1", "2", "3", "저"]
        floor_str = floor_info.split("/")[0].strip()

        if floor_str in target_floors:
            return True
        if floor_str.isdigit() and int(floor_str) <= 3:
            return True
        if "탑" in floor_info:
            return True

        parts = floor_info.split("/")
        if len(parts) == 2:
            curr, total = parts[0].strip(), parts[1].strip()
            if curr.isdigit() and total.isdigit():
                if int(curr) == int(total):
                    return True
        return False

    @staticmethod
    def format_price(num):
        if num == 0:
            return "-"
        # Input is in Won (e.g., 1,600,000,000)
        eok = num // 100000000
        remainder = num % 100000000
        man = remainder // 10000

        if man > 0:
            return f"{eok}억 {man:,}"
        return f"{eok}억"


class NaverLandPlaywright:
    def __init__(self):
        self.results = []
        self.complexes = {}
        self.captured_articles = {}
        self.handle_response_wrapper = None

    async def run_test(self, headless: bool = True):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                locale="ko-KR",
                timezone_id="Asia/Seoul",
            )
            page = await context.new_page()

            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            # ---------------------------
            # 1. Warm-up
            # ---------------------------
            try:
                await page.goto(
                    "https://m.land.naver.com/", wait_until="networkidle", timeout=30000
                )
            except:
                logging.warning("Warm-up navigation timed out, proceeding...")

            # ---------------------------
            # 2. Iterate Regions
            # ---------------------------
            for target_url in TARGET_URLS:
                logging.info(f"Visiting Region URL: {target_url}")
                try:
                    await page.goto(
                        target_url, wait_until="domcontentloaded", timeout=45000
                    )
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    logging.error(f"Failed to load region page: {e}")
                    continue

                # ---------------------------
                # 3. Load All Complexes (Pagination)
                # ---------------------------
                logging.info("Loading full list by clicking 'More' button...")
                while True:
                    try:
                        # Find "더보기" button
                        more_btn = page.locator("button", has_text="더보기")

                        # Check visibility with a short timeout
                        if await more_btn.is_visible(timeout=2000):
                            logging.info("Clicking 'More' button...")
                            await more_btn.click()
                            # Wait for list to update - simple pause or wait for network
                            await page.wait_for_timeout(500)
                        else:
                            logging.info(
                                "'More' button not found or not visible. Assuming full list loaded."
                            )
                            break
                    except Exception as e:
                        logging.warning(f"Error checking/clicking 'More' button: {e}")
                        break

                # ---------------------------
                # Extract & Filter in List
                # ---------------------------
                logging.info("Extracting and filtering complexes...")

                try:
                    await page.wait_for_selector(
                        "li[class*='ComplexItem_article']", timeout=15000
                    )
                except:
                    logging.warning("Timeout waiting for complex list.")

                complex_items = await page.query_selector_all(
                    "li[class*='ComplexItem_article']"
                )
                filtered_cids = []

                for item in complex_items:
                    try:
                        # Link & CID
                        link_el = await item.query_selector(
                            "a[class*='ComplexItem_link']"
                        )
                        if not link_el:
                            continue
                        href = await link_el.get_attribute("href")
                        match = re.search(r"/complexes/(\d+)", href)
                        if not match:
                            continue
                        cid = match.group(1)

                        # Name
                        name_el = await item.query_selector(
                            "strong[class*='ComplexItem_name']"
                        )
                        name = (
                            await name_el.inner_text() if name_el else f"Complex_{cid}"
                        )

                        # Type Filter (Strict Apartment)
                        is_apt = False
                        badge_el = await item.query_selector(
                            "span[class*='TitleBadge_article']"
                        )
                        if badge_el:
                            badge_text = await badge_el.inner_text()
                            if "아파트" in badge_text and "오피스텔" not in badge_text:
                                is_apt = True

                        if not is_apt:
                            continue

                        # Household Filter
                        info_items = await item.query_selector_all(
                            "li[class*='ComplexItem_item-info']"
                        )
                        households = 0
                        for info in info_items:
                            text = await info.inner_text()
                            if "세대" in text:
                                h_match = re.search(r"(\d[\d,]*)\s*세대", text)
                                if h_match:
                                    households = int(h_match.group(1).replace(",", ""))
                                    break

                        if households < MIN_HOUSEHOLDS:
                            continue

                        self.complexes[cid] = {"name": name, "households": households}
                        filtered_cids.append(cid)
                        logging.info(f"Found Target: {name} ({cid}) - {households}세대")

                    except Exception as e:
                        continue

                logging.info(
                    f"Total targets found in this region: {len(filtered_cids)}"
                )
                if not filtered_cids:
                    continue

                # ---------------------------
                # Setup API Interception
                # ---------------------------
                if self.handle_response_wrapper:
                    try:
                        page.remove_listener("response", self.handle_response_wrapper)
                    except:
                        pass

                async def handle_response(response):
                    try:
                        url = response.url
                        if "front-api/v1" in url and response.status == 200:
                            is_target = False
                            if "article/list" in url:
                                is_target = True
                            if "realtor/advertisement" in url:
                                is_target = True

                            if is_target:
                                data = await response.json()
                                if "result" in data:
                                    res = data["result"]
                                    items = []

                                    if isinstance(res, list):
                                        items = res
                                    elif isinstance(res, dict) and "list" in res:
                                        items = res["list"]

                                    if items:
                                        cid = None

                                        # 1. Try URL (GET)
                                        match = re.search(r"complexNumber=(\d+)", url)
                                        if match:
                                            cid = match.group(1)

                                        # 2. Try POST Data
                                        if not cid:
                                            try:
                                                req = response.request
                                                post_data = req.post_data_json
                                                if (
                                                    post_data
                                                    and "complexNumber" in post_data
                                                ):
                                                    cid = str(
                                                        post_data["complexNumber"]
                                                    )

                                                if not cid and req.post_data:
                                                    pmatch = re.search(
                                                        r"complexNumber=(\d+)",
                                                        req.post_data,
                                                    )
                                                    if pmatch:
                                                        cid = pmatch.group(1)
                                            except:
                                                pass

                                        if cid:
                                            if cid not in self.captured_articles: self.captured_articles[cid] = []
                                            # Avoid duplicates if possible? checking ID might be expensive. 
                                            # Just extend for now.
                                            self.captured_articles[cid].extend(items)
                                            logging.info(
                                                f"Captured {len(items)} items for {self.complexes.get(cid, cid)}..."
                                            )
                    except:
                        pass

                self.handle_response_wrapper = handle_response  # Store ref
                page.on("response", self.handle_response_wrapper)

                # ---------------------------
                # Visit Filtered URLs (Sale & Jeonse)
                # ---------------------------
                for cid in filtered_cids:
                    for t_type in ["A1", "B1"]:
                        type_name = "매매" if t_type == "A1" else "전세"
                        detail_url = f"https://fin.land.naver.com/complexes/{cid}?tab=article&tradeType={t_type}&articleTradeTypes={t_type}&articleSortingType=PRICE_ASC"
                        logging.info(f"Visiting {self.complexes[cid]} ({type_name})...")

                        try:
                            await page.goto(
                                detail_url, wait_until="domcontentloaded", timeout=45000
                            )
                        except Exception as e:
                            logging.warning(f"Nav error {cid} ({t_type}): {e}")

                        # Wait for initial load
                        await page.wait_for_timeout(2000)

                        # ---------------------------
                        # Scroll to Load All Articles
                        # ---------------------------
                        logging.info(
                            f"Scrolling to load all articles for {type_name}..."
                        )
                        last_height = await page.evaluate("document.body.scrollHeight")
                        no_change_count = 0
                        scroll_count = 0

                        while True:
                            # Safety break
                            if scroll_count > 50:
                                logging.warning("Max scroll attempts reached. Breaking.")
                                break
                            
                            scroll_count += 1
                            
                            try:
                                # Scroll to bottom
                                await page.evaluate(
                                    "window.scrollTo(0, document.body.scrollHeight)"
                                )
    
                                # Wait for potential load
                                await page.wait_for_timeout(1500)
    
                                new_height = await page.evaluate(
                                    "document.body.scrollHeight"
                                )
                            except:
                                break

                            if new_height == last_height:
                                no_change_count += 1
                                if (
                                    no_change_count >= 2
                                ):  # Stop if no change for 2 iterations
                                    break
                            else:
                                no_change_count = 0  # Reset if height changed
                                # logging.info("Scroll triggered new content...")
    
                            last_height = new_height

            await browser.close()

    def process_data(self):
        processor = DataProcessor()
        results = []

        logging.info(f"Processing data for {len(self.captured_articles)} complexes...")

        for cid, articles_or_groups in self.captured_articles.items():
            complex_info = self.complexes.get(cid, "")
            cname = str(cid)
            household_count_from_list = 0
            
            if isinstance(complex_info, dict):
                cname = complex_info.get("name", str(cid))
                household_count_from_list = complex_info.get("households", 0)
            elif complex_info:
                cname = str(complex_info)

            flat_articles = []

            for item in articles_or_groups:
                if "articleInfoList" in item:
                    flat_articles.extend(item["articleInfoList"])
                elif "representativeArticleInfo" in item:
                    flat_articles.append(item["representativeArticleInfo"])
                elif (
                    "articleName" in item
                    or "priceInfo" in item
                    or "articleNo" in item
                    or "articleNumber" in item
                ):
                    flat_articles.append(item)

            if not flat_articles:
                continue

            groups = {}
            for art in flat_articles:
                space = art.get("spaceInfo", {})
                if not space and "supplySpaceName" in art:
                    space = art

                s_name = space.get("supplySpaceName", "")
                e_name = space.get("exclusiveSpaceName", "")
                if not s_name:
                    s_name = str(space.get("supplySpace", ""))
                if not e_name:
                    e_name = str(space.get("exclusiveSpace", ""))

                ptp_key = f"{s_name}_{e_name}"

                if ptp_key not in groups:
                    groups[ptp_key] = {"trade": [], "rent": [], "info": art}

                t_type = art.get("tradeType", "")

                # Floor Extraction
                floor_info = art.get("floorDetailInfo")
                floor_str = ""

                # If not at top level, check inside articleDetail
                if not floor_info:
                    detail = art.get("articleDetail", {})
                    floor_info = detail.get("floorDetailInfo")
                    
                    # If still no detail dict, check for simple string in detail
                    if not floor_info:
                         floor_raw = detail.get("floorInfo", "")
                         if floor_raw:
                             floor_str = floor_raw

                if floor_info:
                    target = floor_info.get("targetFloor", "")
                    total = floor_info.get("totalFloor", "")
                    floor_str = f"{target}/{total}"
                elif not floor_str:
                    # Fallback to top-level simple keys
                    target = art.get("floorLayerName", "")
                    total = art.get("totalFloor", "")
                    if target or total:
                        floor_str = f"{target}/{total}"
                    else:
                        floor_str = "-"

                # Price Extraction
                price_info = art.get("priceInfo", {})
                price = 0
                if t_type in ["A1", "매매"]:
                    price = (
                        price_info.get("dealPrice", 0)
                        if price_info
                        else art.get("dealPrice", 0)
                    )
                elif t_type in ["B1", "전세"]:
                    # Jeonse uses 'warrantyPrice' usually
                    if price_info:
                        price = price_info.get("warrantyPrice", 0)
                        if price == 0:
                             price = price_info.get("leasePrice", 0)
                    else:
                        price = art.get("warrantyPrice") or art.get("leasePrice", 0)

                art["_mapped_price"] = price
                art["_mapped_floor"] = floor_str

                # Article No for deduplication
                article_no = art.get("articleNo") or art.get("articleNumber")

                if t_type == "A1" or t_type == "매매":
                    # Simple deduplication within group
                    if not any(
                        x.get("articleNo") == article_no
                        for x in groups[ptp_key]["trade"]
                    ):
                        groups[ptp_key]["trade"].append(art)
                elif t_type == "B1" or t_type == "전세":
                    # No floor filter for Jeonse as per user request
                    if not any(
                        x.get("articleNo") == article_no
                        for x in groups[ptp_key]["rent"]
                    ):
                        groups[ptp_key]["rent"].append(art)

            for ptp_key, g in groups.items():
                if not g["trade"] and not g["rent"]:
                    continue

                # --- Analysis Logic ---
                # Trade Analysis
                trade_all = g["trade"]
                trade_standard = []
                trade_special = []  # Low, Top, 1-3

                for item in trade_all:
                     floor_s = item.get("_mapped_floor", "-")
                     if processor.is_low_or_top_floor(floor_s):
                         trade_special.append(item)
                     else:
                         trade_standard.append(item)
                
                # Sort
                trade_standard.sort(key=lambda x: int(x.get("_mapped_price", 999999999)))
                trade_special.sort(key=lambda x: int(x.get("_mapped_price", 999999999)))
                trade_all.sort(key=lambda x: int(x.get("_mapped_price", 999999999)))

                # Stats: Min Price (Standard)
                t_min_std = 0
                if trade_standard:
                    t_min_std = trade_standard[0]["_mapped_price"]
                
                # Stats: Min Price (Low/Top)
                t_min_spc = 0
                if trade_special:
                    t_min_spc = trade_special[0]["_mapped_price"]
                
                # Stats: Max Price (All)
                t_max = 0
                if trade_all:
                    t_max = trade_all[-1]["_mapped_price"]
                
                # Stats: Average Price (All)
                t_avg = 0
                if trade_all:
                    total_price = sum(x["_mapped_price"] for x in trade_all)
                    t_avg = total_price / len(trade_all)
                
                # Stats: Total Count
                t_count = len(trade_all)

                # Use Min Price (Standard) as baseline for Gap if available, else Min Price (All)
                base_price_for_gap = t_min_std if t_min_std > 0 else (t_min_spc if t_min_spc > 0 else 0)

                # Jeonse Info (unchanged logic for now, just sort)
                g["rent"].sort(key=lambda x: int(x.get("_mapped_price", 999999999)))
                r_min = 0
                r_max = 0
                r_avg = 0
                r_floor = "-"
                r_count = len(g["rent"])
                if g["rent"]:
                    best_rent = g["rent"][0]
                    r_min = best_rent["_mapped_price"]
                    r_max = g["rent"][-1]["_mapped_price"]
                    r_avg = sum(x["_mapped_price"] for x in g["rent"]) / r_count
                    r_floor = best_rent.get("_mapped_floor", "-")

                # Gap & Ratio
                gap = 0
                ratio = 0
                if base_price_for_gap > 0 and r_min > 0:
                    gap = base_price_for_gap - r_min
                    # gap_str = processor.format_price(gap)
                    ratio = (r_min / base_price_for_gap) * 100
                    # ratio_str = f"{ratio:.1f}%"

                info = g["info"]
                space = info.get("spaceInfo", {})
                if not space:
                    space = info

                # Region Name Extraction
                address = info.get("address", {})
                city = address.get("city", "")
                dist = address.get("division", "")
                dong = address.get("sector", "")

                # Building Info
                building = info.get("buildingInfo", {})
                completion_date = building.get("buildingConjunctionDate", "")
                age = building.get("approvalElapsedYear", "")
                total_households = household_count_from_list or building.get("totalHouseholdCount", 0)
                
                # Format Age: "YYYY (N년차)"
                final_age = "-"
                year_str = completion_date[:4] if completion_date and len(completion_date) >= 4 else ""
                if year_str and age:
                    final_age = f"{year_str} ({age}년차)"
                elif age:
                     final_age = f"{age}년차"
                elif year_str:
                     final_age = year_str

                link_url = f"https://fin.land.naver.com/complexes/{cid}"

                results.append(
                    {
                        "시/도": city,
                        "시/군/구": dist,
                        "읍/면/동": dong,
                        "아파트명": cname,
                        "총세대수": total_households,
                        "준공일": completion_date,
                        "연식": final_age,
                        "평형": f"{space.get('supplySpace')}/{space.get('exclusiveSpace')}m² ({space.get('supplySpaceName')})",
                        "공급면적": float(space.get("supplySpace", 0)),
                        "전용면적": float(space.get("exclusiveSpace", 0)),
                        "매매 최저가 (일반)": t_min_std,
                        "매매 최저가 (저층/탑층)": t_min_spc,
                        "매매 최고가": t_max,
                        "매매 평균가": int(t_avg),
                        "매매 매물수 (전체)": t_count,
                        "전세 최저가": r_min,
                        "전세 최고가": r_max,
                        "전세 평균가": int(r_avg),
                        "전세 층수": r_floor,
                        "전세 매물수": r_count,
                        "갭": gap, # Raw Gap
                        "전세가율": ratio, # Raw Ratio (Already * 100 in calc above)
                        "링크": link_url,
                    }
                )

        return pd.DataFrame(results)

    # Helper to avoid error on first remove_listener logic?
    # Simply initialize wrapper as dummy
    handle_response_wrapper = lambda s: None


async def main():
    crawler = NaverLandPlaywright()
    print("Playwright 크롤러 시작... (다중 지역 지원)")
    print(f"대상 지역 수: {len(TARGET_URLS)}")

    await crawler.run_test(headless=True)

    df = crawler.process_data()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"naver_land_result_{timestamp}.xlsx"

    if not df.empty:
        # --- 1. Create Summary DataFrame ---
        # Group by complex and LOOSE Exclusive Space
        # User requested "Similar exclusive space should be merged". 
        # "Difference within 3". 
        # Using a bin size of 2 on Exclusive Space.
        if "전용면적" in df.columns:
             df["PyeongGroup"] = df["전용면적"].agg(lambda x: int(x / 2))
        else:
             # Fallback if Exclusive Space missing (shouldn't happen)
             df["PyeongGroup"] = df["공급면적"].agg(lambda x: int(x / 2))
        
        agg_rules = {
            "총세대수": "first",
            "연식": "first",
            "매매 최저가 (일반)": "min",
            "전세 최저가": "min",
            "매매 매물수 (전체)": "sum",
            "전세 매물수": "sum",
            "공급면적": "mean",
            "전용면적": "mean",
            "링크": "first"
        }
        
        # Valid numerical columns only for aggregation source
        # Note: "매매 최저가 (일반)", "전세 최저가" might be 0 if not exist, need handling
        numeric_cols_src = [
            "총세대수", "공급면적", "전용면적",
            "매매 최저가 (일반)", "전세 최저가",
            "매매 매물수 (전체)", "전세 매물수"
        ]
         # Ensure numerics
        for col in numeric_cols_src:
             df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # Grouping
        group_keys = ["시/도", "시/군/구", "읍/면/동", "아파트명", "PyeongGroup"]
        # Include necessary static columns in keys or Agg? PyeongGroup is key. 
        df_summary = df.groupby(group_keys).agg(agg_rules).reset_index()
        
        # --- Recalculate Derived Metrics on Aggregated Data ---
        # Gap = Trade Min - Jeonse Min
        def calc_gap(row):
            t = row["매매 최저가 (일반)"]
            j = row["전세 최저가"]
            if t > 0 and j > 0:
                return t - j
            return 0
            
        # Ratio = Jeonse Min / Trade Min * 100
        def calc_ratio(row):
             t = row["매매 최저가 (일반)"]
             j = row["전세 최저가"]
             if t > 0 and j > 0:
                 return (j / t) * 100
             return 0

        df_summary["갭"] = df_summary.apply(calc_gap, axis=1)
        df_summary["전세가율(최저)"] = df_summary.apply(calc_ratio, axis=1)
        
        # Date
        today_str = datetime.now().strftime("%Y-%m-%d")
        df_summary["수집일"] = today_str

        # Format Columns
        # Supply / Exclusive Pyeong
        df_summary["공급평형"] = df_summary["공급면적"].apply(lambda x: f"{x:.2f}")
        df_summary["전용평형"] = df_summary["전용면적"].apply(lambda x: f"{x:.2f}")

        # Hyperlink
        # Excel Hyperlink Formula: =HYPERLINK("url", "LinkText")
        # Note: '링크' column currently has URL.
        # df_summary["링크"] = df_summary["링크"].apply(lambda x: f'=HYPERLINK("{x}", "이동")')
        # However, openpyxl writer usually handles simple strings. For formula, need specific care. 
        # Or just leave as URL or cleaner TEXT. User said "Hyperlink rather than full URL". 
        # Let's try to make it an Excel formula.
        df_summary["링크"] = df_summary["링크"].apply(lambda x: f'=HYPERLINK("{x}", "이동")')

        # --- 2. Formatting Helper ---
        def format_cols(dataframe, is_summary=False):
            # Price columns - JUST Comma format, no Korean text
            # Convert to 'Man-won' unit: 10000 -> 1
            p_cols = [
                "매매 최저가 (일반)", "매매 최저가 (저층/탑층)", "매매 최고가", "매매 평균가", 
                "전세 최저가", "전세 최고가", "전세 평균가", "갭"
            ]
            for c in p_cols:
                if c in dataframe.columns:
                     # Remove .0 for clean integers if possible
                    dataframe[c] = dataframe[c].apply(lambda x: f"{int(x / 10000):,}" if x != 0 else "-")
            
            # Ratios
            r_cols = ["전세가율", "전세가율(최저)", "전세가율(평균)"]
            for r in r_cols:
                if r in dataframe.columns:
                    dataframe[r] = dataframe[r].apply(lambda x: f"{x:.1f}%" if x > 0 else "-")
            
            return dataframe

        # Apply formatting
        df_detail_formatted = format_cols(df.copy())
        df_summary_formatted = format_cols(df_summary.copy(), is_summary=True)

        # Reorder / Rename Summary Columns
        # Target: 시/도, 시/군/구, 읍/면/동, 아파트명, 총세대수, 공급평형, 전용평형, 연식, 
        #         매매 최저가(일반), 전세 최저가, 매매 매물수, 전세 매물수, 갭, 전세가율(최저), 링크, 수집일
        
        rename_map = {
            "매매 매물수 (전체)": "매매 매물수",
            # Others match roughly or need direct selection
        }
        df_summary_formatted.rename(columns=rename_map, inplace=True)

        final_cols = [
            "시/도", "시/군/구", "읍/면/동", "아파트명", "총세대수", 
            "공급평형", "전용평형", "연식", 
            "매매 최저가 (일반)", "전세 최저가", 
            "매매 매물수", "전세 매물수", 
            "갭", "전세가율(최저)", 
            "링크", "수집일"
        ]
        
        # Select (ensure exist)
        final_cols = [c for c in final_cols if c in df_summary_formatted.columns]
        df_summary_formatted = df_summary_formatted[final_cols]

        # Save to Multi-sheet Excel
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df_summary_formatted.to_excel(writer, sheet_name='평형별_요약', index=False)
            df_detail_formatted.to_excel(writer, sheet_name='상세내역', index=False)

        print(f"완료! 저장된 파일: {filename}")
        print(f"총 {len(df)}개 데이터 수집됨.")
    else:
        print("데이터가 수집되지 않았습니다.")


if __name__ == "__main__":
    asyncio.run(main())
