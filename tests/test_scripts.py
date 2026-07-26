import csv,json,tempfile,unittest,datetime as dt,io
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlparse
from pathlib import Path
import sys;sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from csv_to_json import convert
from market_data import JQuantsProvider, update_market_data
from merge_benefit_universe import merge
from fetch_tdnet import extract,merge_queue
from update_listed_companies_from_jpx import parse_workbook,update
class Tests(unittest.TestCase):
 def test_jquants_provider_uses_latest_close(self):
  response=io.BytesIO(b'{"data":[{"Date":"2026-07-24","C":123.5},{"Date":"2026-07-23","C":120}]}')
  with patch('market_data.urlopen',return_value=response) as request:
   quote=JQuantsProvider('secret',today=dt.date(2026,7,26)).fetch('1234')
  self.assertEqual(quote.price,123.5);self.assertEqual(quote.price_at,'2026-07-24');self.assertEqual(quote.source,'jquants')
  self.assertEqual(request.call_args.args[0].headers['X-api-key'],'secret')
 def test_jquants_free_plan_uses_delayed_window_and_actual_latest_trading_date(self):
  # 84日前が日曜日でも、幅を持たせた期間から最新取引日を選ぶ。
  response=io.BytesIO(b'{"data":[{"Date":"2026-05-01","C":101},{"Date":"2026-04-30","C":99}]}')
  with patch('market_data.urlopen',return_value=response) as request:
   quote=JQuantsProvider('secret',today=dt.date(2026,7,26)).fetch('1234')
  params=parse_qs(urlparse(request.call_args.args[0].full_url).query)
  self.assertEqual(params,{'code':['1234'],'from':['2026-04-19'],'to':['2026-05-03']})
  self.assertEqual(quote.price,101.0)
  self.assertEqual(quote.price_at,'2026-05-01')
 def test_previous_quote_retained_on_failure(self):
  class Broken:
   def fetch(self,code):raise RuntimeError('failure')
  old={'1234':{'price':100}};new,errors=update_market_data(['1234'],Broken(),old);self.assertEqual(new,old);self.assertTrue(errors[0]['previous_data_retained'])
 def test_tdnet_keyword_and_deduplication(self):
  xml='<rss><channel><item><title>株主優待制度の変更</title><link>https://x</link></item><item><title>決算短信</title></item></channel></rss>'.encode();found=extract(xml);self.assertEqual(len(found),1);self.assertEqual(len(merge_queue(found,found)),1)
 def test_benefit_master_schema_and_kddi_orix(self):
  with tempfile.TemporaryDirectory() as directory:
   items=convert(Path('data/benefits.csv'),Path(directory)/'benefits.json')
  self.assertEqual(len(items),13)
  self.assertEqual(sum(item['benefit_status']=='official_confirmed' for item in items),11)
  by_code={item['code']:item for item in items}
  self.assertEqual(by_code['8591']['benefit_status'],'abolished')
  self.assertEqual(by_code['8591']['last_record_date'],'2024-03-31')
  self.assertEqual(by_code['9433']['benefit_tiers'][0]['shares'],200)
  kyokuyo=by_code['1301']
  self.assertEqual(kyokuyo['market'],'プライム')
  self.assertEqual(kyokuyo['industry'],'水産・農林業')
  self.assertEqual(kyokuyo['record_months'],[3])
  self.assertEqual(kyokuyo['benefit_status'],'official_confirmed')
  self.assertEqual(kyokuyo['data_confidence'],'official_confirmed')
  self.assertEqual(kyokuyo['confidence_score'],95)
  self.assertEqual(kyokuyo['minimum_shares'],100)
  self.assertEqual(kyokuyo['annual_value_yen'],2500)
  self.assertEqual(kyokuyo['benefit_tiers'],[
   {'shares':100,'maximum_shares':299,'description':'2,500円相当の自社製品','annual_value_yen':2500},
   {'shares':300,'maximum_shares':None,'description':'6,000円相当の自社製品','annual_value_yen':6000},
  ])
  self.assertIn('3月31日',kyokuyo['change_or_abolition_note'])
  self.assertIn('毎年7月贈呈予定',kyokuyo['change_or_abolition_note'])
  self.assertTrue(all(item['official_source_url'].startswith('https://') for item in items if item['data_confidence']=='official_confirmed'))

 def test_universe_merge_is_unique_and_preserves_official(self):
  with tempfile.TemporaryDirectory() as directory:
   out=Path(directory)/'out.json'; queue=Path(directory)/'queue.json'
   items,q=merge(Path('data/benefits.json'),Path('data/benefit-universe.csv'),out,queue)
   self.assertEqual(len(items),13); self.assertEqual(len(items),len({x['code'] for x in items})); self.assertEqual(q,[])
   self.assertEqual(next(x for x in items if x['code']=='8267')['data_confidence'],'official_confirmed')
   self.assertTrue(all(x['benefit_status']=='candidate' for x in q))

 def test_candidate_requires_real_company_source_month_and_check_date(self):
  fields=['code','name','market','sector','benefit_status','record_months','source_hint','last_checked_at']
  bad={'code':'1302','name':'公式確認待ち銘柄（1302）','market':'未確認','sector':'未確認','benefit_status':'candidate','record_months':'8','source_hint':'企業公式IRページを確認','last_checked_at':''}
  with tempfile.TemporaryDirectory() as directory:
   universe=Path(directory)/'universe.csv'
   with universe.open('w',encoding='utf-8',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerow(bad)
   with self.assertRaisesRegex(ValueError,'正式な会社名'):
    merge(Path('data/benefits.json'),universe,Path(directory)/'out.json',Path(directory)/'queue.json')

 def test_no_dummy_candidates_and_status_counts_are_preserved(self):
  items=json.loads(Path('data/benefits.json').read_text())
  self.assertFalse(any(x['name'].startswith('公式確認待ち銘柄') for x in items))
  self.assertEqual(sum(x['benefit_status']=='official_confirmed' for x in items),11)
  self.assertEqual(sum(x['benefit_status']=='abolished' for x in items),2)
  self.assertEqual(sum(x['benefit_status']=='candidate' for x in items),0)

 def test_kyokuyo_is_confirmed_and_removed_only_from_verification_queue(self):
  items=json.loads(Path('data/benefits.json').read_text())
  kyokuyo=next(x for x in items if x['code']=='1301')
  self.assertEqual(kyokuyo['benefit_status'],'official_confirmed')
  self.assertEqual(kyokuyo['official_source_url'],'https://www.kyokuyo.co.jp/ir/concept/')
  self.assertGreaterEqual(kyokuyo['confidence_score'],90)
  self.assertEqual(kyokuyo['annual_value_yen'],2500)
  queue=json.loads(Path('data/verification-queue.json').read_text())
  self.assertFalse(any(x['code']=='1301' for x in queue))
  self.assertEqual(len(queue),9)
  progress=json.loads(Path('data/discovery-progress.json').read_text())
  self.assertNotIn('1301',progress['processed_codes'])

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

 def test_search_and_structured_extraction_are_separate_requests(self):
  payloads=[]
  def request(payload,key,stage,model,max_retries=5):payloads.append((stage,model,payload));return {}
  with patch.object(self.d,'request_gemini',side_effect=request):
   self.d.call_gemini_search(self.company,'secret')
   self.d.call_gemini_structured(self.company,'secret','調査回答',['https://example.co.jp/ir'])
  search=payloads[0][2];structured=payloads[1][2]
  self.assertEqual(payloads[0][0],'search');self.assertIn('google_search',search['tools'][0])
  self.assertNotIn('responseMimeType',search['generationConfig']);self.assertNotIn('responseJsonSchema',search['generationConfig'])
  self.assertEqual(payloads[1][0],'structured_extraction');self.assertNotIn('tools',structured)
  self.assertEqual(structured['generationConfig']['responseMimeType'],'application/json');self.assertEqual(structured['generationConfig']['responseJsonSchema'],self.d.SCHEMA)

 def http_error(self,status,message='bad request',api_status='INVALID_ARGUMENT'):
  body=json.dumps({'error':{'status':api_status,'message':message}}).encode()
  return HTTPError('https://example.invalid',status,'failed',{},io.BytesIO(body))

 def test_http_429_does_not_retry_and_400_does_not_retry(self):
  response=type('Response',(),{'__enter__':lambda s:s,'__exit__':lambda *a:None,'read':lambda s:b'{}'})()
  with patch.object(self.d,'urlopen',side_effect=[self.http_error(429),response]) as opened,patch.object(self.d.time,'sleep'):
   with self.assertRaises(self.d.StageHTTPError):self.d.request_gemini({},'secret','search',max_retries=2)
  self.assertEqual(opened.call_count,1)
  with patch.object(self.d,'urlopen',side_effect=self.http_error(400)) as opened:
   with self.assertRaises(self.d.StageHTTPError):self.d.request_gemini({},'secret','search')
  self.assertEqual(opened.call_count,1)

 def test_http_error_fields_are_bounded_and_secret_is_redacted(self):
  secret='TOP-SECRET-KEY'
  with patch.object(self.d,'urlopen',side_effect=self.http_error(403,secret+' '+('x'*600),'PERMISSION_DENIED')):
   with self.assertRaises(self.d.StageHTTPError) as caught:self.d.request_gemini({},secret,'structured_extraction')
  record=self.d.error_record(caught.exception,self.company)
  self.assertEqual(record['http_status'],403);self.assertEqual(record['api_error_status'],'PERMISSION_DENIED');self.assertEqual(record['stage'],'structured_extraction')
  self.assertLessEqual(len(record['api_error_message']),500);self.assertNotIn(secret,json.dumps(record))

 def run_fixture(self,directory,failed_codes):
  root=Path(directory);companies=[dict(self.company,code='130A')]+[dict(self.company,code=str(2000+i),name=f'会社{i}') for i in range(12)]
  (root/'listed-companies.json').write_text(json.dumps(companies),encoding='utf-8')
  (root/'benefits.json').write_text('[]',encoding='utf-8');(root/'verification-queue.json').write_text('[]',encoding='utf-8')
  (root/'discovery-progress.json').write_text(json.dumps({'next_index':0,'processed_codes':[],'failed_codes':failed_codes}),encoding='utf-8')
  (root/'company-domains.json').write_text('{}',encoding='utf-8');(root/'api-usage.json').write_text('[]',encoding='utf-8')
  return type('Args',(),{'start_code':None,'end_code':None,'retry_failed':True,'official_only':False,'batch_size':100,'daily_limit':100,'diagnostic_mode':False})()

 def test_first_http_400_stops_and_saves_error_fields(self):
  with tempfile.TemporaryDirectory() as directory:
   args=self.run_fixture(directory,[])
   error=self.d.StageHTTPError('search',400,'INVALID_ARGUMENT','bad request')
   with patch.object(self.d,'DATA',Path(directory)),patch.dict('os.environ',{'GEMINI_API_KEY':'secret'}),patch.object(self.d,'list_models',return_value={'gemini-2.5-flash-lite'}),patch.object(self.d,'select_models',return_value=('gemini-2.5-flash-lite','gemini-2.5-flash-lite',{})),patch.object(self.d,'call_gemini_search',side_effect=error):
    self.assertEqual(self.d.run(args),1)
   usage=json.loads((Path(directory)/'api-usage.json').read_text())[-1];queue=json.loads((Path(directory)/'verification-queue.json').read_text())
   self.assertEqual(usage['processed_companies'],1);self.assertEqual(queue[0]['http_status'],400);self.assertEqual(queue[0]['api_error_message'],'bad request')

 def test_success_removes_failed_code_without_duplicate_processed_code(self):
  with tempfile.TemporaryDirectory() as directory:
   args=self.run_fixture(directory,['130A'])
   result={key:None for key in self.d.FIELDS};result.update({'benefit_status':'candidate','confidence_score':20,'code':'130A','name':'実在株式会社'})
   with patch.object(self.d,'DATA',Path(directory)),patch.dict('os.environ',{'GEMINI_API_KEY':'secret'}),patch.object(self.d,'list_models',return_value={'gemini-2.5-flash-lite'}),patch.object(self.d,'select_models',return_value=('gemini-2.5-flash-lite','gemini-2.5-flash-lite',{})),patch.object(self.d,'call_gemini_search',return_value={}),patch.object(self.d,'parse_search_response',return_value=('answer',set(),[],[])),patch.object(self.d,'call_gemini_structured',return_value={}),patch.object(self.d,'parse_structured_response',return_value=result):
    self.d.run(args)
   progress=json.loads((Path(directory)/'discovery-progress.json').read_text())
   self.assertNotIn('130A',progress['failed_codes']);self.assertEqual(progress['processed_codes'].count('130A'),1)

class GeminiModelCompatibilityTests(unittest.TestCase):
 def setUp(self):
  import discover_benefits_with_gemini as discovery
  self.d=discovery
  self.company={'code':'1234','name':'実在株式会社','market':'プライム','sector':'製造業','official_domain':'example.co.jp'}

 def run_fixture(self,directory,failed_codes):
  root=Path(directory);companies=[dict(self.company,code='130A')]+[dict(self.company,code=str(2000+i),name=f'会社{i}') for i in range(12)]
  (root/'listed-companies.json').write_text(json.dumps(companies),encoding='utf-8')
  (root/'benefits.json').write_text('[]',encoding='utf-8');(root/'verification-queue.json').write_text('[]',encoding='utf-8')
  (root/'discovery-progress.json').write_text(json.dumps({'next_index':0,'processed_codes':[],'failed_codes':failed_codes}),encoding='utf-8')
  (root/'company-domains.json').write_text('{}',encoding='utf-8');(root/'api-usage.json').write_text('[]',encoding='utf-8')
  return type('Args',(),{'start_code':None,'end_code':None,'retry_failed':True,'official_only':False,'batch_size':100,'daily_limit':100,'diagnostic_mode':False})()

 def test_models_list_filters_generation_support_and_paginates(self):
  pages=[{'models':[{'name':'models/gemini-2.5-flash','supportedGenerationMethods':['generateContent']},{'name':'models/embed','supportedGenerationMethods':['embedContent']}],'nextPageToken':'next'}, {'models':[{'name':'models/gemini-2.5-flash-lite','supportedGenerationMethods':['generateContent']}]}]
  class Response:
   def __init__(self,body):self.body=body
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self):return json.dumps(self.body).encode()
  with patch.object(self.d,'urlopen',side_effect=[Response(x) for x in pages]),patch.object(self.d,'wait_for_api_interval'):
   available=self.d.list_models('TOP-SECRET-KEY')
  self.assertEqual(available,{'gemini-2.5-flash','gemini-2.5-flash-lite'})

 def test_candidate_order_matches_required_priority(self):
  expected=('gemini-3.1-flash-lite','gemini-3.5-flash-lite','gemini-3.6-flash','gemini-3.5-flash','gemini-flash-latest','gemini-2.5-flash-lite','gemini-2.5-flash')
  self.assertEqual(self.d.SEARCH_MODELS,expected);self.assertEqual(self.d.EXTRACTION_MODELS,expected)

 def test_listed_model_requires_real_plain_and_capability_probes(self):
  available={'gemini-3.1-flash-lite','gemini-3.5-flash-lite','gemini-2.5-flash-lite'}
  unavailable=self.d.StageHTTPError('model_probe_plain',404,'NOT_FOUND','not available')
  calls=[]
  def plain(_key,model):
   calls.append(('plain',model))
   if model in ('gemini-3.1-flash-lite','gemini-2.5-flash-lite'):raise unavailable
  with patch.object(self.d,'probe_plain',side_effect=plain),patch.object(self.d,'probe_search',side_effect=lambda k,m:calls.append(('search',m))),patch.object(self.d,'probe_structured',side_effect=lambda k,m:calls.append(('structured',m))),patch('sys.stderr',io.StringIO()):
   search,extraction,results=self.d.diagnose_models(available,'TOP-SECRET-KEY')
  self.assertEqual((search,extraction),('gemini-3.5-flash-lite','gemini-3.5-flash-lite'))
  self.assertEqual(results['gemini-3.1-flash-lite']['plain']['status'],'unavailable')
  self.assertNotIn(('search','gemini-3.1-flash-lite'),calls)
  self.assertNotIn('TOP-SECRET-KEY',json.dumps(results))

 def test_search_and_structured_failures_select_next_qualified_models(self):
  available={'gemini-3.1-flash-lite','gemini-3.5-flash-lite','gemini-3.6-flash'}
  def search(_key,model):
   if model=='gemini-3.1-flash-lite':raise ValueError('no grounding metadata')
  def structured(_key,model):
   if model!='gemini-3.6-flash':raise ValueError('invalid JSON')
  with patch.object(self.d,'probe_plain'),patch.object(self.d,'probe_search',side_effect=search),patch.object(self.d,'probe_structured',side_effect=structured),patch('sys.stderr',io.StringIO()):
   search_model,extraction_model,_=self.d.diagnose_models(available,'secret')
  self.assertEqual(search_model,'gemini-3.5-flash-lite');self.assertEqual(extraction_model,'gemini-3.6-flash')

 def test_project_quota_stops_probing_but_zero_model_quota_continues(self):
  model_quota=self.d.StageHTTPError('model_probe_plain',429,'RESOURCE_EXHAUSTED','quota',{'violations':[{'quotaValue':'0','model':'gemini-3.1-flash-lite'}]})
  with patch.object(self.d,'probe_plain',side_effect=[model_quota,None]),patch.object(self.d,'probe_search'),patch.object(self.d,'probe_structured'),patch('sys.stderr',io.StringIO()):
   selected=self.d.diagnose_models({'gemini-3.1-flash-lite','gemini-3.5-flash-lite'},'secret')[:2]
  self.assertEqual(selected,('gemini-3.5-flash-lite','gemini-3.5-flash-lite'))
  project_quota=self.d.StageHTTPError('model_probe_plain',429,'RESOURCE_EXHAUSTED','quota',{'violations':[{'quotaValue':'10'}]})
  with patch.object(self.d,'probe_plain',side_effect=project_quota),patch('sys.stderr',io.StringIO()),self.assertRaises(self.d.StageHTTPError):
   self.d.diagnose_models({'gemini-3.1-flash-lite','gemini-3.5-flash-lite'},'secret')

 def test_429_stops_without_advancing_or_recording_company_failure(self):
  with tempfile.TemporaryDirectory() as directory:
   args=self.run_fixture(directory,[])
   root=Path(directory)
   before=json.loads((root/'discovery-progress.json').read_text())
   error=self.d.StageHTTPError('search',429,'RESOURCE_EXHAUSTED','quota exhausted',{'violations':[{'quotaMetric':'metric','model':'gemini-2.5-flash-lite'}],'retryDelay':'10s'})
   stderr=io.StringIO()
   with patch.object(self.d,'DATA',root),patch.dict('os.environ',{'GEMINI_API_KEY':'TOP-SECRET-KEY'},clear=True),patch.object(self.d,'list_models',return_value={'gemini-2.5-flash-lite'}),patch.object(self.d,'select_models',return_value=('gemini-2.5-flash-lite','gemini-2.5-flash-lite',{})),patch.object(self.d,'call_gemini_search',side_effect=error),patch('sys.stderr',stderr):
    self.assertEqual(self.d.run(args),1)
   after=json.loads((root/'discovery-progress.json').read_text())
   self.assertEqual(after,before)
   self.assertNotIn('130A',after['failed_codes'])
   self.assertEqual(json.loads((root/'verification-queue.json').read_text()),[])
   log=stderr.getvalue()
   self.assertIn('quotaMetric',log)
   self.assertIn('無料のGoogle検索対応モデルを利用できません',log)
   self.assertNotIn('TOP-SECRET-KEY',log)
