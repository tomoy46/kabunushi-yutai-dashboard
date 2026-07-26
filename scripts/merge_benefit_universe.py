#!/usr/bin/env python3
"""候補台帳を優待マスターへ安全に統合し、確認キューを生成する。"""
import argparse, csv, datetime as dt, json, re
from pathlib import Path
from urllib.parse import urlparse

def _months(value): return [int(x) for x in (value or '').replace(',', '|').split('|') if x.strip()]

PLACEHOLDER_NAME = re.compile(r'^(公式確認待ち銘柄|名称未確認|未確認)')

def validate_candidate(row):
    """Reject speculative candidates before they can enter the master data."""
    code, name = row.get('code', '').strip(), row.get('name', '').strip()
    market = row.get('market', '').strip()
    source = row.get('source_hint', '').strip()
    checked = row.get('last_checked_at', '').strip()
    months = _months(row.get('record_months'))
    parsed = urlparse(source)
    if not re.fullmatch(r'\d{4}', code): raise ValueError(f'{code or "(空)"}: 実在する4桁の証券コードが必要です')
    if not name or PLACEHOLDER_NAME.match(name): raise ValueError(f'{code}: 正式な会社名が必要です')
    if not market or market == '未確認': raise ValueError(f'{code}: 市場区分が必要です')
    if parsed.scheme != 'https' or not parsed.netloc: raise ValueError(f'{code}: 具体的な情報源URLが必要です')
    if not months or any(month < 1 or month > 12 for month in months): raise ValueError(f'{code}: 根拠を確認した権利確定月が必要です')
    try: dt.date.fromisoformat(checked)
    except ValueError: raise ValueError(f'{code}: 最終確認日（YYYY-MM-DD）が必要です') from None

def merge(master_path, universe_path, output_path, queue_path, today=None):
    today = today or dt.date.today()
    master = json.loads(Path(master_path).read_text(encoding='utf-8'))
    by_code = {str(x['code']): x for x in master}
    with Path(universe_path).open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        code = row['code'].strip()
        if not code or code in by_code: continue
        validate_candidate(row)
        months = _months(row.get('record_months'))
        by_code[code] = {'code':code,'name':row['name'].strip(),'market':row['market'].strip(),'sector':row.get('sector','').strip() or None,'industry':row.get('sector','').strip() or '未確認','benefit_status':'candidate','data_confidence':'candidate','record_months':months,'minimum_shares':None,'benefit_summary':None,'benefit_tiers':[],'long_term_required':None,'long_term_condition':None,'official_source_url':row['source_hint'].strip(),'official_verified_at':None,'last_checked_at':row['last_checked_at'].strip(),'abolished_at':None,'last_record_date':None,'notes':'具体的な公式情報源を確認済み。優待条件の確定まで候補扱い。','source_hint':row['source_hint'].strip(),'category':'未確認','annual_occurrences':len(months),'change_or_abolition_note':None}
    result=sorted(by_code.values(),key=lambda x:x['code'])
    if len({x['code'] for x in result}) != len(result): raise ValueError('証券コードが重複しています')
    Path(output_path).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    upcoming=[(today.month-1+i)%12+1 for i in range(3)]
    def priority(x):
        hits=[i+1 for i,m in enumerate(upcoming) if m in x.get('record_months',[])]
        return min(hits) if hits else (4 if x.get('disclosures') else 5)
    queue=[{'code':x['code'],'name':x['name'],'record_months':x.get('record_months',[]),'benefit_status':x['benefit_status'],'last_checked_at':x.get('last_checked_at'),'official_page_candidate':x.get('official_source_url') or x.get('source_hint'),'related_disclosures':x.get('disclosures',[]),'priority':priority(x)} for x in result if x['benefit_status']=='candidate']
    queue.sort(key=lambda x:(x['priority'],x['code']))
    Path(queue_path).write_text(json.dumps(queue,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result,queue

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--master',default='data/benefits.json');p.add_argument('--universe',default='data/benefit-universe.csv');p.add_argument('--output',default='data/benefits.json');p.add_argument('--queue',default='data/verification-queue.json');a=p.parse_args();merge(a.master,a.universe,a.output,a.queue)
