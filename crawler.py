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
    "https://fin.land.naver.com/regions?si=1100000000&gun=1171000000&eup=1171011100", # Seoul Songpa-gu Bangi-dong
]

# Filtering Options
MIN_HOUSEHOLDS = 100        # Minimum number of households
EXCLUDE_LOW_FLOORS = True   # Exclude 1st, 2nd, 3rd floors, and "Low" labeled floors
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("crawler.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class DataProcessor:
    @staticmethod
    def is_filtered_floor(floor_info: str) -> bool:
        if not floor_info: return False
        if not EXCLUDE_LOW_FLOORS: return False
        
        target_floors = ["1", "2", "3", "저"]
        floor_str = floor_info.split("/")[0].strip()
        
        if floor_str in target_floors: return True
        if floor_str.isdigit() and int(floor_str) <= 3: return True
        if "탑" in floor_info: return True
            
        parts = floor_info.split("/")
        if len(parts) == 2:
            curr, total = parts[0].strip(), parts[1].strip()
            if curr.isdigit() and total.isdigit():
                if int(curr) == int(total): return True
        return False

    @staticmethod
    def format_price(num):
        if num == 0: return "-"
        # Input is in Won (e.g., 1,600,000,000)
        eok = num // 100000000
        remainder = num % 100000000
        man = remainder // 10000
        
        if man > 0: return f"{eok}억 {man:,}"
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
                    "--disable-setuid-sandbox"
                ]
            )
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                locale="ko-KR",
                timezone_id="Asia/Seoul"
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
                await page.goto("https://m.land.naver.com/", wait_until="networkidle", timeout=30000)
            except:
                logging.warning("Warm-up navigation timed out, proceeding...")

            # ---------------------------
            # 2. Iterate Regions
            # ---------------------------
            for target_url in TARGET_URLS:
                logging.info(f"Visiting Region URL: {target_url}")
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
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
                            logging.info("'More' button not found or not visible. Assuming full list loaded.")
                            break
                    except Exception as e:
                        logging.warning(f"Error checking/clicking 'More' button: {e}")
                        break

                # ---------------------------
                # Extract & Filter in List
                # ---------------------------
                logging.info("Extracting and filtering complexes...")
                
                try:
                    await page.wait_for_selector("li[class*='ComplexItem_article']", timeout=15000)
                except:
                    logging.warning("Timeout waiting for complex list.")

                complex_items = await page.query_selector_all("li[class*='ComplexItem_article']")
                filtered_cids = []
                
                for item in complex_items:
                    try:
                        # Link & CID
                        link_el = await item.query_selector("a[class*='ComplexItem_link']")
                        if not link_el: continue
                        href = await link_el.get_attribute("href")
                        match = re.search(r'/complexes/(\d+)', href)
                        if not match: continue
                        cid = match.group(1)
                        
                        # Name
                        name_el = await item.query_selector("strong[class*='ComplexItem_name']")
                        name = await name_el.inner_text() if name_el else f"Complex_{cid}"
                        
                        # Type Filter (Strict Apartment)
                        is_apt = False
                        badge_el = await item.query_selector("span[class*='TitleBadge_article']")
                        if badge_el:
                             badge_text = await badge_el.inner_text()
                             if "아파트" in badge_text and "오피스텔" not in badge_text:
                                 is_apt = True
                        
                        if not is_apt: continue

                        # Household Filter
                        info_items = await item.query_selector_all("li[class*='ComplexItem_item-info']")
                        households = 0
                        for info in info_items:
                            text = await info.inner_text()
                            if "세대" in text:
                                h_match = re.search(r'(\d[\d,]*)\s*세대', text)
                                if h_match:
                                    households = int(h_match.group(1).replace(",", ""))
                                    break
                        
                        if households < MIN_HOUSEHOLDS: continue
                            
                        self.complexes[cid] = name
                        filtered_cids.append(cid)
                        logging.info(f"Found Target: {name} ({cid}) - {households}세대")
                        
                    except Exception as e:
                        continue
                
                logging.info(f"Total targets found in this region: {len(filtered_cids)}")
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
                            if "article/list" in url: is_target = True
                            if "realtor/advertisement" in url: is_target = True
                            
                            if is_target:
                                 data = await response.json()
                                 if 'result' in data:
                                     res = data['result']
                                     items = []
                                     
                                     if isinstance(res, list):
                                         items = res
                                     elif isinstance(res, dict) and 'list' in res:
                                         items = res['list']
                                     
                                     if items:
                                         cid = None
                                         
                                         # 1. Try URL (GET)
                                         match = re.search(r'complexNumber=(\d+)', url)
                                         if match:
                                             cid = match.group(1)
                                         
                                         # 2. Try POST Data
                                         if not cid:
                                             try:
                                                 req = response.request
                                                 post_data = req.post_data_json
                                                 if post_data and 'complexNumber' in post_data:
                                                      cid = str(post_data['complexNumber'])
                                                 
                                                 if not cid and req.post_data:
                                                      pmatch = re.search(r'complexNumber=(\d+)', req.post_data)
                                                      if pmatch:
                                                          cid = pmatch.group(1)
                                             except:
                                                 pass
                                         
                                         if cid:
                                             if cid not in self.captured_articles: self.captured_articles[cid] = []
                                             # Avoid duplicates if possible? checking ID might be expensive. 
                                             # Just extend for now.
                                             self.captured_articles[cid].extend(items)
                                             logging.info(f"Captured {len(items)} items for {self.complexes.get(cid, cid)}...")
                    except:
                        pass
                
                self.handle_response_wrapper = handle_response # Store ref
                page.on("response", self.handle_response_wrapper)
                
                # ---------------------------
                # Visit Filtered URLs
                # ---------------------------
                for cid in filtered_cids:
                    detail_url = f"https://fin.land.naver.com/complexes/{cid}?tab=article&tradeType=A1&articleTradeTypes=A1&articleSortingType=PRICE_ASC"
                    logging.info(f"Visiting {self.complexes[cid]}...")
                    
                    try:
                        await page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
                    except Exception as e:
                        logging.warning(f"Nav error {cid}: {e}")
                    
                    # Wait for API to fire
                    await page.wait_for_timeout(4000)
            
            await browser.close()

    def process_data(self):
        processor = DataProcessor()
        results = []
        
        logging.info(f"Processing data for {len(self.captured_articles)} complexes...")
        
        for cid, articles_or_groups in self.captured_articles.items():
            cname = self.complexes.get(cid, str(cid))
            
            flat_articles = []
            
            for item in articles_or_groups:
                if 'articleInfoList' in item:
                    flat_articles.extend(item['articleInfoList'])
                elif 'representativeArticleInfo' in item:
                    flat_articles.append(item['representativeArticleInfo'])
                elif 'articleName' in item or 'priceInfo' in item or 'articleNo' in item or 'articleNumber' in item:
                    flat_articles.append(item)
                
            if not flat_articles:
                continue

            groups = {}
            for art in flat_articles:
                space = art.get('spaceInfo', {})
                if not space and 'supplySpaceName' in art:
                    space = art 
                
                s_name = space.get('supplySpaceName', '')
                e_name = space.get('exclusiveSpaceName', '')
                if not s_name: s_name = str(space.get('supplySpace', ''))
                if not e_name: e_name = str(space.get('exclusiveSpace', ''))
                
                ptp_key = f"{s_name}_{e_name}"
                
                if ptp_key not in groups:
                    groups[ptp_key] = {'trade': [], 'info': art}
                
                t_type = art.get('tradeType', '')
                
                # Floor
                floor_info = art.get('floorDetailInfo', {})
                if not floor_info:
                    target = art.get('floorLayerName', '') 
                    total = art.get('totalFloor', '')
                    floor_str = f"{target}/{total}"
                else:
                    target = floor_info.get('targetFloor', '')
                    total = floor_info.get('totalFloor', '')
                    floor_str = f"{target}/{total}"
                
                # Price
                price_info = art.get('priceInfo', {})
                price = 0
                if price_info:
                    price = price_info.get('dealPrice', 0)
                else:
                    price = art.get('dealPrice', 0)
                
                art['_mapped_price'] = price
                art['_mapped_floor'] = floor_str
                
                if t_type == 'A1' or t_type == '매매': 
                     if not processor.is_filtered_floor(floor_str):
                        groups[ptp_key]['trade'].append(art)
            
            # Deduplicate items in groups['trade'] based on articleNo to avoid double counting from multiple paginations if any
            # (Current logic doesn't paginate, but safe to be robust)

            for ptp_key, g in groups.items():
                # Sort by price
                g['trade'].sort(key=lambda x: int(x.get('_mapped_price', 999999999)))
                if not g['trade']: continue
                
                best_trade = g['trade'][0]
                t_price = best_trade['_mapped_price']
                
                info = g['info']
                space = info.get('spaceInfo', {})
                if not space: space = info
                
                link_url = f"https://fin.land.naver.com/complexes/{cid}"

                results.append({
                    "지역": "개포동", # Note: ideally extract this from region info
                    "아파트명": cname,
                    "평형": f"{space.get('supplySpace')}m² ({space.get('supplySpaceName')})",
                    "매매 최저가": processor.format_price(t_price),
                    "매매 층수": best_trade.get('_mapped_floor', '-'),
                    "전세 최저가": "-", 
                    "전세 층수": "-",
                    "갭": "-", 
                    "전세가율": "-",
                    "링크": link_url
                })
        
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
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"naver_land_result_{timestamp}.xlsx"
    
    if not df.empty:
        df.to_excel(filename, index=False)
        print(f"완료! 저장된 파일: {filename}")
        print(f"총 {len(df)}개 데이터 수집됨.")
    else:
        print("데이터가 수집되지 않았습니다.")

if __name__ == "__main__":
    asyncio.run(main())
