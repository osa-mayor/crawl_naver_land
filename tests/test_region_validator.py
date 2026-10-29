import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import region_validator


class RegionValidatorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.context = await self.browser.new_context()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def _check_html(self, html: str) -> bool:
        async def navigate(page, url, region_name=None):
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto(url)
            return {"url": page.url, "name": region_name}

        with (
            patch.object(region_validator, "navigate_region", side_effect=navigate),
            patch.object(region_validator, "TIMEOUT_MS", 300),
        ):
            return await region_validator.check_url(
                self.context, "https://fixture.test/regions", "테스트동"
            )

    async def test_zero_total_is_proven_empty(self):
        self.assertFalse(await self._check_html("<strong>총 0개 단지</strong>"))

    async def test_exact_complex_list_nodata_is_proven_empty(self):
        html = (
            '<p class="ComplexListByRegion-module-scss-module__0Xvz6a__nodata">'
            "단지가 없습니다.</p>"
        )
        self.assertFalse(await self._check_html(html))

    async def test_nonzero_total_requires_loaded_named_complex_link(self):
        html = """
        <strong>총 1개 단지</strong>
        <li><strong class="name">테스트아파트</strong>
          <a href="/complexes/123">단지 보러가기</a></li>
        """
        self.assertTrue(await self._check_html(html))

    async def test_unproven_dom_raises_instead_of_marking_empty(self):
        with self.assertRaises(region_validator.RegionNavigationError):
            await self._check_html("<main>로딩 중</main>")

    async def test_arbitrary_error_or_nodata_text_remains_unproven(self):
        fixtures = (
            '<p class="nodata">단지가 없습니다.</p>',
            '<p class="ComplexListByRegion-error__nodata">오류가 발생했습니다.</p>',
        )
        for html in fixtures:
            with self.subTest(html=html):
                with self.assertRaises(region_validator.RegionNavigationError):
                    await self._check_html(html)

    async def test_naver_429_response_raises_even_after_http_200_navigation(self):
        async def navigate(page, url, region_name=None):
            async def route(request):
                if request.request.url.startswith("https://fin.land.naver.com/api/"):
                    await request.fulfill(status=429, body="rate limited")
                else:
                    await request.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=(
                            "<strong>총 0개 단지</strong>"
                            "<script>fetch('https://fin.land.naver.com/api/regions')</script>"
                        ),
                    )

            await page.route("**/*", route)
            await page.goto(url)
            await page.wait_for_timeout(50)
            return {"url": page.url, "name": region_name}

        with patch.object(region_validator, "navigate_region", side_effect=navigate):
            with self.assertRaisesRegex(region_validator.RegionNavigationError, "429"):
                await region_validator.check_url(
                    self.context, "https://fixture.test/regions", "테스트동"
                )


class FakePage:
    def __init__(self):
        self.listeners = {}
        self.closed = False

    async def add_init_script(self, _script):
        pass

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        if self.listeners.get(event) is callback:
            del self.listeners[event]

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.page = FakePage()

    async def new_page(self):
        return self.page


class RegionValidatorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_error_propagates_and_watcher_is_cleaned_up(self):
        context = FakeContext()
        error = region_validator.RegionNavigationError("redirect unresolved")
        with patch.object(region_validator, "navigate_region", side_effect=error):
            with self.assertRaisesRegex(region_validator.RegionNavigationError, "redirect"):
                await region_validator.check_url(context, "https://example.test", "동")
        self.assertTrue(context.page.closed)
        self.assertNotIn("response", context.page.listeners)

    async def test_failed_validation_does_not_apply_or_save(self):
        with (
            patch.object(region_validator, "load_region_data", return_value={"동": {"url": "u"}}),
            patch.object(
                region_validator,
                "validate_regions",
                new=AsyncMock(side_effect=region_validator.RegionNavigationError("unproven")),
            ),
            patch.object(region_validator, "apply_updates") as apply_updates,
            patch.object(region_validator, "save_region_data") as save_region_data,
            patch.object(region_validator.os.path, "exists", return_value=True),
        ):
            with self.assertRaises(region_validator.RegionNavigationError):
                await region_validator.main()
        apply_updates.assert_not_called()
        save_region_data.assert_not_called()

    async def test_failed_chunk_cancels_and_awaits_sibling_before_browser_close(self):
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        async def check(_context, url, _region_name):
            if url == "bad":
                await sibling_started.wait()
                raise region_validator.RegionNavigationError("failed leaf")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        class Browser:
            def __init__(self):
                self.closed_after_cancellation = False

            async def new_context(self, **_kwargs):
                return object()

            async def close(self):
                self.closed_after_cancellation = sibling_cancelled.is_set()

        class PlaywrightManager:
            async def __aenter__(self):
                return SimpleNamespace(
                    chromium=SimpleNamespace(
                        launch=AsyncMock(return_value=browser)
                    )
                )

            async def __aexit__(self, *_args):
                pass

        browser = Browser()
        data = {
            "시": {
                "children": {
                    "실패동": {"url": "bad"},
                    "대기동": {"url": "slow"},
                }
            }
        }
        with (
            patch.object(region_validator, "async_playwright", return_value=PlaywrightManager()),
            patch.object(region_validator, "check_url", side_effect=check),
            patch.object(region_validator, "MAX_CONCURRENT_TABS", 2),
        ):
            with self.assertRaisesRegex(region_validator.RegionNavigationError, "failed leaf"):
                await region_validator.validate_regions(data)

        self.assertTrue(sibling_cancelled.is_set())
        self.assertTrue(browser.closed_after_cancellation)


class RegionValidatorDataTests(unittest.TestCase):
    def test_apply_updates_requires_every_leaf_result_before_mutation(self):
        data = {
            "시": {
                "has_complexes": True,
                "children": {
                    "가동": {"url": "a", "has_complexes": True},
                    "나동": {"url": "b", "has_complexes": True},
                },
            }
        }
        original = json.loads(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "missing"):
            region_validator.apply_updates(data, {("시", "가동"): False})
        self.assertEqual(data, original)

    def test_atomic_save_preserves_original_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regions.json"
            original = {"old": True}
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch.object(os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    region_validator.save_region_data(str(path), {"new": True})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["regions.json"])


if __name__ == "__main__":
    unittest.main()
