#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from market_data import JQuantsProvider, update_market_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="KVへ送信する一時JSONの出力先")
    parser.add_argument("--previous", help="KVから取得した直前のJSON")
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    benefits = json.loads((root / "data/benefits.json").read_text())
    previous = {}
    if args.previous and Path(args.previous).exists():
        try:
            previous = json.loads(Path(args.previous).read_text())
        except json.JSONDecodeError:
            pass
    updated, failures = update_market_data(
        [item["code"] for item in benefits],
        JQuantsProvider(os.environ.get("JQUANTS_API_KEY")),
        previous,
    )
    if not updated:
        raise RuntimeError("株価を1件も取得できなかったためKVを更新しません")
    Path(args.output).write_text(json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n")
    if failures:
        print(f"warning: {len(failures)}銘柄の取得に失敗（直前値がある場合は保持）")


if __name__ == "__main__":
    main()
