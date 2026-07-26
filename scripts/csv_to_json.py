#!/usr/bin/env python3
"""優待CSVを検証し、JSONへ変換する。"""
import argparse,csv,json
from pathlib import Path

def convert(source: Path, destination: Path):
    with source.open(encoding='utf-8-sig', newline='') as f: rows=list(csv.DictReader(f))
    required={'code','name','market','industry','category','record_months','long_term_condition','official_url','info_checked_at','benefit_tiers_json'}
    if not rows or not required.issubset(rows[0]): raise ValueError('CSVの必須列が不足しています')
    result=[]
    for row in rows:
        result.append({**{k:row[k] for k in required-{'benefit_tiers_json','record_months'}},'record_months':[int(x) for x in row['record_months'].split('|')],'benefit_tiers':json.loads(row['benefit_tiers_json'])})
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',default='data/benefits.csv');p.add_argument('--output',default='data/benefits.json');a=p.parse_args();convert(Path(a.input),Path(a.output))
