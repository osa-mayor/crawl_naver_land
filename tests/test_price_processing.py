import unittest

from crawler import NaverLandPlaywright


def article(
    number,
    price,
    *,
    trade_type="A1",
    supply=84.9,
    exclusive=59.9,
    supply_name="84A",
    exclusive_name="59A",
    floor_detail=None,
    floor_info=None,
):
    value = {
        "articleNumber": str(number),
        "tradeType": trade_type,
        "spaceInfo": {
            "supplySpace": supply,
            "exclusiveSpace": exclusive,
        },
        "priceInfo": {
            "dealPrice" if trade_type == "A1" else "warrantyPrice": price
        },
    }
    if supply_name is not None:
        value["spaceInfo"]["supplySpaceName"] = supply_name
    if exclusive_name is not None:
        value["spaceInfo"]["exclusiveSpaceName"] = exclusive_name
    if floor_detail is not None:
        value["floorDetailInfo"] = floor_detail
    if floor_info is not None:
        value["floorInfo"] = floor_info
    return value


def grouped(representative, *children):
    return {
        "representativeArticleInfo": representative,
        "duplicatedArticleInfo": {"articleInfoList": list(children)},
    }


class PriceProcessingTests(unittest.TestCase):
    def make_crawler(self, captured):
        crawler = NaverLandPlaywright()
        crawler.complexes = {
            "101": {
                "name": "테스트아파트",
                "households": 777,
                "pyeongs": [],
                "_dong_name": "서울시 강남구 테스트동",
            }
        }
        crawler.captured_articles = {"101": captured}
        return crawler

    def test_group_children_affect_extrema_not_representative_count_or_average(self):
        first = grouped(
            article("r1", 2000000000, floor_info="10/20"),
            article("b1", 1800000000, floor_info="2/20"),
            article("b2", 3500000000),
        )
        second = grouped(article("r2", 3000000000, floor_info="12/20"))

        row = self.make_crawler([first, first, second]).process_data().iloc[0]

        self.assertEqual(row["매매 매물수 (전체)"], 2)
        self.assertEqual(row["매매 평균가"], "250,000")
        self.assertEqual(row["매매 최저가 (일반)"], "200,000")
        self.assertEqual(row["매매 최저가 (저층)"], "180,000")
        self.assertEqual(row["매매 최고가"], "350,000")
        self.assertEqual(row["총세대수"], 777)

    def test_invalid_prices_are_dropped_and_unknown_floor_is_not_normal(self):
        captured = [
            article("bad-zero", 0, floor_info="10/20"),
            article("bad-text", "not-a-price", floor_info="10/20"),
            article("unknown", 2100000000),
            article("low", 1900000000, floor_info="3/20"),
        ]

        row = self.make_crawler(captured).process_data().iloc[0]

        self.assertEqual(row["매매 매물수 (전체)"], 2)
        self.assertEqual(row["매매 평균가"], "200,000")
        self.assertEqual(row["매매 최저가 (일반)"], "")
        self.assertEqual(row["매매 최저가 (저층)"], "190,000")
        self.assertEqual(row["매매 최고가"], "210,000")

    def test_missing_area_label_merges_only_with_unambiguous_named_subtype(self):
        captured = [
            article("sale-a", 2000000000),
            article(
                "rent-unlabelled",
                1000000000,
                trade_type="B1",
                supply_name=None,
                exclusive_name=None,
            ),
        ]

        frame = self.make_crawler(captured).process_data()

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["타입"], "84A")
        self.assertEqual(frame.iloc[0]["전세 매물수"], 1)

    def test_equivalent_numeric_labels_share_a_price_row(self):
        frame = self.make_crawler([
            article('sale', 2000000000, supply_name='84', exclusive_name='59'),
            article('rent', 1000000000, trade_type='B1', supply_name='84.0', exclusive_name='59.0'),
        ]).process_data()
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]['전세 매물수'], 1)

    def test_equal_numeric_areas_keep_a_b_and_ambiguous_unlabelled_separate(self):
        captured = [
            article("sale-a", 2000000000),
            article(
                "sale-b",
                2200000000,
                supply_name="84B",
                exclusive_name="59B",
            ),
            article(
                "rent-unlabelled",
                1000000000,
                trade_type="B1",
                supply_name=None,
                exclusive_name=None,
            ),
        ]

        frame = self.make_crawler(captured).process_data()

        self.assertEqual(len(frame), 3)
        self.assertEqual(set(frame["타입"]), {"84A", "84B", "84.9"})


if __name__ == "__main__":
    unittest.main()
