#!/usr/bin/env python3
"""優待CSVを検証し、JSONへ変換する。"""
import argparse, csv, datetime as dt, json, re
from pathlib import Path
from urllib.parse import urlparse

REQUIRED = {
    'code', 'name', 'market', 'industry', 'category', 'record_months',
    'long_term_condition', 'benefit_status', 'official_verified_at',
    'official_source_url', 'abolished_at', 'last_record_date',
    'data_confidence', 'annual_occurrences', 'change_or_abolition_note',
    'benefit_tiers_json'
}
STATUSES = {'active', 'scheduled', 'changed', 'abolished', 'unverified', 'official_confirmed', 'candidate'}
CONFIDENCE = {'official_confirmed', 'partially_confirmed', 'unverified', 'candidate'}
NULLABLE = {'abolished_at', 'last_record_date', 'official_verified_at', 'official_source_url'}

def convert(source: Path, destination: Path):
    with source.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows or not REQUIRED.issubset(rows[0]):
        raise ValueError('CSVの必須列が不足しています')
    result = []
    for row in rows:
        if row['benefit_status'] not in STATUSES or row['data_confidence'] not in CONFIDENCE:
            raise ValueError(f"{row['code']}: ステータスまたは確認レベルが不正です")
        if row['benefit_status'] == 'candidate':
            url = urlparse(row['official_source_url'])
            if (not re.fullmatch(r'\d{4}', row['code']) or not row['name'].strip()
                    or row['name'].startswith(('公式確認待ち銘柄', '名称未確認', '未確認'))
                    or not row['market'].strip() or row['market'] == '未確認'
                    or url.scheme != 'https' or not url.netloc or not row['record_months']):
                raise ValueError(f"{row['code']}: 候補には正式会社名・市場・具体的URL・権利月が必要です")
            try: dt.date.fromisoformat(row['official_verified_at'])
            except ValueError: raise ValueError(f"{row['code']}: 候補には最終確認日が必要です") from None
        item = {k: (None if k in NULLABLE and not row[k] else row[k])
                for k in REQUIRED - {'benefit_tiers_json', 'record_months', 'annual_occurrences'}}
        item['record_months'] = [int(x) for x in row['record_months'].split('|') if x]
        item['annual_occurrences'] = int(row['annual_occurrences'])
        item['benefit_tiers'] = json.loads(row['benefit_tiers_json'])
        result.append(item)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='data/benefits.csv')
    parser.add_argument('--output', default='data/benefits.json')
    args = parser.parse_args()
    convert(Path(args.input), Path(args.output))
