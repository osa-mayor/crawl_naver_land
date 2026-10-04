import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playwright.async_api import async_playwright
import crawler
from local.config import load_config
from local.orchestrator import build_base_env

class BrowserConfigTests(unittest.TestCase):
    def test_configured_mode_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json'
            p.write_text(json.dumps({'crawl':{'headless':False}}))
            c=load_config(p)
            self.assertFalse(c.crawl.headless)
            env=build_base_env(c,{'screenshots':Path(d)})
            self.assertEqual(env['CRAWLER_HEADLESS'],'0')
    def test_invalid_mode_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json'; p.write_text('{"crawl":{"headless":"false"}}')
            with self.assertRaises(ValueError):load_config(p)

class RegionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=await async_playwright().start()
        self.b=await self.p.chromium.launch();self.page=await self.b.new_page()
        self.c=crawler.NaverLandPlaywright(self.tmp.name)
    async def asyncTearDown(self):
        await self.b.close();await self.p.stop();self.tmp.cleanup()
    async def test_region_api_429_aborts_without_timeout(self):
        async def route(r):
            if '/front-api/' in r.request.url:
                await r.fulfill(status=429,body='rate limited')
            else:
                await r.fulfill(content_type='text/html',body="<script>fetch('/front-api/v1/legalDivision/infoListByLevel')</script>")
        await self.page.route('**/*',route)
        import asyncio
        with self.assertRaisesRegex(crawler.ArticleResponseUnavailable,'429'):
            await asyncio.wait_for(self.c._load_region_complexes(self.page,'https://fin.land.naver.com/regions?si=1'),5)
        self.assertTrue(self.c.rate_limit_count)
        self.assertIn('429',self.c.blocking_error)
    async def test_error_redirect_aborts(self):
        async def route(r):
            if '/regions' in r.request.url:
                await r.fulfill(status=302,headers={'location':'https://financial.pstatic.net/404.html'})
            else:await r.fulfill(content_type='text/html',body='페이지를 찾을 수 없습니다')
        await self.page.route('**/*',route)
        with self.assertRaisesRegex(crawler.ArticleResponseUnavailable,'redirect'):
            await self.c._load_region_complexes(self.page,'https://fin.land.naver.com/regions?si=1')
        self.assertTrue(self.c.blocking_error)


class SchedulingFailureTests(unittest.TestCase):
    def test_blocking_failure_does_not_start_next_shard(self):
        from local import orchestrator as o
        config=SimpleNamespace(crawl=SimpleNamespace(shard_groups=['0-2'],shard_total=3,max_local_parallel=1))
        with tempfile.TemporaryDirectory() as d:
            with patch.object(o,'checkpoint_shard_summary'),patch.object(o,'archive_incomplete_shard_db'),patch.object(o,'run_shard',side_effect=o.PipelineError('HTTP 429')) as run:
                with self.assertRaises(o.PipelineError):
                    o.run_all_shards(config,{'shards':Path(d)}, {})
                self.assertEqual(run.call_count,1)
    def test_blocked_result_does_not_retry(self):
        from local import orchestrator as o
        from tests.test_orchestrator_integrity import shard_config
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'shards').mkdir();(root/'logs').mkdir()
            paths={'shards':root/'shards','logs':root/'logs','screenshots':root/'screenshots'}
            def failed(*a,**kw):
                (root/'shards/db_shard_0.db.result.json').write_text(json.dumps({'blocking_error':'HTTP 429','rate_limit_count':1}))
                return SimpleNamespace(returncode=1)
            with patch.object(o,'wait_for_network_ready',return_value=True),patch.object(o,'run_command',side_effect=failed) as run:
                with self.assertRaisesRegex(o.PipelineError,'no immediate retry'):
                    o.run_shard(shard_config(root,retry=2),paths,0)
                self.assertEqual(run.call_count,1)

class LegacyBrowserIdentityTests(unittest.TestCase):
    def test_preserves_pre_repair_user_agent(self):
        with tempfile.TemporaryDirectory() as d:
            options=crawler.NaverLandPlaywright(d).get_context_options()
            self.assertEqual(options['user_agent'], 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

if __name__=='__main__':unittest.main()
