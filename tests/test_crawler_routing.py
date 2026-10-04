import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import crawler
class RoutingTests(unittest.TestCase):
 def test_only_leaf_targets_and_deduplicate_urls(self):
  tree={'광주시':{'url':'root','has_complexes':True,'children':{'북구':{'url':'g','has_complexes':True,'children':{'동':{'url':'leaf','has_complexes':True}}}}},'전라남도':{'url':'root','has_complexes':True,'children':{'목포시':{'url':'m','has_complexes':True,'children':{'동':{'url':'other','has_complexes':True}}}}}}
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'regions.json';p.write_text(json.dumps(tree))
   with patch.object(crawler,'REGION_JSON_PATH',str(p)):
    self.assertEqual({x['url'] for x in crawler.get_sharded_targets(0,1)},{'leaf','other'})
 def test_parent_false_does_not_hide_valid_child(self):
  self.assertEqual(crawler.get_all_leaf_items({'has_complexes':False,'children':{'동':{'url':'u','has_complexes':True}}},'시'),[('시 동','u')])
 def test_merged_site_label_keeps_business_split(self):
  self.assertEqual(crawler.normalize_business_region('전남광주시','광산구','광주시 광산구 수완동'),'광주시')
  self.assertEqual(crawler.normalize_business_region('전남광주시','목포시','전라남도 목포시 상동'),'전라남도')
  self.assertEqual(crawler.normalize_business_region('경기도','광주시','경기도 광주시 경안동'),'경기도')
 def test_bad_shard_rejected(self):
  for args in [(-1,20),(20,20),(0,0)]:
   with self.assertRaises(ValueError):crawler.get_sharded_targets(*args)
if __name__=='__main__':unittest.main()
