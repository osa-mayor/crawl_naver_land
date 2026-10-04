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
