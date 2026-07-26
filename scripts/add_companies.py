#!/usr/bin/env python3
"""Add JPX-master companies to the OpenAI investigation queue."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def add_companies(codes, data_dir=DATA) -> int:
    """Queue valid, known and not-yet-queued codes; return a shell-style status."""
    master = load_json(data_dir / "listed-companies.json")
    by_code = {company["code"]: company for company in master}
    if len(by_code) != len(master):
        print("エラー: 上場会社マスターに証券コードの重複があります。", file=sys.stderr)
        return 1

    queue_path = data_dir / "verification-queue.json"
    queue = load_json(queue_path)
    benefits = load_json(data_dir / "benefits.json")
    known = {str(item.get("code")) for item in queue + benefits}
    added = []
    had_error = False
    for code in dict.fromkeys(codes):
        if not re.fullmatch(r"\d{4}", code):
            print(f"エラー: 証券コードは4桁の数字で指定してください: {code}", file=sys.stderr)
            had_error = True
            continue
        company = by_code.get(code)
        if company is None:
            print(f"エラー: 上場会社マスターに存在しない証券コードです: {code}", file=sys.stderr)
            had_error = True
            continue
        if code in known:
            print(f"スキップ: {code} は既に登録済みです。")
            continue
        item = {
            "code": code,
            "name": company["name"],
            "market": company.get("market"),
            "sector": company.get("sector"),
            "result": "pending",
            "verification_reasons": ["not_investigated"],
        }
        queue.append(item)
        known.add(code)
        added.append(item)

    if added:
        atomic_json(queue_path, queue)
        for item in added:
            print(f"追加: {item['code']} {item['name']}（{item['market']} / {item['sector']}）")
    print(f"調査対象への追加: {len(added)}社")
    return 1 if had_error else 0


def parser():
    result = argparse.ArgumentParser(description="証券コードをOpenAI株主優待調査の対象へ追加します。")
    result.add_argument("codes", nargs="+", help="4桁の証券コード（複数指定可）")
    return result


if __name__ == "__main__":
    raise SystemExit(add_companies(parser().parse_args().codes))
