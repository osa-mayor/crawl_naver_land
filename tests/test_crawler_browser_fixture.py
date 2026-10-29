import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from playwright.async_api import async_playwright
from crawler import NaverLandPlaywright, ArticleResponseUnavailable

class ListBrowserTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  self.pw=await async_playwright().start();self.browser=await self.pw.chromium.launch()
  self.page=await self.browser.new_page();self.c=NaverLandPlaywright(self.tmp.name)
 async def asyncTearDown(self):
  await self.browser.close();await self.pw.stop();self.tmp.cleanup()
 async def load(self,html):
  await self.page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html))
  return await self.c._load_region_complexes(self.page,'https://fixture.test/regions')
 async def test_link_pagination_collects_all_before_filter(self):
  html='''<strong>총 3개 단지</strong><ul id="cards"></ul>
  <a class="button-more" href="?page=1" onclick="event.preventDefault();add(2);add(3);this.remove()">더보기</a>
  <script>function add(n){cards.insertAdjacentHTML('beforeend',`<li><span class="TitleBadge">아파트</span><strong class="name">단지${n}</strong><p>999세대</p><a href="/complexes/${n}">단지 보러가기</a></li>`)}add(1)</script>'''
  rows=await self.load(html);self.assertEqual([r['id'] for r in rows],['1','2','3'])
 async def test_button_pagination(self):
  html='''<strong>총 2개 단지</strong><ul id="cards"><li><span class="TitleBadge">아파트</span><strong class="name">A</strong><p>50세대</p><a href="/complexes/1">단지 보러가기</a></li></ul>
  <button class="button-more" onclick="cards.insertAdjacentHTML('beforeend',cards.innerHTML.replace('/1','/2'));this.remove()">더보기</button>'''
  self.assertEqual(len(await self.load(html)),2)
 async def test_proven_empty(self):
  self.assertEqual(await self.load('<strong>총 0개 단지</strong>'),[])
 async def test_explicit_empty_component_without_total(self):
  self.assertEqual(await self.load('<p class="ComplexListByRegion-module_nodata">단지가 없습니다.</p>'),[])
 async def test_direct_retry_drops_old_resolution(self):
  self.c.region_resolutions['https://fixture.test/regions']={'url':'https://fixture.test/new','name':'구지읍'}
  await self.load('<strong>총 0개 단지</strong>')
  self.assertNotIn('https://fixture.test/regions',self.c.region_resolutions)
 async def test_missing_badge_with_name_first_line_is_failure_not_empty(self):
  html='<strong>총 1개 단지</strong><li><strong class="name">청운현대</strong><p>999세대</p><a href="/complexes/1">단지 보러가기</a></li>'
  with self.assertRaisesRegex(ArticleResponseUnavailable, 'category'):
   await self.load(html)
 async def test_unrecognized_badge_is_failure_not_silent_filter(self):
  html='<strong>총 1개 단지</strong><li><span class="TitleBadge">VR</span><strong class="name">청운현대</strong><p>999세대</p><a href="/complexes/1">단지 보러가기</a></li>'
  with self.assertRaisesRegex(ArticleResponseUnavailable, 'category'):
   await self.load(html)
 async def test_initial_cards_wait_for_loading_without_fixed_buffer(self):
  html='''<strong>총 1개 단지</strong><ul id="cards"></ul>
  <script>setTimeout(() => {cards.innerHTML='<li><span class="TitleBadge">아파트</span><strong class="name">A</strong><p>999세대</p><a href="/complexes/1">단지 보러가기</a></li>';},150)</script>'''
  with patch.object(self.c, '_wait_buffer', new_callable=AsyncMock) as buffer:
   rows=await self.load(html)
  self.assertEqual([r['id'] for r in rows], ['1'])
  buffer.assert_not_awaited()
 async def test_more_waits_for_delayed_new_identity_without_fixed_buffer(self):
  html='''<strong>총 2개 단지</strong><ul id="cards"><li><span class="TitleBadge">아파트</span><strong class="name">A</strong><p>999세대</p><a href="/complexes/1">단지 보러가기</a></li></ul>
  <button class="button-more" onclick="this.remove();setTimeout(() => cards.insertAdjacentHTML('beforeend',cards.innerHTML.replace('/1','/2')),150)">더보기</button>'''
  with patch.object(self.c, '_wait_buffer', new_callable=AsyncMock) as buffer:
   rows=await self.load(html)
  self.assertEqual([r['id'] for r in rows], ['1', '2'])
  buffer.assert_not_awaited()
if __name__=='__main__':unittest.main()
