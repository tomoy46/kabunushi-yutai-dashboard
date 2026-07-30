#!/usr/bin/env python3
"""GitHub Pages の benefits.json がリポジトリの内容と一致するまで確認する。"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def data_metrics(items):
    codes = {str(item["code"]) for item in items}
    confirmed = sum(item.get("benefit_status") == "official_confirmed" for item in items)
    return codes, confirmed


def compare_data(expected, published):
    expected_codes, expected_confirmed = data_metrics(expected)
    published_codes, published_confirmed = data_metrics(published)
    return {
        "repository_count": expected_confirmed,
        "published_count": published_confirmed,
        "missing_codes": sorted(expected_codes - published_codes),
        "extra_codes": sorted(published_codes - expected_codes),
        "matches": (expected_confirmed == published_confirmed
                    and expected_codes == published_codes),
    }


def verify(url, expected_path, attempts=12, interval=10):
    expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    last = None
    for attempt in range(1, attempts + 1):
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        cache_busting_url = f"{url}{separator}timestamp={time.time_ns()}"
        try:
            request = urllib.request.Request(cache_busting_url,
                                             headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                published = json.load(response)
            last = compare_data(expected, published)
            if last["matches"]:
                print(f"公開確認成功（{attempt}回目）: 公式確認済み {last['published_count']}件")
                return last
        except (OSError, ValueError, urllib.error.HTTPError, RuntimeError) as error:
            print(f"公開確認 {attempt}/{attempts} 失敗: {error}")
        if attempt < attempts:
            time.sleep(interval)
    last = last or {"repository_count": data_metrics(expected)[1], "published_count": 0,
                    "missing_codes": sorted(data_metrics(expected)[0]), "extra_codes": []}
    print(f"リポジトリ内の件数: {last['repository_count']}")
    print(f"公開サイトの件数: {last['published_count']}")
    print("不足している証券コード: " + (", ".join(last["missing_codes"]) or "なし"))
    print("余分な証券コード: " + (", ".join(last["extra_codes"]) or "なし"))
    raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected", default="data/benefits.json")
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    result = verify(args.url, args.expected, args.attempts, args.interval)
    output = Path("pages-verification.json")
    output.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
