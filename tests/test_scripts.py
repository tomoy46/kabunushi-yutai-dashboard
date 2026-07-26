import csv,json,tempfile,unittest,datetime as dt
from unittest.mock import patch
from pathlib import Path
import sys;sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from csv_to_json import convert
from market_data import update_market_data
from merge_benefit_universe import merge
from fetch_tdnet import extract,merge_queue
from update_listed_companies_from_jpx import parse_workbook,update
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

class JPXListedCompaniesTests(unittest.TestCase):
 @staticmethod
 def workbook(rows):
  headings=['コード','銘柄名','市場・商品区分','33業種区分']
  class Sheet:
   ncols=len(headings);nrows=len(rows)+1
   def cell_value(self,row,column):return (headings if row==0 else rows[row-1])[column]
  class Book:
   def sheet_by_index(self,index):return Sheet()
  return Book()

 def common_rows(self):
  rows=[
   [2593.0,'伊藤園','プライム（内国株式）','食料品'],
   ['130A','英字コード株式会社','グロース（内国株式）','情報・通信業'],
  ]
  rows.extend([[float(code),f'普通株式会社{code}','スタンダード（内国株式）','サービス業'] for code in range(3000,5998)])
  return rows

 def test_class_share_is_excluded_while_common_and_alphanumeric_stocks_remain(self):
  rows=self.common_rows()+[[25935.0,'伊藤園第1種優先株式','プライム（内国株式）','食料品']]
  excluded=[]
  previous=[{'code':'2593','official_domain':'itoen.co.jp'}]
  fake_xlrd=type('Xlrd',(),{'open_workbook':lambda _self,_path:self.workbook(rows)})()
  with patch.dict(sys.modules,{'xlrd':fake_xlrd}):
   companies=parse_workbook(Path('fixture.xls'),previous,excluded)
  by_code={company['code']:company for company in companies}
  self.assertGreaterEqual(len(companies),3000)
  self.assertEqual(len(companies),len(by_code))
  self.assertIn('2593',by_code);self.assertEqual(by_code['2593']['official_domain'],'itoen.co.jp')
  self.assertIn('130A',by_code)
  self.assertNotIn('25935',by_code)
  self.assertEqual(excluded,[{'code':'25935','name':'伊藤園第1種優先株式'}])

 def test_invalid_presumed_common_stock_still_fails_validation(self):
  rows=self.common_rows()+[[12345.0,'コード不正株式会社','プライム（内国株式）','サービス業']]
  fake_xlrd=type('Xlrd',(),{'open_workbook':lambda _self,_path:self.workbook(rows)})()
  with patch.dict(sys.modules,{'xlrd':fake_xlrd}),self.assertRaisesRegex(ValueError,"12345"):
   parse_workbook(Path('fixture.xls'))

 def test_download_or_validation_failure_does_not_replace_previous_master(self):
  with tempfile.TemporaryDirectory() as directory:
   output=Path(directory)/'listed-companies.json';progress=Path(directory)/'progress.json'
   original='[{"code":"2593","official_domain":"itoen.co.jp"}]\n';output.write_text(original,encoding='utf-8')
   with patch('update_listed_companies_from_jpx.download',side_effect=RuntimeError('download failed')),self.assertRaises(RuntimeError):
    update(output=output,progress_path=progress)
   self.assertEqual(output.read_text(encoding='utf-8'),original)
   rows=self.common_rows()+[[12345.0,'コード不正株式会社','プライム（内国株式）','サービス業']]
   fake_xlrd=type('Xlrd',(),{'open_workbook':lambda _self,_path:self.workbook(rows)})()
   with patch.dict(sys.modules,{'xlrd':fake_xlrd}),self.assertRaises(ValueError):
    update(output=output,source=Path('fixture.xls'),progress_path=progress)
   self.assertEqual(output.read_text(encoding='utf-8'),original)

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

 def test_alphanumeric_codes_are_selected_without_integer_conversion(self):
  master=[dict(self.company,code='130A'),dict(self.company,code='9999')]
  args=type('Args',(),{'start_code':'130A','end_code':'130A','retry_failed':False,'official_only':False,'batch_size':100,'daily_limit':100})()
  self.assertEqual([x['code'] for x in self.d.select(master,args,{'next_index':0},[],[])],['130A'])

 def test_unknown_domain_requires_grounding_fetch_identity_and_same_redirect_host(self):
  company=dict(self.company,official_domain=None)
  item={key:None for key in self.d.FIELDS};item.update({'benefit_status':'official_confirmed','confidence_score':95,'minimum_shares':100,'record_months':[3],'benefit_description':'商品','official_source_url':'https://corp.example/ir','annual_value_yen':None})
  class Headers:
   def get_content_type(self):return 'text/html'
  class Response:
   status=200;headers=Headers()
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def geturl(self):return 'https://corp.example/ir/final'
   def read(self,limit):return '実在株式会社（証券コード 1234）'.encode()
  with patch.object(self.d,'urlopen',return_value=Response()):
   checked,reasons=self.d.validate(item,company,{'https://corp.example/ir'})
  self.assertEqual(checked['benefit_status'],'official_confirmed');self.assertEqual(checked['_verified_domain'],'corp.example');self.assertEqual(reasons,[])

 def test_ungrounded_unknown_domain_is_never_official(self):
  company=dict(self.company,official_domain=None)
  item={key:None for key in self.d.FIELDS};item.update({'benefit_status':'official_confirmed','confidence_score':99,'minimum_shares':100,'record_months':[3],'benefit_description':'商品','official_source_url':'https://corp.example/ir'})
  checked,reasons=self.d.validate(item,company,set())
  self.assertEqual(checked['benefit_status'],'candidate');self.assertIn('fetch_failed',reasons)

 def test_uninvestigated_is_master_minus_unique_processed(self):
  master=['1','2','3','4'];processed=['1','1','3']
  self.assertEqual(len(master)-len(set(processed)),2)
