import json,tempfile,unittest
from pathlib import Path
import sys;sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from csv_to_json import convert
from market_data import update_market_data
from fetch_tdnet import extract,merge_queue
class Tests(unittest.TestCase):
 def test_previous_quote_retained_on_failure(self):
  class Broken:
   def fetch(self,code):raise RuntimeError('failure')
  old={'1234':{'price':100}};new,errors=update_market_data(['1234'],Broken(),old);self.assertEqual(new,old);self.assertTrue(errors[0]['previous_data_retained'])
 def test_tdnet_keyword_and_deduplication(self):
  xml='<rss><channel><item><title>株主優待制度の変更</title><link>https://x</link></item><item><title>決算短信</title></item></channel></rss>'.encode();found=extract(xml);self.assertEqual(len(found),1);self.assertEqual(len(merge_queue(found,found)),1)
 def test_benefit_master_schema_and_kddi_orix(self):
  with tempfile.TemporaryDirectory() as directory:
   items=convert(Path('data/benefits.csv'),Path(directory)/'benefits.json')
  self.assertEqual(len(items),12)
  by_code={item['code']:item for item in items}
  self.assertEqual(by_code['8591']['benefit_status'],'abolished')
  self.assertEqual(by_code['8591']['last_record_date'],'2024-03-31')
  self.assertEqual(by_code['9433']['benefit_tiers'][0]['shares'],200)
  self.assertTrue(all(item['official_source_url'].startswith('https://') for item in items))
if __name__=='__main__':unittest.main()
