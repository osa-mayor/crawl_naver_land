import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import crawler
from tests import test_price_processing as fixtures

class SubtypeStorageTests(unittest.TestCase):
    def make_articles(self, supply_name=None):
        rows=[]
        for subtype, price in [('A',2000000000),('B',2200000000)]:
            value=fixtures.article(subtype,price,supply_name=supply_name,exclusive_name=None)
            value['spaceInfo']['nameType']=subtype
            rows.append(value)
        return rows
    def saved(self, items):
        frame=fixtures.PriceProcessingTests().make_crawler(items).process_data()
        with tempfile.TemporaryDirectory() as d,patch.object(crawler,'DB_PATH',str(Path(d)/'test.db')):
            crawler.init_db()
            crawler.save_to_db(frame);crawler.save_to_db(frame)
            with sqlite3.connect(crawler.DB_PATH) as c:
                return c.execute('select pyeong_type,trade_max from prices order by pyeong_type').fetchall()
    def test_missing_supply_labels_keep_subtypes_and_prices_on_resave(self):
        self.assertEqual(self.saved(self.make_articles()),[('84.9A',200000),('84.9B',220000)])
    def test_shared_numeric_label_keeps_explicit_subtypes(self):
        self.assertEqual(self.saved(self.make_articles('84')),[('84A',200000),('84B',220000)])
    def test_inferred_group_identity_survives_unlabelled_first_article(self):
        unlabelled=fixtures.article('rent',1000000000,trade_type='B1',supply_name=None,exclusive_name=None)
        labelled=self.make_articles()[0]
        frame=fixtures.PriceProcessingTests().make_crawler([unlabelled,labelled]).process_data()
        self.assertEqual(len(frame),1)
        self.assertEqual(frame.iloc[0]['타입'],'84.9A')
        self.assertEqual(frame.iloc[0]['전세 매물수'],1)
    def test_existing_full_type_labels_unchanged(self):
        rows=self.make_articles()
        rows[0]['spaceInfo']['supplySpaceName']='84A'
        rows[1]['spaceInfo']['supplySpaceName']='84B'
        self.assertEqual(self.saved(rows),[('84A',200000),('84B',220000)])

if __name__=='__main__':unittest.main()
