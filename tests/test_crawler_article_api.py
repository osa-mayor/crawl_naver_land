import unittest

from crawler import (
    NaverLandPlaywright,
    article_item_identity,
    extract_article_result,
)


class ArticlePayloadTests(unittest.TestCase):
    def test_extracts_paginated_article_result(self):
        data = {
            "result": {
                "list": [{"representativeArticleInfo": {"articleNumber": "1"}}],
                "hasNextPage": True,
                "lastInfo": [1, 2, 3],
                "totalCount": 31,
            }
        }

        items, has_next, last_info, total_count = extract_article_result(data)

        self.assertEqual(items, data["result"]["list"])
        self.assertTrue(has_next)
        self.assertEqual(last_info, [1, 2, 3])
        self.assertEqual(total_count, 31)

    def test_extracts_legacy_list_result(self):
        data = {"result": [{"articleNumber": "1"}]}

        items, has_next, last_info, total_count = extract_article_result(data)

        self.assertEqual(items, [{"articleNumber": "1"}])
        self.assertFalse(has_next)
        self.assertEqual(last_info, [])
        self.assertIsNone(total_count)

    def test_capture_deduplicates_representative_article_numbers(self):
        crawler = NaverLandPlaywright(screenshot_dir="/tmp/crawler-test-screenshots")
        item = {"representativeArticleInfo": {"articleNumber": "2639177895"}}

        first = crawler.capture_article_items("3", [item])
        second = crawler.capture_article_items("3", [dict(item)])

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(crawler.captured_articles["3"], [item])
        self.assertEqual(article_item_identity(item), "2639177895")


if __name__ == "__main__":
    unittest.main()
