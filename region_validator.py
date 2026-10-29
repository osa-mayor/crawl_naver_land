import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from region_navigation import RegionNavigationError, navigate_region, read_region_total


REGION_FILE = os.getenv("REGION_FILE", "naver_region_codes.json")
MAX_CONCURRENT_TABS = int(os.getenv("MAX_CONCURRENT_TABS", "5"))
TIMEOUT_MS = int(os.getenv("REGION_VALIDATOR_TIMEOUT_MS", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def load_region_data(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_region_data(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


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


async def _read_proven_region_status(page) -> bool:
    return await read_region_total(page, timeout=TIMEOUT_MS) > 0


async def check_url(context, url: str, region_name: Optional[str] = None) -> bool:
    page = await context.new_page()
    await page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """
    )

    rate_limited = asyncio.Event()
    rate_limit_url: list[str] = []

    def observe_response(response) -> None:
        hostname = urlparse(response.url).hostname or ""
        if response.status == 429 and (
            hostname == "naver.com" or hostname.endswith(".naver.com")
        ):
            rate_limit_url.append(response.url)
            rate_limited.set()

    page.on("response", observe_response)

    async def navigate_and_check() -> bool:
        resolved = await navigate_region(page, url, region_name)
        if (
            not isinstance(resolved, dict)
            or not resolved.get("url")
            or "name" not in resolved
        ):
            raise RegionNavigationError("region navigation did not return a resolved URL and name")
        return await _read_proven_region_status(page)

    check_task = asyncio.create_task(navigate_and_check())
    rate_limit_task = asyncio.create_task(rate_limited.wait())
    try:
        await asyncio.wait(
            (check_task, rate_limit_task), return_when=asyncio.FIRST_COMPLETED
        )
        if rate_limited.is_set():
            raise RegionNavigationError(f"HTTP 429: {rate_limit_url[-1]}")
        result = await check_task
        if rate_limited.is_set():
            raise RegionNavigationError(f"HTTP 429: {rate_limit_url[-1]}")
        return result
    finally:
        page.remove_listener("response", observe_response)
        for task in (check_task, rate_limit_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(check_task, rate_limit_task, return_exceptions=True)
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

        try:
            for i in range(0, total, chunk_size):
                chunk = all_tasks[i : i + chunk_size]
                check_tasks = [
                    asyncio.create_task(check_url(context, node["url"], path[-1]))
                    for path, node in chunk
                ]
                try:
                    flags = await asyncio.gather(*check_tasks)
                except BaseException:
                    for task in check_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*check_tasks, return_exceptions=True)
                    raise

                for (path, _node), flag in zip(chunk, flags):
                    results[tuple(path)] = flag

                processed = min(i + chunk_size, total)
                logging.info("Progress: %s/%s", processed, total)
        finally:
            await browser.close()

    return results


def apply_updates(data: dict[str, Any], results: dict[tuple[str, ...], bool]) -> None:
    required_paths = {tuple(path) for path, _node in collect_leaf_tasks(data)}
    missing_paths = required_paths.difference(results)
    if missing_paths:
        formatted = ", ".join(" > ".join(path) for path in sorted(missing_paths))
        raise ValueError(f"missing validation results for leaf regions: {formatted}")

    invalid_paths = [
        path for path in required_paths if not isinstance(results[path], bool)
    ]
    if invalid_paths:
        formatted = ", ".join(" > ".join(path) for path in sorted(invalid_paths))
        raise ValueError(f"invalid validation results for leaf regions: {formatted}")

    def walk(node: dict[str, Any], current_path: list[str]) -> bool:
        children = node.get("children")
        if isinstance(children, dict) and children:
            any_child_valid = False
            for key, child in children.items():
                if walk(child, current_path + [key]):
                    any_child_valid = True

            node["has_complexes"] = any_child_valid
            return any_child_valid

        path = tuple(current_path)
        if "url" not in node:
            return node.get("has_complexes") is True

        flag = results[path]
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
