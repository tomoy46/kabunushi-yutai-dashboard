#!/usr/bin/env python3
"""TDnet RSS/XMLから優待関連開示をレビューキューへ追加する（マスターは変更しない）。"""
import argparse,json,urllib.request,xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
KEYWORDS=('株主優待','優待制度','優待内容','優待品','記念優待')
def extract(xml_bytes):
    root=ET.fromstring(xml_bytes); results=[]
    for item in root.findall('.//item'):
        title=(item.findtext('title') or '').strip()
        if any(k in title for k in KEYWORDS): results.append({'title':title,'url':item.findtext('link') or '', 'published_at':item.findtext('pubDate') or '', 'matched_keywords':[k for k in KEYWORDS if k in title],'status':'pending'})
    return results
def merge_queue(queue,new):
    urls={x['url'] for x in queue};return queue+[x for x in new if x['url'] not in urls]
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--feed-url');p.add_argument('--input');p.add_argument('--queue',default='data/review-queue.json');a=p.parse_args()
    if not (a.feed_url or a.input): p.error('--feed-url または --input が必要です')
    raw=Path(a.input).read_bytes() if a.input else urllib.request.urlopen(a.feed_url,timeout=30).read()
    path=Path(a.queue);old=json.loads(path.read_text()) if path.exists() else []; additions=extract(raw)
    now=datetime.now(ZoneInfo('Asia/Tokyo')).isoformat()
    for x in additions:x['detected_at']=now
    path.write_text(json.dumps(merge_queue(old,additions),ensure_ascii=False,indent=2)+'\n')
