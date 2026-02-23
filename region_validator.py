import asyncio
import json
import logging
import os
from typing import Any

from playwright.async_api import async_playwright


REGION_FILE = os.getenv("REGION_FILE", "naver_region_codes.json")
MAX_CONCURRENT_TABS = int(os.getenv("MAX_CONCURRENT_TABS", "5"))
TIMEOUT_MS = int(os.getenv("REGION_VALIDATOR_TIMEOUT_MS", "10000"))

SELECTOR_NO_RESULT = "div[class*='no_result'], div[class*='no_data']"
COMPLEX_SELECTORS = [
    "li[class*='ComplexItem'][class*='article']",
    "div[class*='list_complex']",
    "button[class*='complex_link']",
    "li[class*='ComplexItem']",
]


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def load_region_data(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_region_data(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def collect_leaf_tasks(data: dict[str, Any]) -> list[tuple[list[str], dict[str, Any]]]:
    tasks: list[tuple[list[str], dict[str, Any]]] = []

    def walk(node: dict[str, Any], path: list[str]) -> None:
        children = node.get("children")
        if isinstance(children, dict) and children:
            for key, child in children.items():
                walk(child, path + [key])
            return

        if "url" in node:
            tasks.append((path, node))

    for key, value in data.items():
        walk(value, [key])

    return tasks


async def check_url(context, url: str) -> bool:
    page = await context.new_page()
    await page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """
    )

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=max(TIMEOUT_MS, 30000))

        try:
            async with page.expect_response(
                lambda response: "complex" in response.url and response.status == 200,
                timeout=min(TIMEOUT_MS, 10000),
            ):
                pass
        except Exception:
            pass

        if await page.query_selector(SELECTOR_NO_RESULT):
            return False

        await page.wait_for_timeout(min(TIMEOUT_MS, 5000))

        for selector in COMPLEX_SELECTORS:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        return False
    except Exception as exc:
        logging.debug("URL check failed: %s (%s)", url, exc)
        return False
    finally:
        await page.close()


async def validate_regions(data: dict[str, Any]) -> dict[tuple[str, ...], bool]:
    all_tasks = collect_leaf_tasks(data)
    total = len(all_tasks)

    if total == 0:
        return {}

    logging.info("Validating %s leaf regions", total)
    results: dict[tuple[str, ...], bool] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            is_mobile=False,
            has_touch=False,
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        )

        chunk_size = max(1, MAX_CONCURRENT_TABS)

        for i in range(0, total, chunk_size):
            chunk = all_tasks[i : i + chunk_size]
            checks = [check_url(context, node["url"]) for _, node in chunk]
            flags = await asyncio.gather(*checks)

            for (path, _node), flag in zip(chunk, flags):
                results[tuple(path)] = flag

            processed = min(i + chunk_size, total)
            logging.info("Progress: %s/%s", processed, total)

        await browser.close()

    return results


def apply_updates(data: dict[str, Any], results: dict[tuple[str, ...], bool]) -> None:
    def walk(node: dict[str, Any], current_path: list[str]) -> bool:
        children = node.get("children")
        if isinstance(children, dict) and children:
            any_child_valid = False
            for key, child in children.items():
                if walk(child, current_path + [key]):
                    any_child_valid = True

            node["has_complexes"] = any_child_valid
            return any_child_valid

        flag = results.get(tuple(current_path), False)
        node["has_complexes"] = flag
        return flag

    for key, value in data.items():
        walk(value, [key])


async def main() -> None:
    if not os.path.exists(REGION_FILE):
        raise FileNotFoundError(f"Region file not found: {REGION_FILE}")

    data = load_region_data(REGION_FILE)
    logging.info("Starting region validation")

    results = await validate_regions(data)
    apply_updates(data, results)
    save_region_data(REGION_FILE, data)

    true_count = sum(1 for flag in results.values() if flag)
    logging.info("Validation complete. Regions with complexes: %s", true_count)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.exception("Region validation failed: %s", exc)
        raise
