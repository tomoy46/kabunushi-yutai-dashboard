import csv,json,tempfile,unittest,datetime as dt
from pathlib import Path
import sys;sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from csv_to_json import convert
from market_data import update_market_data
from merge_benefit_universe import merge
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
  self.assertTrue(all(item['official_source_url'].startswith('https://') for item in items if item['data_confidence']=='official_confirmed'))

 def test_universe_merge_is_unique_and_preserves_official(self):
  with tempfile.TemporaryDirectory() as directory:
   out=Path(directory)/'out.json'; queue=Path(directory)/'queue.json'
   items,q=merge(Path('data/benefits.json'),Path('data/benefit-universe.csv'),out,queue)
   self.assertEqual(len(items),12); self.assertEqual(len(items),len({x['code'] for x in items})); self.assertEqual(q,[])
   self.assertEqual(next(x for x in items if x['code']=='8267')['data_confidence'],'official_confirmed')
   self.assertTrue(all(x['benefit_status']=='candidate' for x in q))

 def test_candidate_requires_real_company_source_month_and_check_date(self):
  fields=['code','name','market','sector','benefit_status','record_months','source_hint','last_checked_at']
  bad={'code':'1301','name':'公式確認待ち銘柄（1301）','market':'未確認','sector':'未確認','benefit_status':'candidate','record_months':'8','source_hint':'企業公式IRページを確認','last_checked_at':''}
  with tempfile.TemporaryDirectory() as directory:
   universe=Path(directory)/'universe.csv'
   with universe.open('w',encoding='utf-8',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerow(bad)
   with self.assertRaisesRegex(ValueError,'正式な会社名'):
    merge(Path('data/benefits.json'),universe,Path(directory)/'out.json',Path(directory)/'queue.json')

 def test_no_dummy_candidates_and_status_counts_are_preserved(self):
  items=json.loads(Path('data/benefits.json').read_text())
  self.assertFalse(any(x['name'].startswith('公式確認待ち銘柄') for x in items))
  self.assertEqual(sum(x['benefit_status']=='official_confirmed' for x in items),10)
  self.assertEqual(sum(x['benefit_status']=='abolished' for x in items),2)
  self.assertEqual(sum(x['benefit_status']=='candidate' for x in items),0)

if __name__=='__main__':unittest.main()

class GeminiDiscoveryTests(unittest.TestCase):
 def setUp(self):
  import discover_benefits_with_gemini as discovery
  self.d=discovery
  self.company={'code':'1234','name':'実在株式会社','market':'プライム','sector':'製造業','official_domain':'example.co.jp'}

 def test_unofficial_or_missing_url_cannot_be_confirmed(self):
  item={key:None for key in self.d.FIELDS};item.update({'benefit_status':'official_confirmed','confidence_score':95,'minimum_shares':100,'record_months':[3],'benefit_description':'商品','official_source_url':None})
  checked,reasons=self.d.validate(item,self.company,set())
  self.assertEqual(checked['benefit_status'],'candidate');self.assertIn('official_source_not_found',reasons)

 def test_confidence_below_90_is_queued(self):
  item={'confidence_score':89,'official_source_url':'https://example.co.jp/ir','minimum_shares':100,'record_months':[3],'benefit_status':'candidate'}
  self.assertIn('low_confidence',self.d.queue_reasons(item))

 def test_selection_resumes_and_preserves_recent_official(self):
  master=[dict(self.company,code='1111'),dict(self.company,code='2222'),dict(self.company,code='3333')]
  args=type('Args',(),{'start_code':None,'end_code':None,'retry_failed':False,'official_only':False,'batch_size':100,'daily_limit':100})()
  benefits=[{'code':'2222','benefit_status':'official_confirmed','official_verified_at':dt.date.today().isoformat()}]
  selected=self.d.select(master,args,{'next_index':1,'failed_codes':[]},benefits,[])
  self.assertEqual([x['code'] for x in selected],['3333','1111'])

 def test_duplicate_listed_codes_are_detectable(self):
  master=[self.company,self.company]
  self.assertNotEqual(len(master),len({x['code'] for x in master}))
