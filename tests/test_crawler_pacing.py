import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import crawler

class PerPageBufferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.c=crawler.NaverLandPlaywright(self.tmp.name)
    async def test_buffers_on_two_pages_can_overlap(self):
        entered=0;both=asyncio.Event()
        async def sleep(_):
            nonlocal entered
            entered+=1
            if entered==2:both.set()
            await asyncio.wait_for(both.wait(),1)
        with patch('crawler.asyncio.sleep',side_effect=sleep):
            await asyncio.gather(self.c._wait_buffer(.5,1.5),self.c._wait_buffer(.5,1.5))
        self.assertEqual(entered,2)
    async def test_range_is_preserved(self):
        with patch('crawler.random.uniform',return_value=2.5) as rand,patch('crawler.asyncio.sleep',new_callable=AsyncMock) as sleep:
            await self.c._wait_buffer(2,3)
        rand.assert_called_once_with(2,3);sleep.assert_awaited_once_with(2.5)
    async def test_throttling_during_buffer_blocks_next_action(self):
        async def sleep(_):self.c.rate_limit_count=1
        with patch('crawler.asyncio.sleep',side_effect=sleep),self.assertRaises(crawler.ArticleResponseUnavailable):
            await self.c._wait_buffer(.5,1)
    async def test_preexisting_block_does_not_wait(self):
        self.c.blocking_error='blocked'
        with patch('crawler.asyncio.sleep',new_callable=AsyncMock) as sleep,self.assertRaisesRegex(crawler.ArticleResponseUnavailable,'blocked'):
            await self.c._wait_buffer(.5,1)
        sleep.assert_not_awaited()
    async def test_detail_navigation_keeps_original_buffer(self):
        page=SimpleNamespace(goto=AsyncMock())
        with patch.object(self.c,'_wait_buffer',new_callable=AsyncMock) as delay:
            await self.c._visit_detail_page(page,'https://fixture.test/complexes/1')
        delay.assert_awaited_once_with(.5,1.0)

if __name__=='__main__':unittest.main()
