import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from crawler import NaverLandPlaywright, ArticleResponseUnavailable

class Response:
    url='https://fin.land.naver.com/front-api/v1/complex/article/list'
    request=SimpleNamespace(post_data_json={'complexNumber':1,'tradeTypes':['A1']},post_data='{"complexNumber":1,"tradeTypes":["A1"]}')
    def __init__(self,payload,status=200,gate=None):self.payload,self.status,self.gate=payload,status,gate
    async def json(self):
        if self.gate:await self.gate.wait()
        if isinstance(self.payload,Exception):raise self.payload
        return self.payload
    async def dispose(self):pass

def payload(next_page=False):
    return {'isSuccess':True,'result':{'list':[{'representativeArticleInfo':{'articleNumber':'a','tradeType':'A1'}}], 'hasNextPage':next_page,'totalCount':2 if next_page else 1,'lastInfo':['cursor'],'seed':'seed'}}

class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.c=NaverLandPlaywright(self.tmp.name)
    async def test_concurrent_consumers_wait_for_same_parse(self):
        gate=asyncio.Event();r=Response(payload(),gate=gate)
        first=asyncio.create_task(self.c._record_article_response(r));await asyncio.sleep(0)
        second=asyncio.create_task(self.c._record_article_response(r));await asyncio.sleep(0)
        self.assertFalse(second.done());gate.set();await asyncio.gather(first,second)
        self.assertEqual(self.c.article_response_stats[('1','A1')]['parsed_200_count'],1)
    async def test_failed_parse_can_retry_identical_request(self):
        await self.c._record_article_response(Response(ValueError('parse')))
        await self.c._record_article_response(Response(payload()))
        self.assertTrue(self.c._has_acceptable_article_response('1','A1'))
    async def test_error_envelope_not_empty_success(self):
        await self.c._record_article_response(Response({'isSuccess':False,'message':'error'}))
        self.assertFalse(self.c._has_acceptable_article_response('1','A1'))
    async def test_missing_list_not_empty_success(self):
        await self.c._record_article_response(Response({'result':{}}))
        self.assertFalse(self.c._has_acceptable_article_response('1','A1'))
    async def test_next_page_http_failure_raises(self):
        await self.c._record_article_response(Response(payload(True)))
        async def post(*a,**kw):return Response({},503)
        with self.assertRaises(ArticleResponseUnavailable):
            await self.c._fetch_remaining_article_pages(SimpleNamespace(request=SimpleNamespace(post=post)),'1','A1')
    async def test_cap_with_more_data_raises(self):
        await self.c._record_article_response(Response(payload(True)))
        with patch('crawler.MAX_ARTICLE_PAGES_PER_TRADE',1):
            with self.assertRaises(ArticleResponseUnavailable):
                await self.c._fetch_remaining_article_pages(SimpleNamespace(),'1','A1')
    async def test_other_region_rate_limit_stops_pagination_before_request(self):
        await self.c._record_article_response(Response(payload(True)))
        self.c.rate_limit_count = 1
        with self.assertRaisesRegex(ArticleResponseUnavailable, 'rate limiting'):
            await self.c._fetch_remaining_article_pages(SimpleNamespace(), '1', 'A1')
    async def test_partial_capture_is_not_complete(self):
        await self.c._record_article_response(Response(payload(True)))
        self.assertFalse(self.c._has_complete_article_response('1','A1'))
    async def test_complete_response_passes(self):
        await self.c._record_article_response(Response(payload()))
        self.assertTrue(self.c._has_complete_article_response('1','A1'))
    async def test_empty_requires_valid_terminal_response(self):
        await self.c._record_article_response(Response({'isSuccess':True,'result':{'list':[],'hasNextPage':False,'totalCount':0,'lastInfo':[]}}))
        self.assertTrue(self.c._has_complete_article_response('1','A1'))
    async def test_count_mismatch_not_complete(self):
        p=payload();p['result']['totalCount']=3
        await self.c._record_article_response(Response(p))
        self.assertFalse(self.c._has_complete_article_response('1','A1'))

    async def test_new_response_with_identical_request_is_not_cached(self):
        p=payload();p['result']['totalCount']=3
        await self.c._record_article_response(Response(p))
        await self.c._record_article_response(Response(payload()))
        self.assertTrue(self.c._has_complete_article_response('1','A1'))

    async def test_retry_replaces_only_failed_trade_not_union(self):
        await self.c._record_article_response(Response(payload()))
        rent=payload();rent['result']['list'][0]['representativeArticleInfo'].update(articleNumber='rent',tradeType='B1')
        await self.c._record_article_response(Response(rent),{'complexNumber':1,'tradeTypes':['B1']})
        self.c._reset_article_pair('1','A1')
        fresh=payload();fresh['result']['list'][0]['representativeArticleInfo']['articleNumber']='fresh'
        await self.c._record_article_response(Response(fresh))
        self.assertEqual(self.c.article_seen_ids[('1','A1')],{'fresh'})
        self.assertEqual(self.c.article_seen_ids[('1','B1')],{'rent'})
        ids={a['representativeArticleInfo']['articleNumber'] for a in self.c.captured_articles['1']}
        self.assertEqual(ids,{'fresh','rent'})
        self.assertTrue(self.c._has_complete_article_response('1','A1'))

    async def test_reset_does_not_count_empty_complex_as_having_articles(self):
        self.c._reset_article_pair('1','A1')
        self.c._reset_article_pair('1','B1')
        empty={'isSuccess':True,'result':{'list':[],'hasNextPage':False,'totalCount':0}}
        for trade in ('A1','B1'):
            await self.c._record_article_response(Response(empty),{'complexNumber':1,'tradeTypes':[trade]})
        summary=self.c._summarize_article_capture(['1'])
        self.assertEqual(summary['complexes_with_articles'],0)
        self.assertEqual(summary['empty_confirmed_complexes'],1)

    async def test_paginated_retry_can_recover_missing_unique_article(self):
        first=payload(True)
        terminal=payload();terminal['result']['totalCount']=2
        await self.c._record_article_response(Response(first))
        await self.c._record_article_response(Response(terminal),{'complexNumber':1,'tradeTypes':['A1'],'lastInfo':['cursor']})
        self.assertFalse(self.c._has_complete_article_response('1','A1'))
        self.c._reset_article_pair('1','A1')
        await self.c._record_article_response(Response(first))
        terminal['result']['list'][0]['representativeArticleInfo']['articleNumber']='b'
        await self.c._record_article_response(Response(terminal),{'complexNumber':1,'tradeTypes':['A1'],'lastInfo':['cursor']})
        self.assertTrue(self.c._has_complete_article_response('1','A1'))

    async def test_region_collection_retries_failed_pair_with_fresh_snapshot(self):
        from unittest.mock import AsyncMock
        counts={'A1':0,'B1':0}
        async def visit(page,url,cid,trade):
            counts[trade]+=1
            data=payload()
            info=data['result']['list'][0]['representativeArticleInfo']
            info['tradeType']=trade
            info['articleNumber']=f'{trade}-{counts[trade]}'
            if trade=='A1' and counts[trade]==1:
                data['result']['totalCount']=2
            return await self.c._record_article_response(Response(data),{'complexNumber':1,'tradeTypes':[trade]})
        async def metadata(page,url):
            return [{}] if 'pyeongList' in url else {'name':'test'}
        row={'id':'1','name':'test','badge':'아파트','text':'10세대'}
        with patch.object(self.c,'_load_region_complexes',AsyncMock(return_value=[row])), \
                patch.object(self.c,'_get_required_json',side_effect=metadata), \
                patch.object(self.c,'_visit_detail_page_and_wait_for_article',side_effect=visit), \
                patch('crawler.asyncio.sleep',AsyncMock()):
            self.assertTrue(await self.c._process_region_tab_once(SimpleNamespace(),'fixture','서울시 테스트구 테스트동'))
        self.assertEqual(counts,{'A1':2,'B1':1})
        self.assertEqual(self.c.article_seen_ids[('1','A1')],{'A1-2'})
        self.assertTrue(self.c._has_complete_article_response('1','A1'))

    async def test_timed_out_parser_cannot_write_into_next_attempt(self):
        gate=asyncio.Event()
        stale=payload();stale['result']['list'][0]['representativeArticleInfo']['articleNumber']='stale'
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.c._record_article_response(Response(stale,gate=gate)),0.01)
        self.c._reset_article_pair('1','A1')
        fresh=payload();fresh['result']['list'][0]['representativeArticleInfo']['articleNumber']='fresh'
        await self.c._record_article_response(Response(fresh))
        gate.set();await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(self.c.article_seen_ids[('1','A1')],{'fresh'})
        self.assertTrue(self.c._has_complete_article_response('1','A1'))
        self.assertFalse(self.c.article_response_tasks)
    async def test_empty_pyeong_catalog_requires_both_trades_proven_empty(self):
        self.c.complexes['1'] = {'pyeongs': []}
        with self.assertRaises(ArticleResponseUnavailable):
            self.c._validate_empty_pyeong_catalog('1')
        empty = {'isSuccess': True, 'result': {'list': [], 'hasNextPage': False, 'totalCount': 0}}
        for trade in ('A1', 'B1'):
            await self.c._record_article_response(Response(empty), {'complexNumber': 1, 'tradeTypes': [trade]})
        self.c._validate_empty_pyeong_catalog('1')

    async def test_empty_pyeong_catalog_with_listing_still_fails(self):
        self.c.complexes['1'] = {'pyeongs': []}
        await self.c._record_article_response(Response(payload()))
        with self.assertRaises(ArticleResponseUnavailable):
            self.c._validate_empty_pyeong_catalog('1')

if __name__=='__main__':unittest.main()
