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


def add_companies(codes, data_dir=DATA, from_code=None, to_code=None) -> int:
    """Queue valid, known and not-yet-queued codes; return a shell-style status."""
    if (from_code is None) != (to_code is None):
        print("エラー: 範囲指定には --from と --to の両方が必要です。", file=sys.stderr)
        return 1
    if from_code is not None:
        if not re.fullmatch(r"\d{4}", from_code) or not re.fullmatch(r"\d{4}", to_code):
            print("エラー: --from と --to は4桁の証券コードで指定してください。", file=sys.stderr)
            return 1
        if int(from_code) > int(to_code):
            print(f"エラー: 範囲の開始は終了以下にしてください: {from_code} > {to_code}", file=sys.stderr)
            return 1

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
    requested_codes = list(codes)
    if from_code is not None:
        requested_codes.extend(
            company["code"] for company in master
            if re.fullmatch(r"\d{4}", company["code"])
            and int(from_code) <= int(company["code"]) <= int(to_code)
        )

    for code in dict.fromkeys(requested_codes):
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
    print(f"追加した証券コード: {', '.join(item['code'] for item in added) or 'なし'}")
    return 1 if had_error else 0


def parser():
    result = argparse.ArgumentParser(description="証券コードをOpenAI株主優待調査の対象へ追加します。")
    result.add_argument("codes", nargs="*", help="4桁の証券コード（複数指定可）")
    result.add_argument("--from", dest="from_code", help="追加する証券コード範囲の開始（4桁）")
    result.add_argument("--to", dest="to_code", help="追加する証券コード範囲の終了（4桁）")
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    if not args.codes and args.from_code is None and args.to_code is None:
        parser().error("証券コード、または --from と --to による範囲を指定してください。")
    raise SystemExit(add_companies(args.codes, from_code=args.from_code, to_code=args.to_code))
