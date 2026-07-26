#!/usr/bin/env python3
"""候補台帳を優待マスターへ安全に統合し、確認キューを生成する。"""
import argparse, csv, datetime as dt, json
from pathlib import Path

def _months(value): return [int(x) for x in (value or '').replace(',', '|').split('|') if x.strip()]

def merge(master_path, universe_path, output_path, queue_path, today=None):
    today = today or dt.date.today()
    master = json.loads(Path(master_path).read_text(encoding='utf-8'))
    by_code = {str(x['code']): x for x in master}
    with Path(universe_path).open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        code = row['code'].strip()
        if not code or code in by_code: continue
        months = _months(row.get('record_months'))
        by_code[code] = {'code':code,'name':row.get('name','').strip() or '名称未確認','market':row.get('market','').strip() or '未確認','sector':row.get('sector','').strip() or None,'industry':row.get('sector','').strip() or '未確認','benefit_status':'candidate','data_confidence':'candidate','record_months':months,'minimum_shares':None,'benefit_summary':None,'benefit_tiers':[],'long_term_required':None,'long_term_condition':None,'official_source_url':None,'official_verified_at':None,'last_checked_at':row.get('last_checked_at','').strip() or None,'abolished_at':None,'last_record_date':None,'notes':'優待の実施有無・詳細条件を企業公式IRで確認するまで候補扱い。','source_hint':row.get('source_hint','').strip() or None,'category':'未確認','annual_occurrences':len(months),'change_or_abolition_note':None}
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
