import json,tempfile,unittest
from pathlib import Path
import sys;sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from market_data import update_market_data
from fetch_tdnet import extract,merge_queue
class Tests(unittest.TestCase):
 def test_previous_quote_retained_on_failure(self):
  class Broken:
   def fetch(self,code):raise RuntimeError('failure')
  old={'1234':{'price':100}};new,errors=update_market_data(['1234'],Broken(),old);self.assertEqual(new,old);self.assertTrue(errors[0]['previous_data_retained'])
 def test_tdnet_keyword_and_deduplication(self):
  xml='<rss><channel><item><title>株主優待制度の変更</title><link>https://x</link></item><item><title>決算短信</title></item></channel></rss>'.encode();found=extract(xml);self.assertEqual(len(found),1);self.assertEqual(len(merge_queue(found,found)),1)
if __name__=='__main__':unittest.main()
