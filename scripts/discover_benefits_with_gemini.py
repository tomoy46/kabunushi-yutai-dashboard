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
MODEL=os.getenv('GEMINI_MODEL','gemini-2.5-flash')
ENDPOINT='https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
OFFICIAL_HOSTS=('jpx.co.jp','tdnet.info')
BLOCKED=('yahoo.co.jp','minkabu.jp','kabutan.jp','rakuten-sec.co.jp','sbisec.co.jp','monex.co.jp','note.com','x.com','facebook.com','instagram.com','youtube.com')
FIELDS={'code':{'type':'string'},'name':{'type':'string'},'benefit_status':{'type':'string','enum':['official_confirmed','candidate','abolished']},'record_months':{'type':'array','items':{'type':'integer'}},'record_date':{'type':['string','null']},'annual_occurrences':{'type':['integer','null']},'minimum_shares':{'type':['integer','null']},'maximum_shares':{'type':['integer','null']},'benefit_title':{'type':['string','null']},'benefit_description':{'type':['string','null']},'category':{'type':['string','null']},'annual_value_yen':{'type':['integer','null']},'valuation_type':{'type':'string','enum':['official_amount','not_calculated']},'long_term_required':{'type':['boolean','null']},'holding_period_months':{'type':['integer','null']},'conditions':{'type':['string','null']},'official_source_url':{'type':['string','null']},'official_source_title':{'type':['string','null']},'official_verified_at':{'type':'string'},'abolished_at':{'type':['string','null']},'last_record_date':{'type':['string','null']},'change_or_abolition_note':{'type':['string','null']},'confidence_score':{'type':'integer','minimum':0,'maximum':100},'evidence_text':{'type':['string','null']},'error_reason':{'type':['string','null']}}
SCHEMA={'type':'object','properties':FIELDS,'required':list(FIELDS),'additionalProperties':False}
QUEUE_REASONS={'low_confidence','official_source_not_found','minimum_shares_unknown','record_date_unknown','conflicting_official_sources','planned_change','pdf_parse_failed','fetch_failed'}

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
 with urlopen(req,timeout=20) as response:
  if response.status!=200:raise ValueError(f'http_{response.status}')
  final=response.geturl()
  if normalized_host(final)!=candidate:raise ValueError('redirected_outside_candidate_domain')
  if known and not host_allowed(final,company):raise ValueError('redirected_outside_official_domain')
  body=response.read(2_000_000)
  ctype=response.headers.get_content_type()
 if not company_identified(body,ctype,company):raise ValueError('company_identity_not_found')
 return final,ctype,body,candidate

def call_gemini(company,key,max_retries=5):
 prompt=f'''日本の上場会社「{company["name"]}」（証券コード {company["code"]}）の現在の株主優待を調査してください。
Google Searchで次を検索:「会社名 証券コード 株主優待 公式」「会社名 株主優待 IR」「会社名 株主優待制度 PDF」。
{company.get("official_domain") or "企業公式ドメイン"}、企業公式IR/PDF、JPX/TDnetだけを根拠にし、証券会社・まとめ・ブログ・SNSは参照しないでください。値を推測しないでください。
URLは実際に検索結果で確認した直接URLだけ。割引券の価値はnot_calculated。evidence_textは原文の判断箇所を短く要約（200字以内）。確定条件を欠く場合candidateにしてください。'''
 payload={'contents':[{'role':'user','parts':[{'text':prompt}]}],'tools':[{'google_search':{}}],'generationConfig':{'responseMimeType':'application/json','responseJsonSchema':SCHEMA,'temperature':0}}
 req=Request(ENDPOINT.format(model=MODEL),data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','x-goog-api-key':key},method='POST')
 for attempt in range(max_retries):
  try:
   with urlopen(req,timeout=90) as r:return json.loads(r.read())
  except HTTPError as e:
   if e.code not in (429,500,502,503,504) or attempt==max_retries-1:raise
  except (URLError,socket.timeout):
   if attempt==max_retries-1:raise
  time.sleep(min(60,2**attempt+random.random()))

def parse_response(response):
 candidates=response.get('candidates') or []
 if not candidates:raise ValueError('gemini_no_candidate')
 parts=candidates[0].get('content',{}).get('parts',[])
 text=''.join(p.get('text','') for p in parts)
 result=json.loads(text)
 grounding=candidates[0].get('groundingMetadata',{}).get('groundingChunks',[])
 urls={x.get('web',{}).get('uri') for x in grounding if x.get('web',{}).get('uri')}
 return result,urls

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
  except Exception as e:item['official_source_url']=None;fetch_error=str(e);item['error_reason']=fetch_error
 required=bool(item.get('official_source_url') and item['confidence_score']>=90 and item.get('minimum_shares') is not None and (item.get('record_months') or item.get('record_date')) and item.get('benefit_description'))
 if item.get('benefit_status')!='abolished' and not required:item['benefit_status']='candidate'
 if item.get('annual_value_yen') is None:item['valuation_type']='not_calculated'
 item['evidence_text']=(item.get('evidence_text') or '')[:200] or None
 return item,queue_reasons(item,fetch_error)

def to_app(item,company):
 shares=item.get('minimum_shares');desc=item.get('benefit_description') or item.get('benefit_title') or '優待内容 未確認'
 return {'code':item['code'],'name':item['name'],'market':company.get('market',''),'sector':company.get('sector',''),'industry':company.get('sector',''),'benefit_status':item['benefit_status'],'data_confidence':'official_confirmed' if item['benefit_status'] in ('official_confirmed','abolished') else 'candidate','record_months':item.get('record_months') or [],'annual_occurrences':item.get('annual_occurrences') or 0,'minimum_shares':shares,'category':item.get('category') or '未分類','long_term_required':item.get('long_term_required'),'long_term_condition':item.get('conditions') or '未確認','official_source_url':item.get('official_source_url'),'official_source_title':item.get('official_source_title'),'official_verified_at':item.get('official_verified_at'),'confidence_score':item.get('confidence_score'),'evidence_text':item.get('evidence_text'),'abolished_at':item.get('abolished_at'),'last_record_date':item.get('last_record_date'),'change_or_abolition_note':item.get('change_or_abolition_note'),'benefit_tiers':[] if shares is None else [{'shares':shares,'description':desc,'annual_value_yen':item.get('annual_value_yen')}], 'discovery_error':item.get('error_reason')}

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

def run(args):
 key=os.getenv('GEMINI_API_KEY');
 if not key:raise SystemExit('GEMINI_API_KEY is required (it is never persisted or logged)')
 master=load(DATA/'listed-companies.json',[]);benefits=load(DATA/'benefits.json',[]);queue=load(DATA/'verification-queue.json',[]);progress=load(DATA/'discovery-progress.json',{})
 if len(master)<=12:raise SystemExit('上場会社マスターが未更新です（listed-companies.json は13社以上必要です）')
 if len(master)!=len({x['code'] for x in master}):raise SystemExit('duplicate code in listed-companies.json')
 domains=load(DATA/'company-domains.json',{});master_by_code={x['code']:x for x in master}
 for code,domain in domains.items():
  if code in master_by_code and not master_by_code[code].get('official_domain'):master_by_code[code]['official_domain']=domain
 today=dt.date.today().isoformat();usage=load(DATA/'api-usage.json',[]);used_today=sum(x.get('processed_companies',0) for x in usage if str(x.get('executed_at','')).startswith(today));args.daily_limit=max(0,args.daily_limit-used_today);args.batch_size=min(100,max(0,args.batch_size));selected=select(master,args,progress,benefits,queue);existing={x['code']:x for x in benefits};preserved={c for c,x in existing.items() if x.get('benefit_status') in ('official_confirmed','abolished')};started=time.monotonic();calls=success=failed=0
 for company in selected:
  calls+=1
  try:
   raw=call_gemini(company,key);item,urls=parse_response(raw);item,reasons=validate(item,company,urls)
   verified_domain=item.pop('_verified_domain',None)
   if verified_domain and not any(verified_domain==host or verified_domain.endswith('.'+host) for host in OFFICIAL_HOSTS):domains[company['code']]=verified_domain
   # Never overwrite pre-existing confirmed or abolished records.
   if company['code'] not in preserved:existing[company['code']]=to_app(item,company)
   queue=[q for q in queue if q.get('code')!=company['code']]
   if reasons:queue.append({**item,'verification_reasons':reasons,'result':'verification_required'})
   else:success+=1
   progress['failed_codes']=[x for x in progress.get('failed_codes',[]) if x!=company['code']]
  except Exception as e:
   failed+=1
   if company['code'] not in progress.get('failed_codes',[]):progress.setdefault('failed_codes',[]).append(company['code'])
   queue=[q for q in queue if q.get('code')!=company['code']]+[{'code':company['code'],'name':company['name'],'confidence_score':0,'result':'failed','error_reason':type(e).__name__,'verification_reasons':['fetch_failed']}]
  progress.setdefault('processed_codes',[]).append(company['code']);progress['processed_codes']=list(dict.fromkeys(progress['processed_codes']));progress['next_index']=(master.index(company)+1)%max(1,len(master));progress['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
  progress['total_companies']=len(master);progress['uninvestigated_count']=len(master)-len(set(progress['processed_codes']));progress['status_counts']={'uninvestigated':progress['uninvestigated_count'],'processed':len(progress['processed_codes']),'candidate':sum(q.get('result')!='failed' for q in queue),'official_confirmed':sum(x.get('benefit_status')=='official_confirmed' for x in existing.values()),'abolished':sum(x.get('benefit_status')=='abolished' for x in existing.values()),'failed':len(progress.get('failed_codes',[]))}
  atomic(DATA/'benefits.json',list(existing.values()));atomic(DATA/'verification-queue.json',queue);atomic(DATA/'discovery-progress.json',progress);atomic(DATA/'company-domains.json',domains)
 usage.append({'executed_at':dt.datetime.now(dt.timezone.utc).isoformat(),'processed_companies':len(selected),'gemini_api_calls':calls,'successes':success,'verification_required':len(selected)-success-failed,'failures':failed,'duration_seconds':round(time.monotonic()-started,2)});atomic(DATA/'api-usage.json',usage[-365:])
 return 1 if failed else 0

def parser():
 p=argparse.ArgumentParser();p.add_argument('--batch-size',type=int,default=100);p.add_argument('--daily-limit',type=int,default=100);p.add_argument('--start-code');p.add_argument('--end-code');p.add_argument('--retry-failed',action='store_true');p.add_argument('--official-only',action='store_true');return p
if __name__=='__main__':sys.exit(run(parser().parse_args()))
