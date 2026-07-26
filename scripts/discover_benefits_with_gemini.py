#!/usr/bin/env python3
"""Discover shareholder benefits from official sources with Gemini grounding.

Uses only the Python standard library. Existing confirmed/abolished records are immutable.
Writes each completed company atomically so interrupted runs can resume safely.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, random, re, socket, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'
SEARCH_MODELS=EXTRACTION_MODELS=('gemini-3.1-flash-lite','gemini-3.5-flash-lite','gemini-3.6-flash','gemini-3.5-flash','gemini-flash-latest','gemini-2.5-flash-lite','gemini-2.5-flash')
MODEL_STATUS_FILE='gemini-model-status.json'
MODEL_STATUS_TTL=dt.timedelta(hours=24)
ENDPOINT='https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
MODELS_ENDPOINT='https://generativelanguage.googleapis.com/v1beta/models'
OFFICIAL_HOSTS=('jpx.co.jp','tdnet.info')
BLOCKED=('yahoo.co.jp','minkabu.jp','kabutan.jp','rakuten-sec.co.jp','sbisec.co.jp','monex.co.jp','note.com','x.com','facebook.com','instagram.com','youtube.com')
FIELDS={'code':{'type':'string'},'name':{'type':'string'},'benefit_status':{'type':'string','enum':['official_confirmed','candidate','abolished']},'record_months':{'type':'array','items':{'type':'integer'}},'record_date':{'type':['string','null']},'annual_occurrences':{'type':['integer','null']},'minimum_shares':{'type':['integer','null']},'maximum_shares':{'type':['integer','null']},'benefit_title':{'type':['string','null']},'benefit_description':{'type':['string','null']},'category':{'type':['string','null']},'annual_value_yen':{'type':['integer','null']},'valuation_type':{'type':'string','enum':['official_amount','not_calculated']},'long_term_required':{'type':['boolean','null']},'holding_period_months':{'type':['integer','null']},'conditions':{'type':['string','null']},'official_source_url':{'type':['string','null']},'official_source_title':{'type':['string','null']},'official_verified_at':{'type':'string'},'abolished_at':{'type':['string','null']},'last_record_date':{'type':['string','null']},'change_or_abolition_note':{'type':['string','null']},'confidence_score':{'type':'integer','minimum':0,'maximum':100},'evidence_text':{'type':['string','null']},'error_reason':{'type':['string','null']}}
SCHEMA={'type':'object','properties':FIELDS,'required':list(FIELDS),'additionalProperties':False}
QUEUE_REASONS={'low_confidence','official_source_not_found','minimum_shares_unknown','record_date_unknown','conflicting_official_sources','planned_change','pdf_parse_failed','fetch_failed'}

class StageHTTPError(Exception):
 def __init__(self,stage,http_status,api_status,message,quota=None):
  self.stage=stage;self.http_status=http_status;self.api_status=api_status;self.api_message=(message or '')[:300]
  self.quota=quota or {}
  super().__init__(f'{stage}: HTTP {http_status} {api_status or ""} {self.api_message}'.strip())

def http_error(error,stage,secret=None):
 """Read Gemini's safe error fields without retaining headers, keys, or request URLs."""
 try: body=json.loads(error.read().decode('utf-8','replace'))
 except (ValueError,UnicodeError):body={}
 detail=body.get('error') if isinstance(body,dict) else {}
 if not isinstance(detail,dict):detail={}
 message=str(detail.get('message') or error.reason or 'HTTP error')
 if secret:message=message.replace(secret,'[REDACTED]')
 message=re.sub(r'([?&](?:key|api_key)=)[^&\s]+',r'\1[REDACTED]',message,flags=re.I)
 quota={}
 if error.code==429:
  violations=[];retry_delay=None
  for entry in detail.get('details',[]) if isinstance(detail.get('details'),list) else []:
   if not isinstance(entry,dict):continue
   kind=entry.get('@type','')
   if kind.endswith('QuotaFailure'):
    for violation in entry.get('violations',[]):
     if isinstance(violation,dict):
      safe={k:safe_text(violation.get(k),secret) for k in ('quotaMetric','quotaId','quotaValue') if violation.get(k) is not None}
      dimensions=violation.get('quotaDimensions') if isinstance(violation.get('quotaDimensions'),dict) else {}
      model=violation.get('model') or dimensions.get('model')
      if model is not None:safe['model']=safe_text(model,secret)
      violations.append(safe)
   if kind.endswith('RetryInfo'):retry_delay=entry.get('retryDelay')
  quota={'violations':violations,'retryDelay':safe_text(retry_delay,secret) if retry_delay is not None else None}
 return StageHTTPError(stage,error.code,detail.get('status'),message,quota)

def model_name(value):
 return str(value or '').removeprefix('models/')

def list_models(key):
 """Return generateContent-capable model IDs without logging the key or response."""
 available=set();page_token=None
 while True:
  url=MODELS_ENDPOINT+(('?pageToken='+page_token) if page_token else '')
  req=Request(url,headers={'x-goog-api-key':key})
  wait_for_api_interval()
  try:
   with urlopen(req,timeout=30) as response:body=json.loads(response.read())
  except HTTPError as error:raise http_error(error,'models.list',key) from None
  for model in body.get('models',[]):
   if isinstance(model,dict) and 'generateContent' in (model.get('supportedGenerationMethods') or []):available.add(model_name(model.get('name')))
  page_token=body.get('nextPageToken')
  if not page_token:return available

def quota_is_model_specific_zero(error):
 """A zero quota explicitly scoped to a model means only that model is unusable."""
 if not isinstance(error,StageHTTPError) or error.http_status!=429:return False
 violations=error.quota.get('violations',[]) if isinstance(error.quota,dict) else []
 return bool(violations) and all(str(v.get('quotaValue','')).strip() in ('0','0.0') and bool(v.get('model')) for v in violations)

def probe_error(error,key):
 if not isinstance(error,StageHTTPError):return {'status':'unavailable','error':safe_text(str(error),key)[:300]}
 result={'status':'unavailable','http_status':error.http_status,'api_status':error.api_status,'error':safe_text(error.api_message,key)[:300]}
 if error.http_status==429:result['quota']=error.quota
 return result

def probe_plain(key,model):
 payload={'contents':[{'role':'user','parts':[{'text':'OKとだけ回答してください'}]}]}
 return request_gemini(payload,key,'model_probe_plain',model,max_retries=1)

def probe_search(key,model):
 payload={'contents':[{'role':'user','parts':[{'text':'株式会社極洋の公式サイトを1件検索してください'}]}],'tools':[{'google_search':{}}]}
 response=request_gemini(payload,key,'model_probe_search',model,max_retries=1)
 candidates=response.get('candidates') or []
 if not candidates or not candidates[0].get('groundingMetadata'):raise ValueError('groundingMetadata was not returned')
 return response

def probe_structured(key,model):
 schema={'type':'object','properties':{'status':{'type':'string'}},'required':['status']}
 payload={'contents':[{'role':'user','parts':[{'text':'statusをOKとして回答してください'}]}],'generationConfig':{'responseMimeType':'application/json','responseJsonSchema':schema}}
 response=request_gemini(payload,key,'model_probe_structured',model,max_retries=1)
 parsed=parse_structured_response(response)
 if not isinstance(parsed,dict):raise ValueError('structured response is not a JSON object')
 return response

def print_probe(model,result):
 print(model,file=sys.stderr)
 for label in ('plain','search grounding','structured output'):
  value=result.get(label.replace(' ','_'))
  if value:
   print(f'{label}: {"success" if value.get("status")=="success" else "unavailable"}',file=sys.stderr)
   if value.get('status')!='success':
    prefix=('HTTP %s %s' % (value.get('http_status',''),value.get('api_status',''))).strip()
    print((prefix+' '+value.get('error','')).strip()[:300],file=sys.stderr)

def diagnose_models(available,key):
 results={};search_model=extraction_model=None
 for model in SEARCH_MODELS:
  if model not in available:continue
  result=results.setdefault(model,{})
  try:probe_plain(key,model);result['plain']={'status':'success'}
  except Exception as error:
   result['plain']=probe_error(error,key);print_probe(model,result)
   if isinstance(error,StageHTTPError) and error.http_status==429 and not quota_is_model_specific_zero(error):raise
   continue
  if search_model is None:
   try:probe_search(key,model);result['search_grounding']={'status':'success'};search_model=model
   except Exception as error:
    result['search_grounding']=probe_error(error,key)
    if isinstance(error,StageHTTPError) and error.http_status==429 and not quota_is_model_specific_zero(error):raise
  if extraction_model is None:
   try:probe_structured(key,model);result['structured_output']={'status':'success'};extraction_model=model
   except Exception as error:
    result['structured_output']=probe_error(error,key)
    if isinstance(error,StageHTTPError) and error.http_status==429 and not quota_is_model_specific_zero(error):raise
  print_probe(model,result)
  if search_model and extraction_model:break
 return search_model,extraction_model,results

def cached_models(available):
 status=load(DATA/MODEL_STATUS_FILE,{})
 try:checked=dt.datetime.fromisoformat(status['checked_at'].replace('Z','+00:00'))
 except (KeyError,TypeError,ValueError):return None
 if dt.datetime.now(dt.timezone.utc)-checked>MODEL_STATUS_TTL:return None
 search=status.get('selected_search_model');extraction=status.get('selected_extraction_model')
 if search not in available or extraction not in available:return None
 return search,extraction,status.get('probe_results',{})

def select_models(available,key,force=False):
 cached=None if force else cached_models(available)
 if cached:return cached
 search,extraction,results=diagnose_models(available,key)
 if search and extraction:
  atomic(DATA/MODEL_STATUS_FILE,{'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'available_model_count':len(available),'selected_search_model':search,'selected_extraction_model':extraction,'probe_results':results})
 return search,extraction,results

def invalidate_model_cache():
 try:(DATA/MODEL_STATUS_FILE).unlink()
 except FileNotFoundError:pass

def load(path,default):
 try:return json.loads(path.read_text(encoding='utf-8'))
 except FileNotFoundError:return default

def atomic(path,value):
 tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(path)

def host_allowed(url,company):
 try: host=(urlparse(url).hostname or '').lower().removeprefix('www.')
 except ValueError:return False
 if not host or any(host==x or host.endswith('.'+x) for x in BLOCKED):return False
 expected=(company.get('official_domain') or '').lower().removeprefix('www.')
 return any(host==x or host.endswith('.'+x) for x in OFFICIAL_HOSTS) or bool(expected and (host==expected or host.endswith('.'+expected)))

def normalized_host(url):
 try:
  parsed=urlparse(url);host=(parsed.hostname or '').lower().removeprefix('www.')
  return host if parsed.scheme=='https' and host and not any(host==x or host.endswith('.'+x) for x in BLOCKED) else None
 except ValueError:return None

def safe_text(value,secret):
 value=str(value).replace(secret,'[REDACTED]') if secret else str(value)
 return re.sub(r'([?&](?:key|api_key)=)[^&\s]+',r'\1[REDACTED]',value,flags=re.I)

def company_identified(body,ctype,company):
 # PDFs frequently contain plain/UTF-16 text; decoding several safe representations
 # is conservative: an unextractable PDF is queued rather than trusted.
 variants=[body.decode('utf-8','ignore'),body.decode('shift_jis','ignore'),body.decode('utf-16','ignore')]
 needles={str(company['code']),company['name'],company['name'].replace('株式会社','').replace('（株）','')}
 return any(needle and needle in text for needle in needles for text in variants)

def fetch_official(url,company,grounded_urls):
 if url not in grounded_urls or not normalized_host(url):raise ValueError('url_not_https_grounding')
 candidate=normalized_host(url);known=company.get('official_domain')
 if known and not host_allowed(url,company):raise ValueError('official_domain_mismatch')
 req=Request(url,headers={'User-Agent':'kabunushi-yutai-dashboard/1.0 (+official-source-verification)'})
 try:
  with urlopen(req,timeout=20) as response:
   if response.status!=200:raise StageHTTPError('official_url_fetch',response.status,None,'Official URL returned a non-200 response')
   final=response.geturl()
   if normalized_host(final)!=candidate:raise ValueError('redirected_outside_candidate_domain')
   if known and not host_allowed(final,company):raise ValueError('redirected_outside_official_domain')
   body=response.read(2_000_000)
   ctype=response.headers.get_content_type()
 except HTTPError as e:raise http_error(e,'official_url_fetch') from None
 if not company_identified(body,ctype,company):raise ValueError('company_identity_not_found')
 return final,ctype,body,candidate

_last_api_call=0.0
def wait_for_api_interval():
 global _last_api_call
 elapsed=time.monotonic()-_last_api_call
 if elapsed<1:time.sleep(1-elapsed)
 _last_api_call=time.monotonic()

def request_gemini(payload,key,stage,model='gemini-2.5-flash-lite',max_retries=5):
 req=Request(ENDPOINT.format(model=model),data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','x-goog-api-key':key},method='POST')
 for attempt in range(max_retries):
  wait_for_api_interval()
  try:
   with urlopen(req,timeout=90) as r:return json.loads(r.read())
  except HTTPError as e:
   error=http_error(e,stage,key)
   # A grounded request is limited to one attempt per company. A quota error must
   # stop the checkpoint in place instead of consuming another grounded request.
   if e.code==429 or e.code not in (500,502,503,504) or attempt==max_retries-1:raise error from None
   print(f'Gemini {stage}: HTTP {e.code}; retry {attempt + 1}/{max_retries}',file=sys.stderr)
  except (URLError,socket.timeout):
   if attempt==max_retries-1:raise
  time.sleep(min(60,2**attempt+random.random()))

def call_gemini_plain(company,key,model,max_retries=5):
 payload={'contents':[{'role':'user','parts':[{'text':f'証券コード {company["code"]} の会社名だけを復唱してください。検索はしないでください。'}]}]}
 return request_gemini(payload,key,'plain',model,max_retries)

def call_gemini_search(company,key,model='gemini-2.5-flash-lite',max_retries=5):
 prompt=f'''日本の上場会社「{company["name"]}」（証券コード {company["code"]}）の現在の株主優待を調査してください。
Google Searchで次を検索:「会社名 証券コード 株主優待 公式」「会社名 株主優待 IR」「会社名 株主優待制度 PDF」。
{company.get("official_domain") or "企業公式ドメイン"}、企業公式IR/PDF、JPX/TDnetだけを根拠にし、証券会社・まとめ・ブログ・SNSは参照しないでください。値を推測しないでください。
URLは実際に検索結果で確認した直接URLだけ。割引券の価値はnot_calculated。evidence_textは原文の判断箇所を短く要約（200字以内）。確定条件を欠く場合candidateにしてください。'''
 payload={'contents':[{'role':'user','parts':[{'text':prompt}]}],'tools':[{'google_search':{}}],'generationConfig':{}}
 return request_gemini(payload,key,'search',model,max_retries)

def parse_search_response(response,company):
 candidates=response.get('candidates') or []
 if not candidates:raise ValueError('gemini_no_candidate')
 parts=candidates[0].get('content',{}).get('parts',[])
 text=''.join(p.get('text','') for p in parts)
 metadata=candidates[0].get('groundingMetadata',{})
 grounding=metadata.get('groundingChunks',[])
 urls={x.get('web',{}).get('uri') for x in grounding if x.get('web',{}).get('uri')}
 queries=metadata.get('webSearchQueries') or []
 official_urls=sorted(url for url in urls if normalized_host(url))
 return text,urls,queries,official_urls

def call_gemini_structured(company,key,search_text,official_urls,model='gemini-2.5-flash-lite',max_retries=5):
 evidence=json.dumps({'company':{'code':company['code'],'name':company['name']},'search_answer':search_text,'verified_grounding_urls':official_urls},ensure_ascii=False)
 prompt='''次の検索調査結果を指定スキーマへ忠実に変換してください。検索や外部参照はせず、入力にない値は推測しないでください。official_source_urlはverified_grounding_urlsのURLだけを使用してください。\n'''+evidence
 payload={'contents':[{'role':'user','parts':[{'text':prompt}]}],'generationConfig':{'responseMimeType':'application/json','responseJsonSchema':SCHEMA}}
 return request_gemini(payload,key,'structured_extraction',model,max_retries)

def parse_structured_response(response):
 candidates=response.get('candidates') or []
 if not candidates:raise ValueError('gemini_no_candidate')
 text=''.join(p.get('text','') for p in candidates[0].get('content',{}).get('parts',[]))
 return json.loads(text)

def queue_reasons(item,fetch_error=None):
 reasons=[]
 if item.get('confidence_score',0)<90:reasons.append('low_confidence')
 if not item.get('official_source_url'):reasons.append('official_source_not_found')
 if item.get('minimum_shares') is None and item.get('benefit_status')!='abolished':reasons.append('minimum_shares_unknown')
 if not item.get('record_months') and not item.get('record_date') and item.get('benefit_status')!='abolished':reasons.append('record_date_unknown')
 note=(item.get('change_or_abolition_note') or '').lower()
 if '矛盾' in note:reasons.append('conflicting_official_sources')
 if any(x in note for x in ('予定','変更')):reasons.append('planned_change')
 if fetch_error:reasons.append('pdf_parse_failed' if 'pdf' in fetch_error else 'fetch_failed')
 return list(dict.fromkeys(reasons))

def validate(item,company,grounded_urls):
 item={k:item.get(k) for k in FIELDS};item['code']=company['code'];item['name']=company['name'];item['official_verified_at']=dt.date.today().isoformat()
 item['confidence_score']=max(0,min(100,int(item.get('confidence_score') or 0)))
 url=item.get('official_source_url');fetch_error=None
 if url:
  try:
   final,ctype,body,domain=fetch_official(url,company,grounded_urls);item['official_source_url']=final;item['_verified_domain']=domain
  except StageHTTPError:raise
  except Exception as e:item['official_source_url']=None;fetch_error=str(e);item['error_reason']=fetch_error
 required=bool(item.get('official_source_url') and item['confidence_score']>=90 and item.get('minimum_shares') is not None and (item.get('record_months') or item.get('record_date')) and item.get('benefit_description'))
 if item.get('benefit_status')!='abolished' and not required:item['benefit_status']='candidate'
 if item.get('annual_value_yen') is None:item['valuation_type']='not_calculated'
 item['evidence_text']=(item.get('evidence_text') or '')[:200] or None
 return item,queue_reasons(item,fetch_error)

def to_app(item,company,research=None):
 shares=item.get('minimum_shares');desc=item.get('benefit_description') or item.get('benefit_title') or '優待内容 未確認'
 return {'code':item['code'],'name':item['name'],'market':company.get('market',''),'sector':company.get('sector',''),'industry':company.get('sector',''),'benefit_status':item['benefit_status'],'data_confidence':'official_confirmed' if item['benefit_status'] in ('official_confirmed','abolished') else 'candidate','record_months':item.get('record_months') or [],'annual_occurrences':item.get('annual_occurrences') or 0,'minimum_shares':shares,'category':item.get('category') or '未分類','long_term_required':item.get('long_term_required'),'long_term_condition':item.get('conditions') or '未確認','official_source_url':item.get('official_source_url'),'official_source_title':item.get('official_source_title'),'official_verified_at':item.get('official_verified_at'),'confidence_score':item.get('confidence_score'),'evidence_text':item.get('evidence_text'),'abolished_at':item.get('abolished_at'),'last_record_date':item.get('last_record_date'),'change_or_abolition_note':item.get('change_or_abolition_note'),'benefit_tiers':[] if shares is None else [{'shares':shares,'description':desc,'annual_value_yen':item.get('annual_value_yen')}], 'discovery_error':item.get('error_reason'),**(research or {})}

def select(master,args,progress,benefits,queue):
 start=args.start_code;end=args.end_code
 existing={x['code']:x for x in benefits};queued={x.get('code') for x in queue};today=dt.date.today();out=[]
 ordered=master[progress.get('next_index',0):]+master[:progress.get('next_index',0)]
 processed=set(progress.get('processed_codes',[]))
 ordered=sorted(enumerate(ordered),key=lambda pair:(pair[1]['code'] in processed,pair[0]))
 for _,c in ordered:
  code=str(c['code'])
  if start is not None and code<str(start):continue
  if end is not None and code>str(end):continue
  old=existing.get(c['code']);failed=c['code'] in progress.get('failed_codes',[])
  if failed and not args.retry_failed:continue
  if old and old.get('benefit_status') in ('official_confirmed','abolished'):
   try:fresh=(today-dt.date.fromisoformat(old['official_verified_at'])).days<90
   except (ValueError,TypeError,KeyError):fresh=False
   if fresh:continue
  if args.official_only and c['code'] in queued:continue
  out.append(c)
 return out[:min(args.batch_size,args.daily_limit)]

def error_record(error,company):
 if isinstance(error,StageHTTPError):
  return {'code':company['code'],'name':company['name'],'stage':error.stage,'http_status':error.http_status,'api_error_status':error.api_status,'api_error_message':error.api_message,**({'quota':error.quota} if error.http_status==429 else {})}
 return {'code':company['code'],'name':company['name'],'stage':'unknown','http_status':None,'api_error_status':None,'api_error_message':str(error)[:500]}

def run(args):
 key=os.getenv('GEMINI_API_KEY');
 if not key:raise SystemExit('GEMINI_API_KEY is required (it is never persisted or logged)')
 available=list_models(key)
 print(f'Available Gemini models: {len(available)}件',file=sys.stderr)
 try:search_model,extraction_model,_=select_models(available,key)
 except StageHTTPError as error:
  print(f'Model probe stopped: HTTP {error.http_status} {error.api_status or ""} {safe_text(error.api_message,key)[:300]}',file=sys.stderr)
  return 1
 print(f'Selected search model: {search_model or "unavailable"}',file=sys.stderr)
 print(f'Selected extraction model: {extraction_model or "unavailable"}',file=sys.stderr)
 if not search_model:
  print('このAPIキーでは無料のGoogle検索対応モデルを利用できません。\n課金設定を行うか、検索なしモードへ切り替えてください。',file=sys.stderr);return 1
 if not extraction_model:
  print('構造化抽出に利用できるGeminiモデルがありません。',file=sys.stderr);return 1
 master=load(DATA/'listed-companies.json',[]);benefits=load(DATA/'benefits.json',[]);queue=load(DATA/'verification-queue.json',[]);progress=load(DATA/'discovery-progress.json',{})
 if len(master)<=12:raise SystemExit('上場会社マスターが未更新です（listed-companies.json は13社以上必要です）')
 if len(master)!=len({x['code'] for x in master}):raise SystemExit('duplicate code in listed-companies.json')
 domains=load(DATA/'company-domains.json',{});master_by_code={x['code']:x for x in master}
 for code,domain in domains.items():
  if code in master_by_code and not master_by_code[code].get('official_domain'):master_by_code[code]['official_domain']=domain
 today=dt.date.today().isoformat();usage=load(DATA/'api-usage.json',[]);used_today=sum(x.get('processed_companies',0) for x in usage if str(x.get('executed_at','')).startswith(today));args.daily_limit=max(0,args.daily_limit-used_today);args.batch_size=min(100,max(0,args.batch_size));selected=select(master,args,progress,benefits,queue)
 if args.diagnostic_mode:
  kyokuyo=next((company for company in master if str(company.get('code'))=='1301'),None)
  if not kyokuyo:raise SystemExit('診断対象の極洋（1301）がlisted-companies.jsonに存在しません。')
  selected=[kyokuyo]
 existing={x['code']:x for x in benefits};preserved={c for c,x in existing.items() if x.get('benefit_status') in ('official_confirmed','abolished')};started=time.monotonic();calls=success=failed=0;errors=[];previous_error=None;consecutive_errors=0;processed=0;diagnostic_result=None;model_unavailable=False
 for index,company in enumerate(selected):
  current_error=None
  try:
   if args.diagnostic_mode:
    calls+=1;call_gemini_plain(company,key,search_model);print('Plain request: success',file=sys.stderr)
   calls+=1;search_raw=call_gemini_search(company,key,search_model)
   search_text,urls,queries,official_urls=parse_search_response(search_raw,company)
   if args.diagnostic_mode:print('Google Search grounding: success',file=sys.stderr)
   print(f'{company["code"]} {company["name"]}: search success ({len(urls)} grounded URLs)',file=sys.stderr)
   calls+=1;structured_raw=call_gemini_structured(company,key,search_text,official_urls,extraction_model)
   item=parse_structured_response(structured_raw);item,reasons=validate(item,company,urls)
   if args.diagnostic_mode:print('Structured extraction: success',file=sys.stderr)
   print(f'{company["code"]} {company["name"]}: structured_extraction success; validation complete',file=sys.stderr)
   verified_domain=item.pop('_verified_domain',None)
   if not args.diagnostic_mode and verified_domain and not any(verified_domain==host or verified_domain.endswith('.'+host) for host in OFFICIAL_HOSTS):domains[company['code']]=verified_domain
   research={'search_answer':safe_text(search_text,key),'grounded_urls':[safe_text(x,key) for x in sorted(urls)],'search_queries':[safe_text(x,key) for x in queries],'official_url_candidates':[safe_text(x,key) for x in official_urls]}
   diagnostic_result={**research,'code':company['code'],'name':company['name'],'search':'success','structured_extraction':'success','validation':'success'}
   # Never overwrite pre-existing confirmed or abolished records.
   if not args.diagnostic_mode and company['code'] not in preserved:existing[company['code']]=to_app(item,company,research)
   if not args.diagnostic_mode:queue=[q for q in queue if q.get('code')!=company['code']]
   if reasons and not args.diagnostic_mode:queue.append({**item,**research,'verification_reasons':reasons,'result':'verification_required'})
   else:success+=1
   if not args.diagnostic_mode:progress['failed_codes']=[x for x in progress.get('failed_codes',[]) if x!=company['code']]
   previous_error=None;consecutive_errors=0
  except Exception as e:
   current_error=e
   if isinstance(e,StageHTTPError) and e.http_status==429:
    invalidate_model_cache()
    detail=error_record(e,company);errors.append(detail);failed+=1
    print(f'Gemini {e.stage}: HTTP 429; quota={json.dumps(e.quota,ensure_ascii=False)}',file=sys.stderr)
    if e.stage=='search':print('このAPIキーでは無料のGoogle検索対応モデルを利用できません。\n課金設定を行うか、検索なしモードへ切り替えてください。',file=sys.stderr)
    else:print(f'Gemini {e.stage} の割り当て上限に達したため、処理位置を変更せず停止します。',file=sys.stderr)
    # Do not mark the company failed and do not advance/persist its checkpoint.
    model_unavailable=True;break
   if isinstance(e,StageHTTPError) and e.http_status==404 and e.api_status=='NOT_FOUND':
    invalidate_model_cache()
    model_unavailable=True
    print('選択されたモデルが利用できません。',file=sys.stderr)
    break
   if isinstance(e,StageHTTPError) and e.http_status==403:invalidate_model_cache()
   failed+=1
   detail=error_record(e,company);errors.append(detail);signature=(detail['http_status'],detail['api_error_status'],detail['api_error_message'])
   consecutive_errors=consecutive_errors+1 if signature==previous_error else 1;previous_error=signature
   print(f'{company["code"]} {company["name"]}: {detail["stage"]} failed; HTTP {detail["http_status"]}; Gemini status={detail["api_error_status"]}; message={detail["api_error_message"]}',file=sys.stderr)
   if not args.diagnostic_mode:
    if company['code'] not in progress.get('failed_codes',[]):progress.setdefault('failed_codes',[]).append(company['code'])
    queue=[q for q in queue if q.get('code')!=company['code']]+[{**detail,'confidence_score':0,'result':'failed','error_reason':type(e).__name__,'verification_reasons':['fetch_failed']}]
  processed+=1
  if not args.diagnostic_mode:
   progress.setdefault('processed_codes',[]).append(company['code']);progress['processed_codes']=list(dict.fromkeys(progress['processed_codes']));progress['next_index']=(master.index(company)+1)%max(1,len(master));progress['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
   progress['total_companies']=len(master);progress['uninvestigated_count']=len(master)-len(set(progress['processed_codes']));progress['status_counts']={'uninvestigated':progress['uninvestigated_count'],'processed':len(progress['processed_codes']),'candidate':sum(q.get('result')!='failed' for q in queue),'official_confirmed':sum(x.get('benefit_status')=='official_confirmed' for x in existing.values()),'abolished':sum(x.get('benefit_status')=='abolished' for x in existing.values()),'failed':len(progress.get('failed_codes',[]))}
   atomic(DATA/'benefits.json',list(existing.values()));atomic(DATA/'verification-queue.json',queue);atomic(DATA/'discovery-progress.json',progress);atomic(DATA/'company-domains.json',domains)
  stop=index==0 and isinstance(current_error,StageHTTPError) and current_error.http_status in (400,401,403)
  # A repeated deterministic failure is unlikely to improve for the remaining companies.
  if failed and (stop or consecutive_errors>=3):
   print(f'Stopping run after {processed} company/companies to prevent repeated failures.',file=sys.stderr);break
 usage.append({'executed_at':dt.datetime.now(dt.timezone.utc).isoformat(),'diagnostic_mode':args.diagnostic_mode,'diagnostic_result':diagnostic_result if args.diagnostic_mode else None,'processed_companies':processed,'gemini_api_calls':calls,'successes':success,'verification_required':processed-success-failed,'failures':failed,'errors':errors,'duration_seconds':round(time.monotonic()-started,2)});atomic(DATA/'api-usage.json',usage[-365:])
 return 1 if failed or model_unavailable else 0

def parser():
 p=argparse.ArgumentParser();p.add_argument('--batch-size',type=int,default=100);p.add_argument('--daily-limit',type=int,default=100);p.add_argument('--start-code');p.add_argument('--end-code');p.add_argument('--retry-failed',action='store_true');p.add_argument('--official-only',action='store_true');p.add_argument('--diagnostic-mode',action='store_true');return p
if __name__=='__main__':sys.exit(run(parser().parse_args()))
