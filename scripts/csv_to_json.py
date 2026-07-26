#!/usr/bin/env python3
"""優待CSVを検証し、JSONへ変換する。"""
import argparse, csv, json
from pathlib import Path

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
