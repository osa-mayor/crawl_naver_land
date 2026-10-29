import unittest
from playwright.async_api import async_playwright
from region_navigation import RegionNavigationError, navigate_region


class RegionNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch()
        self.page = await self.browser.new_page()
        self.parent = 'https://fin.land.naver.com/regions?si=27&gun=2771'
        self.old = self.parent + '&eup=old'
        self.new = self.parent + '&eup=new'

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def route(self, links):
        async def handler(route):
            if route.request.url == self.old:
                await route.fulfill(content_type='text/html; charset=utf-8',
                    body=f'<script>history.replaceState(null,"","{self.parent}")</script>{links}')
            elif route.request.url == self.parent:
                await route.fulfill(content_type='text/html; charset=utf-8', body=links)
            else:
                await route.fulfill(content_type='text/html', body='총 0개 단지')
        await self.page.route('**/*', handler)

    async def test_verified_same_parent_town_promotion(self):
        await self.route(f'<a href="{self.new}">구지읍</a>')
        result = await navigate_region(self.page, self.old, '구지면')
        self.assertEqual(result, {'url': self.new, 'name': '구지읍'})

    async def test_ambiguous_or_unrelated_change_is_not_empty(self):
        for links in [f'<a href="{self.new}">다른읍</a>',
                      f'<a href="{self.new}">구지읍</a><a href="{self.parent}&eup=other">구지면</a>',
                      '<a href="https://evil.test/regions?si=27&gun=2771&eup=new">구지읍</a>',
                      '<a href="/regions?si=27&gun=other&eup=new">구지읍</a>']:
            await self.route(links)
            with self.assertRaises(RegionNavigationError):
                await navigate_region(self.page, self.old, '구지면')
            await self.page.unroute('**/*')

    async def test_unexpected_redirect_still_fails_closed(self):
        async def handler(route):
            if route.request.url == self.old:
                await route.fulfill(content_type='text/html', body='<script>history.replaceState(null,"","/404.html")</script>')
            else:
                await route.fulfill(body='error')
        await self.page.route('**/*', handler)
        with self.assertRaises(RegionNavigationError):
            await navigate_region(self.page, self.old, '구지면')

    async def test_resolution_cannot_redirect_again(self):
        await self.route(f'<a href="{self.new}">구지읍</a>')
        await self.page.route('**eup=new', lambda r: r.fulfill(content_type='text/html',
            body=f'<script>history.replaceState(null,"","{self.parent}")</script>'))
        with self.assertRaises(RegionNavigationError):
            await navigate_region(self.page, self.old, '구지면')

    def test_no_reverse_promotion_or_same_name_recoding(self):
        from region_navigation import _replacement
        for old,new in [('구지읍','구지면'),('구지면','구지면'),('구지읍','구지읍')]:
            self.assertIsNone(_replacement(self.old,self.parent,old,[{'url':self.new,'name':new}]))

    async def test_conflicting_total_labels_cannot_prove_empty(self):
        from region_navigation import read_region_total
        for first,second in [(0,3),(3,0)]:
            await self.page.set_content(f'<p>총 {first}개 단지</p><p>총 {second}개 단지</p>')
            with self.assertRaises(RegionNavigationError):
                await read_region_total(self.page,timeout=100)


if __name__ == '__main__':
    unittest.main()
