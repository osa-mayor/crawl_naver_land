import asyncio
import re
import json
import random
import pandas as pd
from playwright.async_api import async_playwright
import logging
from datetime import datetime, timezone, timedelta
import os
import sqlite3
import argparse
import sys
import socket
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from land_selectors import NaverLandSelectors
from region_navigation import navigate_region, read_region_total, RegionNavigationError

# ==========================================
# [Configuration]
# ==========================================
# Target Regions (Search by Name using naver_region_codes.json)
TARGET_REGIONS = ["경기도"]

# (Optional) RAW URLs override or addition
TARGET_URLS = []

# Filtering Options
MIN_HOUSEHOLDS = 0
EXCLUDE_LOW_FLOORS = False

# [System Config]
MAX_CONCURRENT_PAGES = int(
    os.getenv("MAX_CONCURRENT_PAGES", 3)
)  # Configurable via Env Var
MAX_API_PREFETCH_CONCURRENCY = int(os.getenv("MAX_API_PREFETCH_CONCURRENCY", 6))
NAVIGATION_RETRY_ATTEMPTS = int(os.getenv("NAVIGATION_RETRY_ATTEMPTS", 3))
NETWORK_READY_RETRY_ATTEMPTS = int(os.getenv("NETWORK_READY_RETRY_ATTEMPTS", 12))
NETWORK_READY_RETRY_DELAY_SECONDS = float(
    os.getenv("NETWORK_READY_RETRY_DELAY_SECONDS", 5)
)
MAX_FAILED_REGION_RATIO = float(os.getenv("MAX_FAILED_REGION_RATIO", 0.25))
MAX_RATE_LIMIT_RESPONSES = int(os.getenv("MAX_RATE_LIMIT_RESPONSES", 20))
ARTICLE_RESPONSE_TIMEOUT_MS = int(os.getenv("ARTICLE_RESPONSE_TIMEOUT_MS", 15000))
MAX_ARTICLE_PAGES_PER_TRADE = int(os.getenv("MAX_ARTICLE_PAGES_PER_TRADE", 80))
MAX_ARTICLE_MISSING_COMPLEX_RATIO = float(
    os.getenv("MAX_ARTICLE_MISSING_COMPLEX_RATIO", 0.50)
)
MIN_COMPLEXES_FOR_ARTICLE_CHECK = int(os.getenv("MIN_COMPLEXES_FOR_ARTICLE_CHECK", 20))
HEADLESS_MODE = True  # Set to False to watch process
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "real_estate.db")  # SQLite Database File
REGION_JSON_PATH = str(
    Path(os.getenv("NAVER_REGION_JSON_PATH", BASE_DIR / "naver_region_codes.json"))
)
CRAWLER_LOG_PATH = str(
    Path(os.getenv("CRAWLER_LOG_PATH", BASE_DIR / "crawling_db.log"))
)
SCREENSHOT_DIR = str(Path(os.getenv("CRAWLER_SCREENSHOT_DIR", BASE_DIR)))

# ==========================================


def ensure_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def ensure_parent_directory(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def sanitize_filename(value):
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "unnamed"


def normalize_business_region(sido, gungu, full_path):
    """Keep the project's metro/province split for the site's merged label."""
    if sido != "전남광주시":
        return sido
    root = str(full_path or "").split()
    if root and root[0] in {"광주시", "전라남도"}:
        return root[0]
    if gungu in {"광산구", "남구", "동구", "북구", "서구"}:
        return "광주시"
    if gungu and gungu.endswith(("시", "군")):
        return "전라남도"
    raise ValueError(f"merged region cannot be classified: {gungu}")


def configure_logging(log_path):
    ensure_parent_directory(log_path)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


TRANSIENT_NAVIGATION_ERROR_MARKERS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_DNS_NO_MATCHING_SUPPORTED_ALPN",
    "Timeout",
    "Target page, context or browser has been closed",
)


def is_transient_navigation_error(error) -> bool:
    message = str(error)
    return any(marker in message for marker in TRANSIENT_NAVIGATION_ERROR_MARKERS)


def can_connect(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def wait_for_network_ready(
    host: str = "fin.land.naver.com",
    attempts: int = NETWORK_READY_RETRY_ATTEMPTS,
    delay_seconds: float = NETWORK_READY_RETRY_DELAY_SECONDS,
) -> bool:
    for attempt in range(1, attempts + 1):
        if await asyncio.to_thread(can_connect, host):
            return True
        logging.warning(
            "Network check failed for %s (%s/%s); waiting %.1fs",
            host,
            attempt,
            attempts,
            delay_seconds,
        )
        await asyncio.sleep(delay_seconds)
    return False


class DataProcessor:
    @staticmethod
    def positive_price(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            if isinstance(value, str):
                value = value.replace(",", "").strip()
            price = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
        return price if price > 0 else None

    @staticmethod
    def floor_category(article) -> str:
        floor_info = article.get("floorDetailInfo")
        if not floor_info:
            floor_info = (article.get("articleDetail") or {}).get("floorDetailInfo")

        floor_value = ""
        if isinstance(floor_info, dict):
            floor_value = floor_info.get("targetFloor", "")
        elif floor_info:
            floor_value = str(floor_info).split("/")[0]

        if not floor_value:
            floor_value = article.get("floorInfo", "")
        if not floor_value:
            floor_value = (article.get("articleDetail") or {}).get("floorInfo", "")

        floor_value = str(floor_value).split("/")[0].strip()
        if floor_value in {"1", "2", "3", "저"}:
            return "low"
        if floor_value.isdigit():
            return "normal" if int(floor_value) > 3 else "low"
        if floor_value in {"중", "고"}:
            return "normal"
        return "unknown"

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


class ArticleResponseUnavailable(RuntimeError):
    pass


class NaverLandPlaywright:
    def __init__(self, screenshot_dir=None):
        self.complexes = {}
        self.captured_articles = {}
        self.article_response_stats = {}
        self.article_pagination_states = {}
        self.article_response_tasks = {}
        self.article_seen_ids = {}
        self.region_results = []
        self.list_inventories = {}
        self.region_resolutions = {}
        self.article_attempts = set()
        self.article_missing_responses = []
        self.failed_regions = []
        self.blocking_error = None
        self.rate_limit_count = 0
        self.region_name = ""
        self.screenshot_dir = Path(screenshot_dir or SCREENSHOT_DIR)
        ensure_directory(self.screenshot_dir)

    async def _wait_buffer(self, minimum, maximum):
        """Historical per-page buffer; independent pages may wait concurrently."""
        if self.blocking_error or self.rate_limit_count:
            raise ArticleResponseUnavailable(self.blocking_error or 'paused after rate limiting')
        await asyncio.sleep(random.uniform(minimum, maximum))
        if self.blocking_error or self.rate_limit_count:
            raise ArticleResponseUnavailable(self.blocking_error or 'paused after rate limiting')

    def get_context_options(self):
        return {
            # Preserve the browser identity used by the previously working crawler.
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "viewport": {"width": 1280, "height": 720},
            "is_mobile": False,
            "has_touch": False,
            "extra_http_headers": {
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        }

    @staticmethod
    def _request_post_json(request):
        try:
            value = getattr(request, "post_data_json", None)
            if callable(value):
                value = value()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _article_query_values(url):
        try:
            query = parse_qs(urlparse(url).query)
        except Exception:
            return {}
        return {key: values[0] for key, values in query.items() if values}

    @staticmethod
    def _is_article_api_url(url):
        return "/front-api/v1/complex/article/list" in url

    def _extract_article_identity(self, response, request_body=None):
        url = response.url
        query = self._article_query_values(url)
        post = request_body or {}
        if not post:
            request = getattr(response, "request", None)
            post = self._request_post_json(request) if request is not None else {}

        cid = query.get("complexNumber") or post.get("complexNumber")
        trade_type = (
            query.get("tradeType")
            or query.get("articleTradeTypes")
            or query.get("tradeTypes")
            or post.get("tradeType")
            or post.get("articleTradeTypes")
            or post.get("tradeTypes")
        )

        if isinstance(trade_type, list):
            trade_type = trade_type[0] if trade_type else None
        if cid is not None:
            cid = str(cid)
        if trade_type is not None:
            trade_type = str(trade_type)

        return cid, trade_type

    def _is_expected_article_response(self, response, cid, trade_type):
        if not self._is_article_api_url(response.url):
            return False

        found_cid, found_trade_type = self._extract_article_identity(response)
        if found_cid != str(cid):
            return False

        # Some endpoints may use articleTradeTypes/tradeTypes; require the target
        # trade type when the response exposes one.
        if found_trade_type and str(trade_type) not in found_trade_type.split(","):
            return False
        return True

    @staticmethod
    def _extract_article_items(data):
        if not isinstance(data, dict) or "result" not in data:
            return []

        result = data["result"]
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "list" in result:
            value = result["list"]
            return value if isinstance(value, list) else []
        return []

    @staticmethod
    def _trade_type_keys(trade_type):
        if not trade_type:
            return ["UNKNOWN"]
        if isinstance(trade_type, list):
            values = trade_type
        else:
            values = str(trade_type).split(",")
        keys = [str(value).strip() for value in values if str(value).strip()]
        return keys or ["UNKNOWN"]

    def _article_stats_for(self, cid, trade_type):
        return self.article_response_stats.setdefault(
            (str(cid), str(trade_type)),
            {
                "response_count": 0,
                "status_counts": {},
                "parsed_200_count": 0,
                "item_count": 0,
                "empty_200_count": 0,
                "non_200_count": 0,
                "json_error_count": 0,
                "last_url": "",
            },
        )

    def _article_pagination_key(self, cid, trade_type):
        trade_keys = self._trade_type_keys(trade_type)
        trade_key = trade_keys[0] if trade_keys else "UNKNOWN"
        return (str(cid), str(trade_key))

    def _extract_request_body(self, response, request_body=None):
        body = request_body or {}
        if not body:
            request = getattr(response, "request", None)
            body = self._request_post_json(request) if request is not None else {}
        if body:
            return dict(body)
        return {}

    def _update_article_pagination_state(self, response, data, request_body=None):
        if not isinstance(data, dict):
            return
        result = data.get("result")
        if not isinstance(result, dict):
            return

        cid, trade_type = self._extract_article_identity(response, request_body)
        if not cid or not trade_type:
            return

        body = self._extract_request_body(response, request_body)
        if not body:
            return

        key = self._article_pagination_key(cid, trade_type)
        self.article_pagination_states[key] = {
            "body": body,
            "has_next_page": bool(result.get("hasNextPage")),
            "last_info": result.get("lastInfo") or [],
            "seed": result.get("seed"),
            "total_count": result.get("totalCount"),
        }

    async def _record_article_response(self, response, request_body=None):
        if not self._is_article_api_url(response.url):
            return False
        # Join consumers of this response, not later responses to the same URL/body.
        key = response
        task = self.article_response_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._parse_article_response(response, request_body))
            self.article_response_tasks[key] = task
        try:
            # A timed-out attempt must not keep parsing into the next attempt.
            return await task
        finally:
            if task.done() and self.article_response_tasks.get(key) is task:
                self.article_response_tasks.pop(key, None)

    def _reset_article_pair(self, cid, trade_type):
        """Retry a fresh snapshot without mixing old/deleted listings into it."""
        cid, trade_type = str(cid), str(trade_type)
        key = (cid, trade_type)
        for state in (self.article_response_stats, self.article_pagination_states,
                      self.article_seen_ids):
            state.pop(key, None)
        self.article_attempts.discard(key)
        remaining = [
            item for item in self.captured_articles.get(cid, [])
            if (item.get('representativeArticleInfo') or item).get('tradeType') != trade_type
        ]
        if remaining:
            self.captured_articles[cid] = remaining
        else:
            self.captured_articles.pop(cid, None)

    async def _parse_article_response(self, response, request_body=None):
        cid, trade_type = self._extract_article_identity(response, request_body)
        if not cid or not trade_type:
            return False
        status = int(response.status)
        trade_keys = self._trade_type_keys(trade_type)
        stats_list = [self._article_stats_for(cid, key) for key in trade_keys]
        for stats in stats_list:
            stats['response_count'] += 1
            stats['status_counts'][str(status)] = stats['status_counts'].get(str(status), 0) + 1
            stats['last_url'] = response.url
        if status == 429:
            self.rate_limit_count += 1
            logging.error('Naver rate limited article request: %s', response.url)
        if status != 200:
            for stats in stats_list:
                stats['non_200_count'] += 1
            return False
        try:
            data = await response.json()
            result = data.get('result') if isinstance(data, dict) else None
            if (not isinstance(data, dict) or data.get('isSuccess') is False
                    or not isinstance(result, dict) or not isinstance(result.get('list'), list)
                    or not isinstance(result.get('hasNextPage'), bool)
                    or isinstance(result.get('totalCount'), bool)
                    or not isinstance(result.get('totalCount'), int)
                    or result['totalCount'] < 0):
                raise ValueError('unrecognized article response schema')
            items = result['list']
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError('invalid article item')
                representative = item.get('representativeArticleInfo') or item
                if not isinstance(representative, dict) or not representative.get('articleNumber'):
                    raise ValueError('article identity missing')
                item_trade = representative.get('tradeType')
                if item_trade not in trade_keys:
                    raise ValueError('article trade filter mismatch')
                item_cid = representative.get('complexNumber')
                if item_cid is not None and str(item_cid) != cid:
                    raise ValueError('article complex filter mismatch')
                normalized.append((item, str(representative['articleNumber']), item_trade))
            if result['hasNextPage'] and (not items or not result.get('lastInfo')):
                raise ValueError('next page missing items/cursor')
        except Exception as exc:
            for stats in stats_list:
                stats['json_error_count'] += 1
            logging.warning('Article response parse/schema failed: %s (%s)', response.url, exc)
            return False
        self._update_article_pagination_state(response, data, request_body)
        for stats in stats_list:
            stats['parsed_200_count'] += 1
            if not items:
                stats['empty_200_count'] += 1
        for item, article_id, item_trade in normalized:
            seen = self.article_seen_ids.setdefault((cid, item_trade), set())
            if article_id not in seen:
                seen.add(article_id)
                self.captured_articles.setdefault(cid, []).append(item)
                self._article_stats_for(cid, item_trade)['item_count'] += 1
        return True

    def _has_acceptable_article_response(self, cid, trade_type):
        stats = self.article_response_stats.get((str(cid), str(trade_type)))
        return bool(stats and stats.get('parsed_200_count', 0) > 0)

    def _has_complete_article_response(self, cid, trade_type):
        key = (str(cid), str(trade_type))
        state = self.article_pagination_states.get(key)
        if not state or state.get('has_next_page'):
            return False
        return (self._has_acceptable_article_response(cid, trade_type)
                and len(self.article_seen_ids.get(key, set())) == state.get('total_count'))

    async def _visit_detail_page_and_wait_for_article(self, page, url, cid, trade_type):
        try:
            async with page.expect_response(
                lambda response: self._is_expected_article_response(response, cid, trade_type),
                timeout=ARTICLE_RESPONSE_TIMEOUT_MS,
            ) as response_info:
                await self._visit_detail_page(page, url)
            return await self._record_article_response(await response_info.value)
        except Exception as exc:
            logging.warning('Article navigation/capture %s %s: %s', cid, trade_type, exc)
            return self._has_acceptable_article_response(cid, trade_type)

    async def _fetch_remaining_article_pages(self, page, cid, trade_type):
        key = self._article_pagination_key(cid, trade_type)
        state = self.article_pagination_states.get(key)
        if not state:
            raise ArticleResponseUnavailable(f'pagination state missing: {cid} {trade_type}')
        extra_pages = 0
        while state.get('has_next_page'):
            if self.rate_limit_count:
                raise ArticleResponseUnavailable('pagination paused after rate limiting; retry later')
            if extra_pages + 1 >= MAX_ARTICLE_PAGES_PER_TRADE:
                raise ArticleResponseUnavailable(f'pagination cap reached: {cid} {trade_type}')
            body = dict(state['body'])
            body['lastInfo'] = state['last_info']
            if state.get('seed'):
                body['seed'] = state['seed']
            response = None
            try:
                response = await page.request.post(
                    'https://fin.land.naver.com/front-api/v1/complex/article/list',
                    data=json.dumps(body), headers={'Content-Type': 'application/json'},
                    timeout=ARTICLE_RESPONSE_TIMEOUT_MS,
                )
                if not await self._record_article_response(response, body):
                    raise ArticleResponseUnavailable(f'page response failed: {cid} {trade_type} HTTP{response.status}')
            except ArticleResponseUnavailable:
                raise
            except Exception as exc:
                raise ArticleResponseUnavailable(f'pagination request failed: {cid} {trade_type}: {exc}') from exc
            finally:
                if response is not None:
                    await response.dispose()
            extra_pages += 1
            new_state = self.article_pagination_states.get(key)
            if not new_state or (new_state['has_next_page'] and new_state['last_info'] == state['last_info']):
                raise ArticleResponseUnavailable(f'pagination cursor stalled: {cid} {trade_type}')
            state = new_state
            await self._wait_buffer(0.15, 0.35)
        if not self._has_complete_article_response(cid, trade_type):
            raise ArticleResponseUnavailable(
                f'article total mismatch: {cid} {trade_type}; '
                f'unique={len(self.article_seen_ids.get(key, set()))} '
                f'expected={state.get("total_count")} has_next={state.get("has_next_page")}')
        logging.info('Article collection COMPLETE %s (%s): unique=%s extra_pages=%s',
                     cid, trade_type, len(self.article_seen_ids.get(key, set())), extra_pages)
        return extra_pages

    def _summarize_article_capture(self, filtered_cids):
        attempted_pairs = {(str(cid), t_type) for cid in filtered_cids for t_type in ["A1", "B1"]}
        observed_pairs = attempted_pairs & set(self.article_response_stats.keys())
        missing_pairs = attempted_pairs - observed_pairs
        non_200_pairs = {
            key
            for key in observed_pairs
            if self.article_response_stats[key].get("non_200_count", 0) > 0
            and not self._has_acceptable_article_response(*key)
        }
        empty_pairs = {
            key
            for key in observed_pairs
            if self.article_response_stats[key].get("empty_200_count", 0) > 0
            and self.article_response_stats[key].get("item_count", 0) == 0
        }
        nonempty_pairs = {
            key
            for key in observed_pairs
            if self.article_response_stats[key].get("item_count", 0) > 0
        }

        observed_complexes = {
            cid for cid, _ in observed_pairs
        }
        complexes_with_articles = {
            str(cid) for cid in filtered_cids if str(cid) in self.captured_articles
        }
        missing_all_complexes = {
            str(cid) for cid in filtered_cids if str(cid) not in observed_complexes
        }
        empty_confirmed_complexes = {
            str(cid)
            for cid in filtered_cids
            if str(cid) not in complexes_with_articles
            and (str(cid), "A1") in empty_pairs
            and (str(cid), "B1") in empty_pairs
        }

        complete_pairs = {key for key in attempted_pairs if self._has_complete_article_response(*key)}
        return {
            "complete_pairs": len(complete_pairs),
            "incomplete_pairs": len(attempted_pairs - complete_pairs),
            "target_complexes": len(filtered_cids),
            "attempted_pairs": len(attempted_pairs),
            "observed_pairs": len(observed_pairs),
            "nonempty_pairs": len(nonempty_pairs),
            "empty_pairs": len(empty_pairs),
            "non_200_pairs": len(non_200_pairs),
            "missing_pairs": len(missing_pairs),
            "observed_complexes": len(observed_complexes),
            "complexes_with_articles": len(complexes_with_articles),
            "missing_all_complexes": len(missing_all_complexes),
            "empty_confirmed_complexes": len(empty_confirmed_complexes),
            "missing_pair_sample": sorted(list(missing_pairs))[:10],
            "non_200_pair_sample": sorted(list(non_200_pairs))[:10],
        }

    async def run_test(self, target_urls, headless=True):
        """A run is complete only when every routed target and DB commit succeeds."""
        self.region_results = []
        save_ok = False
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
            sem = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
            async def worker(item):
                name, url = item
                result = {'name':name, 'url':url, 'status':'failed'}
                context = None
                async with sem:
                    try:
                        if self.blocking_error or self.rate_limit_count:
                            raise ArticleResponseUnavailable(self.blocking_error or 'paused after rate limiting; retry later')
                        context = await browser.new_context(**self.get_context_options())
                        page = await context.new_page()
                        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                        logging.info('Processing: %s (%s)', name, url)
                        await self._wait_buffer(0.5, 1.5)
                        ok = await self.process_region_tab(page, url, name)
                        inventory = self.list_inventories.get(url, {})
                        result.update(inventory)
                        if ok:
                            result['status'] = 'complete' if inventory.get('apartment_ids') else 'empty'
                        else:
                            result['error'] = 'region collection incomplete'
                    except Exception as exc:
                        result['error'] = str(exc)
                        logging.error('Region failed %s: %s', name, exc)
                    finally:
                        if context is not None:
                            await context.close()
                        self.region_results.append(result)
                        if result['status'] == 'failed':
                            self.failed_regions.append(result)
            try:
                await asyncio.gather(*(worker(item) for item in target_urls))
                save_complexes_metadata_only(self.complexes)
                df = self.process_data()
                if not df.empty:
                    saved_rows = save_to_db(df)
                    if saved_rows is not None and saved_rows != len(df):
                        raise RuntimeError('committed price row count mismatch')
                    logging.info('Committed %s price rows', len(df))
                save_ok = True
            except Exception as exc:
                logging.exception('Run/DB save failed: %s', exc)
            finally:
                await browser.close()
        complete = bool(target_urls) and save_ok and len(self.region_results) == len(target_urls) and not self.failed_regions
        result = {
            'schema_version':1, 'status':'complete' if complete else 'failed',
            'mode':'backfill' if hasattr(self,'skip_complex_ids') else 'full',
            'region_json_sha256':hashlib.sha256(Path(REGION_JSON_PATH).read_bytes()).hexdigest(),
            'expected_target_count':len(target_urls), 'failed_target_count':len(self.failed_regions),
            'targets':sorted(self.region_results, key=lambda row:(row['name'],row['url'])),
            'saved':save_ok,
            'rate_limit_count':self.rate_limit_count,
            'blocking_error':self.blocking_error,
        }
        result_path = Path(DB_PATH + '.result.json')
        ensure_parent_directory(result_path)
        temp = result_path.with_suffix(result_path.suffix + '.tmp')
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temp,result_path)
        return complete

    async def process_region_tab(self, page, target_url, dong_name):
        for attempt in range(1, NAVIGATION_RETRY_ATTEMPTS + 1):
            try:
                if page.is_closed():
                    page = await page.context.new_page()
                    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                if await self._process_region_tab_once(page, target_url, dong_name):
                    return True
            except Exception as exc:
                logging.warning('Region incomplete %s attempt %s/%s: %s', dong_name, attempt, NAVIGATION_RETRY_ATTEMPTS, exc)
            if self.blocking_error or self.rate_limit_count:
                # Respect throttling: stop requests, retain an incomplete result for a later retry.
                return False
            if attempt < NAVIGATION_RETRY_ATTEMPTS:
                await asyncio.sleep(5 * attempt)
        return False

    async def _load_region_complexes(self, page, target_url, region_name=None):
        """Observe throttling before navigation, including region/auth API responses."""
        if self.blocking_error or self.rate_limit_count:
            raise ArticleResponseUnavailable(self.blocking_error or "paused after HTTP 429")
        blocked = asyncio.Event()
        def observe(response):
            host = urlparse(response.url).hostname or ''
            if (host == 'naver.com' or host.endswith('.naver.com')) and response.status == 429:
                self.rate_limit_count += 1
                self.blocking_error = f'HTTP 429: {response.url}; pause before retry'
                blocked.set()
        page.on('response', observe)
        loader = asyncio.create_task(self._load_region_inventory(page, target_url, region_name))
        waiter = asyncio.create_task(blocked.wait())
        try:
            done, _ = await asyncio.wait((loader, waiter), return_when=asyncio.FIRST_COMPLETED)
            if blocked.is_set():
                raise ArticleResponseUnavailable(self.blocking_error)
            return await loader
        finally:
            page.remove_listener('response', observe)
            for task in (loader, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(loader, waiter, return_exceptions=True)

    async def _load_region_inventory(self, page, target_url, region_name=None):
        """Load the full unfiltered inventory, proving count before filtering."""
        self.region_resolutions.pop(target_url, None)
        try:
            resolved = await navigate_region(page, target_url, region_name)
        except RegionNavigationError as exc:
            self.blocking_error = str(exc)
            raise ArticleResponseUnavailable(self.blocking_error) from exc
        if resolved['url'] != target_url:
            self.region_resolutions[target_url] = resolved
            logging.info('Region resolved %s -> %s (%s)', target_url, resolved['url'], resolved['name'])
        expected = await read_region_total(page)
        inventory = {}
        for _ in range(1000):
            if self.blocking_error or self.rate_limit_count:
                raise ArticleResponseUnavailable(self.blocking_error or "paused after HTTP 429")
            rows = await page.evaluate("""() => [...document.querySelectorAll('a[href^="/complexes/"]')]
              .filter(a => /^\/complexes\/\d+$/.test(a.getAttribute('href') || ''))
              .map(a => { const li=a.closest('li'); const name=li?.querySelector('strong[class*="name"]');
                 const badge=li?.querySelector('span[class*="TitleBadge"], span[class*="badge"]');
                 return {id:a.getAttribute('href').split('/').pop(),name:name?.textContent?.trim(),
                   badge:badge?.textContent?.trim(),text:li?.innerText || ''}; })""")
            for row in rows:
                if not row['name'] or not row['text']:
                    raise ArticleResponseUnavailable('complex card could not be parsed')
                row['badge'] = row.get('badge') or row['text'].split('\n')[0].strip()
                row['badge'] = re.sub(r'\s+', '', row['badge'])
                if row['badge'] not in {'아파트', '오피스텔', '분양권', '아파트분양권', '오피스텔분양권'}:
                    raise ArticleResponseUnavailable(f"complex category missing or unknown: {row['badge']!r}")
                inventory[row['id']] = row
            if len(inventory) == expected:
                return list(inventory.values())
            if len(inventory) > expected:
                raise ArticleResponseUnavailable('region total changed during collection')
            more = page.locator(NaverLandSelectors.MORE_BUTTON)
            await more.wait_for(state='visible', timeout=10000)
            previous_ids = list(inventory)
            await more.click(timeout=10000)
            # Wait for a NEW identity, not a transient spinner or arbitrary delay.
            await page.wait_for_function("""ids => [...document.querySelectorAll('a[href^="/complexes/"]')]
                .some(a => /^\/complexes\/\d+$/.test(a.getAttribute('href') || '') &&
                 !ids.includes(a.getAttribute('href').split('/').pop()))""", arg=previous_ids, timeout=15000)
        raise ArticleResponseUnavailable('region pagination safety cap exceeded')

    async def _get_required_json(self, page, url):
        if self.blocking_error or self.rate_limit_count:
            raise ArticleResponseUnavailable('paused after rate limiting; retry later')
        response = await page.request.get(url, timeout=ARTICLE_RESPONSE_TIMEOUT_MS)
        try:
            if response.status == 429:
                self.rate_limit_count += 1
            if response.status != 200:
                raise ArticleResponseUnavailable(f'metadata HTTP {response.status}: {url}')
            data = await response.json()
            if not isinstance(data, dict) or data.get('isSuccess') is False or 'result' not in data:
                raise ArticleResponseUnavailable(f'metadata schema invalid: {url}')
            return data['result']
        finally:
            await response.dispose()

    async def _process_region_tab_once(self, page, target_url, dong_name):
        rows = await self._load_region_complexes(page, target_url, dong_name.rsplit(' ', 1)[-1])
        self.list_inventories[target_url] = {'total':len(rows), 'ids':[r['id'] for r in rows]}
        resolved = self.region_resolutions.get(target_url)
        if resolved:
            dong_name = dong_name.rsplit(' ', 1)[0] + ' ' + resolved['name']
            self.list_inventories[target_url].update(resolved_url=resolved['url'], resolved_name=dong_name)
        filtered_cids = []
        for row in rows:
            badge = row['badge']
            if not (('아파트' in badge or '분양권' in badge) and '오피스텔' not in badge):
                continue
            match = re.search(r'(\d[\d,]*)\s*세대', row['text'])
            if not match:
                raise ArticleResponseUnavailable(f"households missing for {row['id']}")
            households = int(match.group(1).replace(',', ''))
            if households < MIN_HOUSEHOLDS:
                continue
            cid = row['id']
            existing = self.complexes.get(cid, {})
            self.complexes[cid] = {**existing, 'name':row['name'], 'households':households, '_dong_name':dong_name}
            filtered_cids.append(cid)
        self.list_inventories[target_url]['apartment_ids'] = filtered_cids[:]
        logging.info('Region inventory COMPLETE %s: total=%s target_apartments=%s', dong_name,len(rows),len(filtered_cids))
        # Backfill excludes identities already saved in the preserved source run.
        skipped = getattr(self, 'skip_complex_ids', set())
        for cid in filtered_cids:
            if cid in skipped:
                self.complexes.pop(cid, None)
        filtered_cids = [cid for cid in filtered_cids if cid not in skipped]
        if not filtered_cids:
            return True

        async def fetch_one_complex(cid):
            saved = self.complexes[cid]
            detail = await self._get_required_json(page, f'https://fin.land.naver.com/front-api/v1/complex?complexNumber={cid}')
            if not isinstance(detail, dict) or not detail.get('name'):
                raise ArticleResponseUnavailable(f'complex detail missing: {cid}')
            pyeongs = await self._get_required_json(page, f'https://fin.land.naver.com/front-api/v1/complex/pyeongList?complexNumber={cid}')
            if not isinstance(pyeongs, list) or any(not isinstance(p, dict) for p in pyeongs):
                raise ArticleResponseUnavailable(f'pyeong list schema invalid: {cid}')
            self.complexes[cid] = {**saved, **detail, '_dong_name':dong_name,
                                  'households':saved['households'], 'pyeongs':pyeongs}
            if not self.complexes[cid].get('totalHouseholdNumber'):
                self.complexes[cid]['totalHouseholdNumber'] = saved['households']
        sem_api = asyncio.Semaphore(MAX_API_PREFETCH_CONCURRENCY)
        async def sem_task(cid):
            async with sem_api:
                await fetch_one_complex(cid)
        results = await asyncio.gather(*(sem_task(cid) for cid in filtered_cids), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise ArticleResponseUnavailable(f'metadata/pyeong incomplete: {errors[0]}')

        async def recreate_page(current_page):
            context = current_page.context
            try:
                await current_page.close()
            except Exception:
                pass

            new_page = await context.new_page()
            await new_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            return new_page

        async def visit_detail_with_retries(current_page, cid, t_type, retries=2):
            detail_url = f"https://fin.land.naver.com/complexes/{cid}?tab=article&tradeType={t_type}&articleTradeTypes={t_type}&articleSortingType=PRICE_ASC"

            for attempt in range(1, retries + 2):
                try:
                    if self.blocking_error or self.rate_limit_count:
                        raise ArticleResponseUnavailable(self.blocking_error or 'paused after HTTP 429')
                    self._reset_article_pair(cid, t_type)
                    if await asyncio.wait_for(
                        self._visit_detail_page_and_wait_for_article(
                            current_page, detail_url, cid, t_type
                        ),
                        timeout=45.0,
                    ):
                        await self._fetch_remaining_article_pages(
                            current_page, cid, t_type
                        )
                        if not self._has_complete_article_response(cid, t_type):
                            raise ArticleResponseUnavailable(f'incomplete pages {cid} {t_type}')
                        self.article_attempts.add((str(cid), str(t_type)))
                        return current_page

                    raise ArticleResponseUnavailable(
                        f"article response missing or non-200 for {cid} ({t_type})"
                    )
                except ArticleResponseUnavailable as e:
                    logging.warning(
                        "Article response unavailable %s (%s) attempt %s/%s: %s",
                        cid,
                        t_type,
                        attempt,
                        retries + 1,
                        e,
                    )
                    if attempt > retries or self.rate_limit_count or self.blocking_error:
                        self.article_missing_responses.append(
                            {"complex_no": str(cid), "trade_type": str(t_type)}
                        )
                        return current_page

                    await asyncio.sleep(random.uniform(0.5, 1.0))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning(
                        f"Nav error {cid} ({t_type}) attempt {attempt}/{retries + 1}: {e}"
                    )
                    if attempt > retries:
                        return current_page

                    try:
                        if is_transient_navigation_error(e):
                            await wait_for_network_ready()
                        current_page = await recreate_page(current_page)
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                    except Exception as recreate_e:
                        logging.error(
                            f"Failed to recreate page for {cid}: {recreate_e}"
                        )
                        return current_page

            return current_page

        # 5. Visit Details (Trigger article list)
        for cid in filtered_cids:
            for t_type in ["A1", "B1"]:
                if self.rate_limit_count or self.blocking_error:
                    return False
                page = await visit_detail_with_retries(page, cid, t_type)

        article_summary = self._summarize_article_capture(filtered_cids)
        logging.info("Article capture summary for %s: %s", dong_name, article_summary)

        incomplete = [(cid, trade) for cid in filtered_cids for trade in ['A1', 'B1']
                      if not self._has_complete_article_response(cid, trade)]
        if incomplete:
            logging.error('Incomplete article pairs for %s: %s', dong_name, incomplete[:10])
            return False
        for cid in filtered_cids:
            self._validate_empty_pyeong_catalog(cid)
        return True

    def _validate_empty_pyeong_catalog(self, cid):
        """A valid empty catalog is not an API failure, but prove no listings."""
        if self.complexes[cid].get('pyeongs'):
            return
        for trade in ('A1', 'B1'):
            if (not self._has_complete_article_response(cid, trade)
                    or self.article_seen_ids.get((str(cid), trade))):
                raise ArticleResponseUnavailable(
                    f'empty pyeong catalog without proven empty articles: {cid} {trade}')
        logging.info('Empty pyeong catalog with proven zero sale/rent listings: %s', cid)

    async def _visit_detail_page(self, page, url):
        # First page capture is awaited by the caller; remaining pages use the
        # validated cursor API. Do not scroll window: PC listings live in a panel.
        if self.blocking_error or self.rate_limit_count:
            raise ArticleResponseUnavailable(self.blocking_error or 'paused after rate limiting')
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        await self._wait_buffer(0.5, 1.0)

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

            # Preserve one logical listing per representative while retaining
            # broker variants for extrema and floor-specific minimums.
            logical_articles = []
            seen_group_ids = set()
            for item_index, item in enumerate(articles_or_groups):
                if not isinstance(item, dict):
                    continue

                if isinstance(item.get("representativeArticleInfo"), dict):
                    representative = item["representativeArticleInfo"]
                    variants = [representative]
                    duplicated = item.get("duplicatedArticleInfo") or {}
                    if isinstance(duplicated, dict):
                        variants.extend(duplicated.get("articleInfoList") or [])
                    candidates = [(representative, variants)]
                elif isinstance(item.get("articleInfoList"), list):
                    candidates = [(article, [article]) for article in item["articleInfoList"]]
                else:
                    candidates = [(item, [item])]

                for candidate_index, (representative, variants) in enumerate(candidates):
                    if not isinstance(representative, dict):
                        continue
                    article_number = representative.get("articleNumber")
                    group_id = (
                        str(article_number)
                        if article_number not in (None, "")
                        else f"anonymous:{item_index}:{candidate_index}"
                    )
                    if group_id in seen_group_ids:
                        continue
                    seen_group_ids.add(group_id)
                    logical_articles.append(
                        {
                            "representative": representative,
                            "variants": [v for v in variants if isinstance(v, dict)],
                        }
                    )

            if not logical_articles:
                continue

            def numeric_area(value):
                try:
                    return round(float(value), 3)
                except (TypeError, ValueError):
                    return 0.0

            def area_details(article):
                space = article.get("spaceInfo", {})
                if not space and "supplySpace" in article:
                    space = article
                supply = numeric_area(space.get("supplySpace", 0))
                exclusive = numeric_area(space.get("exclusiveSpace", 0))
                supply_name = str(space.get("supplySpaceName") or "").strip()
                exclusive_name = str(space.get("exclusiveSpaceName") or "").strip()
                subtype = str(space.get("nameType") or "").strip()
                if not subtype:
                    suffixes = []
                    for name in (supply_name, exclusive_name):
                        match = re.search(r"([A-Za-z]+[0-9]*)$", name)
                        if match:
                            suffixes.append(match.group(1).upper())
                    if suffixes and len(set(suffixes)) == 1:
                        subtype = suffixes[0]
                numeric_labels = all(not name or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", name)
                                     for name in (supply_name, exclusive_name))
                label_identity = subtype or (
                    (supply_name, exclusive_name)
                    if (supply_name or exclusive_name) and not numeric_labels
                    else None
                )
                return space, (supply, exclusive), label_identity

            named_area_identities = {}
            for entry in logical_articles:
                _, numeric_key, label_identity = area_details(entry["representative"])
                if label_identity is not None:
                    named_area_identities.setdefault(numeric_key, set()).add(label_identity)

            # Group by canonical numeric areas plus named subtype. Missing labels
            # merge only when the numeric area has exactly one named subtype.
            groups = {}
            for entry in logical_articles:
                art = entry["representative"]
                space, numeric_key, label_identity = area_details(art)
                if label_identity is None:
                    candidates = named_area_identities.get(numeric_key, set())
                    if len(candidates) == 1:
                        label_identity = next(iter(candidates))
                ptp_key = (*numeric_key, str(label_identity))

                if ptp_key not in groups:
                    groups[ptp_key] = {"trade": [], "rent": [], "info": art, "label_identity": label_identity}
                elif not (groups[ptp_key]["info"].get("spaceInfo") or {}).get(
                    "supplySpaceName"
                ) and space.get("supplySpaceName"):
                    groups[ptp_key]["info"] = art

                t_type = art.get("tradeType", "")
                price_info = art.get("priceInfo") or {}
                price_value = (
                    price_info.get("dealPrice", art.get("dealPrice"))
                    if t_type in ["A1", "매매"]
                    else price_info.get("warrantyPrice", art.get("warrantyPrice"))
                )
                if processor.positive_price(price_value) is None:
                    continue
                if t_type in ["A1", "매매"]:
                    groups[ptp_key]["trade"].append(entry)
                elif t_type in ["B1", "전세"]:
                    groups[ptp_key]["rent"].append(entry)

            def get_stats(items, is_trade=True):
                if not items:
                    return 0, 0, 0, 0, 0, 0
                representative_prices = []
                variant_prices = []
                seen_variant_ids = set()
                price_field = "dealPrice" if is_trade else "warrantyPrice"
                for entry in items:
                    representative = entry["representative"]
                    rep_info = representative.get("priceInfo") or {}
                    rep_price = processor.positive_price(
                        rep_info.get(price_field, representative.get(price_field))
                    )
                    if rep_price is not None:
                        representative_prices.append(rep_price)

                    for variant_index, variant in enumerate(entry["variants"]):
                        variant_id = variant.get("articleNumber")
                        variant_id = (
                            str(variant_id)
                            if variant_id not in (None, "")
                            else f"anonymous:{id(entry)}:{variant_index}"
                        )
                        if variant_id in seen_variant_ids:
                            continue
                        seen_variant_ids.add(variant_id)
                        price_info = variant.get("priceInfo") or {}
                        price = processor.positive_price(
                            price_info.get(price_field, variant.get(price_field))
                        )
                        if price is not None:
                            variant_prices.append((price, processor.floor_category(variant)))

                if not representative_prices or not variant_prices:
                    return 0, 0, 0, 0, 0, 0
                std = sorted(price for price, floor in variant_prices if floor == "normal")
                spc = sorted(price for price, floor in variant_prices if floor == "low")
                all_prices = sorted(price for price, _ in variant_prices)
                min_std = std[0] if std else 0
                min_spc = spc[0] if spc else 0
                min_total = all_prices[0]
                max_val = all_prices[-1]
                avg = sum(representative_prices) / len(representative_prices)
                return min_std, min_spc, min_total, max_val, avg, len(items)

            for ptp_key, g in groups.items():
                if not g["trade"] and not g["rent"]:
                    continue

                tm_std, tm_spc, tm_min_total, tm_max, tm_avg, tm_cnt = get_stats(
                    g["trade"]
                )
                _, _, rm_min_total, rm_max, rm_avg, rm_cnt = get_stats(
                    g["rent"], is_trade=False
                )
                rm_min = rm_min_total

                base_price = tm_std if tm_std > 0 else tm_spc

                gap = ""
                jeonse_ratio = ""
                if base_price > 0 and rm_min > 0:
                    gap_val = base_price - rm_min
                    gap = gap_val
                    jeonse_ratio = rm_min / base_price * 100

                info = g["info"]
                space = info.get("spaceInfo", {}) or info
                # Keep the grouping subtype in the persisted type when the API
                # supplies only a numeric label (or no label at all).
                type_name = str(space.get("supplySpaceName") or
                                numeric_area(space.get("supplySpace", 0)))
                subtype = g["label_identity"]
                if isinstance(subtype, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", type_name):
                    type_name += subtype

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

                target_space = numeric_area(space.get("supplySpace", 0))

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
                        target_exclusive = numeric_area(space.get("exclusiveSpace", 0))
                        if (
                            abs(p_supply - target_space) < 0.1
                            and abs(p_exclusive - target_exclusive) < 0.1
                        ):
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

                # ✅ Preserve original full path for fallback (before cleaning)
                dong_full_path = complex_info.get("_dong_name", "")

                # Clean dong to leaf token for display
                dong = dong_full_path
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

                # ✅ Fallback: Use ORIGINAL full path (dong_full_path, not cleaned dong)
                if (not sido or not gungu) and dong_full_path and " " in dong_full_path:
                    d_parts = dong_full_path.split()
                    if len(d_parts) >= 3:
                        sido = sido or d_parts[0]
                        gungu = gungu or d_parts[1]
                        # dong already cleaned above
                sido = normalize_business_region(sido, gungu, dong_full_path)
                approval_date = complex_info.get("useApprovalDate", "")

                results.append(
                    {
                        "시/도": sido,
                        "시/군/구": gungu,
                        "읍/면/동": dong,
                        "아파트명": cname,
                        "준공일": approval_date,
                        "총세대수": complex_info.get("totalHouseholdNumber", 0)
                        or household_count_from_list,
                        "타입": type_name,
                        "공급면적": numeric_area(space.get("supplySpace", 0)),
                        "전용면적": numeric_area(space.get("exclusiveSpace", 0)),
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
                        "전세가율": f"{jeonse_ratio:.1f}%"
                        if jeonse_ratio != ""
                        else "",
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
                        "수집일": datetime.now(timezone(timedelta(hours=9))).strftime(
                            "%Y-%m-%d"
                        ),
                        "complex_id": cid,
                    }
                )

        return pd.DataFrame(results)


# Helper Functions
def get_all_leaf_items(node, current_name=""):
    items = []
    if "children" in node and node["children"]:
        for k, v in node["children"].items():
            # ✅ FIX: keep full path
            next_name = f"{current_name} {k}".strip()
            items.extend(get_all_leaf_items(v, next_name))
    elif "url" in node and node.get("has_complexes") is not False:
        items.append((current_name, node["url"]))
    return items


def get_region_urls(region_list):
    json_path = REGION_JSON_PATH
    if not os.path.exists(json_path):
        raise ValueError(f'region file missing: {json_path}')
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError('region JSON could not be read') from exc

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
            raise ValueError(f'Region not found: {root_part}')

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

        if not valid_path:
            raise ValueError(f'Region path unresolved: {query}')
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
    json_path = REGION_JSON_PATH
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if region_name in data:
            return list(data[region_name].get("children", {}).keys())
    except:
        pass
    return []


def _upsert_complex(cur, values):
    cur.execute(
        """
        INSERT INTO complexes (
            complex_no, name, region_depth1, region_depth2, region_depth3,
            total_households, total_dongs, completion_date, construction_company,
            heating_method, heating_fuel, parking_per_household,
            far, bcr, latitude, longitude, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(complex_no) DO UPDATE SET
            name = CASE
                WHEN excluded.name = '' OR excluded.name = 'Complex_' || excluded.complex_no
                THEN COALESCE(NULLIF(complexes.name, ''), excluded.name)
                ELSE excluded.name
            END,
            region_depth1 = COALESCE(NULLIF(excluded.region_depth1, ''), complexes.region_depth1),
            region_depth2 = COALESCE(NULLIF(excluded.region_depth2, ''), complexes.region_depth2),
            region_depth3 = COALESCE(NULLIF(excluded.region_depth3, ''), complexes.region_depth3),
            total_households = COALESCE(NULLIF(excluded.total_households, 0), complexes.total_households),
            total_dongs = COALESCE(NULLIF(excluded.total_dongs, 0), complexes.total_dongs),
            completion_date = COALESCE(NULLIF(excluded.completion_date, ''), complexes.completion_date),
            construction_company = COALESCE(NULLIF(excluded.construction_company, ''), complexes.construction_company),
            heating_method = COALESCE(NULLIF(excluded.heating_method, ''), complexes.heating_method),
            heating_fuel = COALESCE(NULLIF(excluded.heating_fuel, ''), complexes.heating_fuel),
            parking_per_household = COALESCE(NULLIF(excluded.parking_per_household, 0), complexes.parking_per_household),
            far = COALESCE(NULLIF(excluded.far, 0), complexes.far),
            bcr = COALESCE(NULLIF(excluded.bcr, 0), complexes.bcr),
            latitude = COALESCE(NULLIF(excluded.latitude, 0), complexes.latitude),
            longitude = COALESCE(NULLIF(excluded.longitude, 0), complexes.longitude),
            last_updated = excluded.last_updated
        """,
        values,
    )


def init_db():
    """Initialize Database with Normalized Schema"""
    ensure_parent_directory(DB_PATH)
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

    try:
        duplicate = cur.execute(
            """
            SELECT complex_no, date, pyeong_type, supply_area, exclusive_area, COUNT(*)
            FROM prices
            GROUP BY complex_no, date, pyeong_type, supply_area, exclusive_area
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate:
            raise RuntimeError(
                "duplicate canonical price snapshots exist; refusing automatic migration"
            )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_prices_complex_date ON prices (complex_no, date)"
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_prices_canonical_snapshot
            ON prices (complex_no, date, pyeong_type, supply_area, exclusive_area)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_to_db(df, table_name="real_estate"):
    """Save DataFrame to Normalized SQLite Database"""
    if df.empty:
        return 0

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

            _upsert_complex(
                cur,
                (
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
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            count_complex += 1

            for _, row in group.iterrows():
                cur.execute(
                    """
                INSERT INTO prices (
                    complex_no, date, pyeong_type, supply_area, exclusive_area,
                    hallway_type, room_bath,
                    trade_min_std, trade_min_low, trade_max, trade_avg, trade_count,
                    rent_min, rent_max, rent_avg, rent_count,
                    gap, jeonse_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(complex_no, date, pyeong_type, supply_area, exclusive_area)
                DO UPDATE SET
                    hallway_type = excluded.hallway_type,
                    room_bath = excluded.room_bath,
                    trade_min_std = excluded.trade_min_std,
                    trade_min_low = excluded.trade_min_low,
                    trade_max = excluded.trade_max,
                    trade_avg = excluded.trade_avg,
                    trade_count = excluded.trade_count,
                    rent_min = excluded.rent_min,
                    rent_max = excluded.rent_max,
                    rent_avg = excluded.rent_avg,
                    rent_count = excluded.rent_count,
                    gap = excluded.gap,
                    jeonse_ratio = excluded.jeonse_ratio
                """,
                    (
                        int(cid),
                        row["수집일"],
                        row["타입"],
                        round(float(row["공급면적"] or 0), 3),
                        round(float(row["전용면적"] or 0), 3),
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
                        float(str(row.get("전세가율", "0")).replace("%", "") or 0),
                    ),
                )
                count_prices += 1

        conn.commit()
        print(
            f"💾 Database Updated: {count_complex} complexes updated, {count_prices} price records committed."
        )
        return count_prices

    except Exception as e:
        conn.rollback()
        print(f"❌ Database Error: {e}")
        import traceback

        traceback.print_exc()
        raise
    finally:
        conn.close()


def save_complexes_metadata_only(complexes_dict):
    """
    Save complexes metadata into 'complexes' table even if there are no articles/prices.
    ✅ FIX: if address(dict) is missing, fallback parse from _dong_name full path like "서울시 강남구 신사동"
    """
    if not complexes_dict:
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    committed_count = 0

    try:
        for cid, info in complexes_dict.items():
            if not isinstance(info, dict):
                continue

            name = (
                info.get("name", "")
                or info.get("complexName", "")
                or f"Complex_{cid}"
            )

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
            if (
                (not sido or not gungu)
                and dong_path
                and isinstance(dong_path, str)
                and " " in dong_path
            ):
                tokens = dong_path.split()
                if len(tokens) >= 3:
                    sido = sido or tokens[0]
                    gungu = gungu or tokens[1]
                    dong = tokens[-1]

            # Normalize dong to last token if it still has spaces
            if dong and isinstance(dong, str) and " " in dong:
                dong = dong.split()[-1]

            sido = normalize_business_region(sido, gungu, dong_path)
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

            total_households = info.get("totalHouseholdNumber", 0) or info.get(
                "households", 0
            )
            total_dongs = info.get("dongCount", 0) or 0
            completion_date = info.get("useApprovalDate", "") or ""

            construction_company = info.get("constructionCompany", "") or ""

            _upsert_complex(
                cur,
                (
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
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            committed_count += 1

        conn.commit()
        print(
            f"💾 complexes metadata only saved/updated: {committed_count} rows committed."
        )
        return committed_count

    except Exception as e:
        conn.rollback()
        print(f"❌ complexes metadata save error: {e}")
        import traceback

        traceback.print_exc()
        raise
    finally:
        conn.close()


# 분산 처리(Sharding)를 위한 헬퍼 함수
def get_sharded_targets(shard_index, shard_total):
    if shard_total <= 0 or shard_index < 0 or shard_index >= shard_total:
        raise ValueError('invalid shard index/total')
    with open(REGION_JSON_PATH, encoding='utf-8') as f:
        data = json.load(f)
    targets = []
    seen = {}
    for root, node in data.items():
        for name, url in get_all_leaf_items(node, root):
            if url in seen:
                if seen[url] != name:
                    raise ValueError(f'duplicate leaf region URL: {seen[url]} / {name}')
                continue
            seen[url] = name
            targets.append({'name':name,'url':url})
    targets.sort(key=lambda row:row['name'])
    if not targets:
        raise ValueError('no validated leaf targets')
    return targets[shard_index::shard_total]


async def main():
    parser = argparse.ArgumentParser(description='Naver Land Crawler')
    parser.add_argument('--regions', nargs='+')
    parser.add_argument('--db-path', default='real_estate.db')
    parser.add_argument('--shard-index', type=int)
    parser.add_argument('--shard-total', type=int)
    parser.add_argument('--log-path')
    parser.add_argument('--screenshot-dir')
    parser.add_argument('--region-json-path')
    parser.add_argument('--targets-json', help='Explicit [{name,url}] targets for bounded repair/probes')
    parser.add_argument('--skip-existing-db', help='Read-only original DB: collect missing or metadata-only complexes')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--inventory-preflight', action='store_true')
    args = parser.parse_args()
    if (args.shard_index is None) != (args.shard_total is None):
        parser.error('--shard-index and --shard-total must be supplied together')
    if args.targets_json and (args.regions or args.shard_index is not None):
        parser.error('--targets-json cannot be combined with region/shard selection')
    global DB_PATH, CRAWLER_LOG_PATH, SCREENSHOT_DIR, REGION_JSON_PATH
    DB_PATH = str(Path(args.db_path).expanduser())
    if args.log_path:
        CRAWLER_LOG_PATH = str(Path(args.log_path).expanduser())
    if args.screenshot_dir:
        SCREENSHOT_DIR = str(Path(args.screenshot_dir).expanduser())
    if args.region_json_path:
        REGION_JSON_PATH = str(Path(args.region_json_path).expanduser())
    configure_logging(CRAWLER_LOG_PATH)
    if args.inventory_preflight:
        targets = get_sharded_targets(0, 1)[:1]
        crawler = NaverLandPlaywright(screenshot_dir=SCREENSHOT_DIR)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed and os.getenv('CRAWLER_HEADLESS', '1') != '0', args=['--disable-blink-features=AutomationControlled'])
            try:
                context = await browser.new_context(**crawler.get_context_options())
                page = await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                rows = await crawler._load_region_complexes(page, targets[0]['url'])
                logging.info('PREFLIGHT PASS %s: %s complexes', targets[0]['name'], len(rows))
            finally:
                await browser.close()
        return
    if args.targets_json:
        targets = json.loads(Path(args.targets_json).read_text())
        if not isinstance(targets, list) or any(not isinstance(t,dict) or not t.get('name') or not t.get('url') for t in targets):
            parser.error('invalid targets JSON')
    elif args.shard_index is not None:
        targets = get_sharded_targets(args.shard_index, args.shard_total)
    else:
        pairs = get_region_urls(args.regions or TARGET_REGIONS)
        targets = [{'name':n,'url':u} for n,u in pairs]
    if not targets:
        parser.error('requested selection produced no targets')
    if len({t['url'] for t in targets}) != len(targets):
        parser.error('duplicate target URLs')
    crawler = NaverLandPlaywright(screenshot_dir=SCREENSHOT_DIR)
    if args.skip_existing_db:
        source = Path(args.skip_existing_db).resolve()
        if source == Path(DB_PATH).resolve():
            parser.error('backfill output must not overwrite source DB')
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as conn:
            crawler.skip_complex_ids = {str(r[0]) for r in conn.execute('select distinct complex_no from prices')}
    init_db()
    ok = await crawler.run_test([(t['name'],t['url']) for t in targets],headless=not args.headed and os.getenv('CRAWLER_HEADLESS', '1') != '0')
    if not ok:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
