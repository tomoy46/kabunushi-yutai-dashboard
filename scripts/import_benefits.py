#!/usr/bin/env python3
"""初期株主優待CSVを検証し、公開用CSV/JSONへ重複なしで取り込む。"""

import argparse
import csv
import datetime as dt
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

IMPORT_COLUMNS = [
    "security_code", "company_name", "benefit_summary", "required_shares",
    "record_month", "long_term_condition", "official_url", "status",
    "source_checked_date",
]
MASTER_COLUMNS = [
    "code", "name", "market", "industry", "category", "record_months",
    "long_term_condition", "benefit_status", "official_verified_at",
    "official_source_url", "abolished_at", "last_record_date",
    "data_confidence", "annual_occurrences", "change_or_abolition_note",
    "benefit_tiers_json",
]
ALLOWED_STATUSES = {"confirmed", "abolished"}
CODE_PATTERN = re.compile(r"(?:\d{4}|\d{3}[A-Z])")


class ImportValidationError(ValueError):
    """CSV全体を変更なしで拒否する検証エラー。"""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("\n".join(errors))


def read_and_validate(path: Path):
    errors = []
    try:
        source = path.open(encoding="utf-8-sig", newline="")
        source.read()
        source.seek(0)
    except (UnicodeDecodeError, OSError) as error:
        raise ImportValidationError([f"CSVをUTF-8として読み込めません: {error}"]) from error
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != IMPORT_COLUMNS:
            raise ImportValidationError([
                "CSV列が不正です（列名と順序をテンプレートに合わせてください）"
            ])
        rows = []
        seen = set()
        for line, raw in enumerate(reader, 2):
            if None in raw:
                errors.append(f"{line}行目: 列数が多すぎます（引用符またはカンマを確認してください）")
            row = {key: (value or "").strip() for key, value in raw.items() if key is not None}
            prefix = f"{line}行目"
            required_values = ("security_code", "company_name", "benefit_summary",
                               "required_shares", "record_month", "status",
                               "source_checked_date")
            missing = [key for key in required_values if not row[key]]
            if missing:
                errors.append(f"{prefix}: 必須項目が空です: {', '.join(missing)}")
            code = row["security_code"].upper()
            row["security_code"] = code
            if code and not CODE_PATTERN.fullmatch(code):
                errors.append(f"{prefix}: 証券コード形式が不正です: {code}")
            if code in seen:
                errors.append(f"{prefix}: CSV内で証券コードが重複しています: {code}")
            seen.add(code)
            if row["status"] not in ALLOWED_STATUSES:
                errors.append(f"{prefix}: statusが不正です: {row['status']}")
            try:
                shares = int(row["required_shares"])
                if shares <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{prefix}: required_sharesは正の整数で指定してください")
            try:
                months = [int(value.strip()) for value in row["record_month"].split("|")]
                if not months or any(month < 1 or month > 12 for month in months):
                    raise ValueError
            except ValueError:
                errors.append(f"{prefix}: record_monthは1～12を | 区切りで指定してください")
            try:
                dt.date.fromisoformat(row["source_checked_date"])
            except ValueError:
                errors.append(f"{prefix}: source_checked_dateはYYYY-MM-DD形式で指定してください")
            url = urlparse(row["official_url"])
            if url.scheme not in {"http", "https"} or not url.netloc:
                errors.append(f"{prefix}: official_urlはHTTPまたはHTTPS URLで指定してください")
            rows.append(row)
    if errors:
        raise ImportValidationError(errors)
    return rows


def to_master_row(row):
    status = row["status"]
    benefit_status = {"confirmed": "official_confirmed", "abolished": "abolished",
                      "verification_queue": "unverified"}[status]
    confidence = "official_confirmed" if status in {"confirmed", "abolished"} else "unverified"
    months = [int(value.strip()) for value in row["record_month"].split("|")]
    tier = [{"shares": int(row["required_shares"]),
             "description": row["benefit_summary"], "annual_value_yen": None}]
    return {
        "code": row["security_code"], "name": row["company_name"], "market": "",
        "industry": "", "category": "", "record_months": "|".join(map(str, months)),
        "long_term_condition": row["long_term_condition"], "benefit_status": benefit_status,
        "official_verified_at": row["source_checked_date"],
        "official_source_url": row["official_url"], "abolished_at": "",
        "last_record_date": "", "data_confidence": confidence,
        "annual_occurrences": str(len(months)), "change_or_abolition_note": "",
        "benefit_tiers_json": json.dumps(tier, ensure_ascii=False),
    }


def write_atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="",
                                     dir=path.parent, delete=False) as output:
        output.write(content)
        temporary = Path(output.name)
    temporary.replace(path)


def import_benefits(source: Path, master_csv: Path, master_json: Path):
    rows = read_and_validate(source)
    with master_csv.open(encoding="utf-8-sig", newline="") as current:
        existing = list(csv.DictReader(current))
    existing_codes = [row.get("code", "").strip().upper() for row in existing]
    duplicate_master_codes = sorted({code for code in existing_codes
                                     if existing_codes.count(code) > 1})
    if duplicate_master_codes:
        raise ImportValidationError([
            "既存データに証券コードの重複があります: " + ", ".join(duplicate_master_codes)
        ])
    by_code = {row["code"]: row for row in existing}
    before_confirmed = sum(row.get("benefit_status") == "official_confirmed"
                           for row in existing)
    added, update_candidates, duplicates = [], 0, 0
    for row in rows:
        old = by_code.get(row["security_code"])
        if old:
            proposed = to_master_row(row)
            comparable = ("name", "record_months", "long_term_condition", "benefit_status",
                          "official_source_url", "benefit_tiers_json")
            if any(old.get(key, "") != proposed[key] for key in comparable):
                update_candidates += 1
            else:
                duplicates += 1
            # In particular, official_confirmed records are immutable in this importer.
            continue
        converted = to_master_row(row)
        existing.append(converted)
        by_code[converted["code"]] = converted
        added.append(converted)

    if not added:
        return {"input": len(rows), "added": 0, "updated": 0,
                "unchanged": duplicates + update_candidates,
                "update_candidates": update_candidates, "duplicates": duplicates,
                "errors": 0, "before_confirmed": before_confirmed,
                "after_confirmed": before_confirmed,
                "abolished": sum(row.get("benefit_status") == "abolished"
                                  for row in existing)}

    buffer = tempfile.SpooledTemporaryFile(mode="w+", encoding="utf-8", newline="")
    writer = csv.DictWriter(buffer, fieldnames=MASTER_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(existing)
    buffer.seek(0)
    csv_content = buffer.read()
    buffer.close()

    # Generate JSON from the exact prospective CSV before replacing either output.
    with tempfile.TemporaryDirectory() as directory:
        staged_csv = Path(directory) / "benefits.csv"
        staged_json = Path(directory) / "benefits.json"
        staged_csv.write_text(csv_content, encoding="utf-8")
        from csv_to_json import convert
        converted_items = convert(staged_csv, staged_json)
    write_atomic(master_csv, csv_content)
    write_atomic(master_json, json.dumps(converted_items, ensure_ascii=False, indent=2) + "\n")
    return {"input": len(rows), "added": len(added), "updated": 0,
            "unchanged": duplicates + update_candidates,
            "update_candidates": update_candidates, "duplicates": duplicates,
            "errors": 0, "before_confirmed": before_confirmed,
            "after_confirmed": before_confirmed + sum(
                row["benefit_status"] == "official_confirmed" for row in added),
            "abolished": sum(row.get("benefit_status") == "abolished"
                              for row in existing)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/import-benefits.csv")
    parser.add_argument("--csv-output", default="data/benefits.csv")
    parser.add_argument("--json-output", default="data/benefits.json")
    parser.add_argument("--result-file", help="取込集計をJSONで保存するパス")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate_only:
            rows = read_and_validate(Path(args.input))
            print(f"CSV検証成功: {len(rows)}件")
            return
        result = import_benefits(Path(args.input), Path(args.csv_output), Path(args.json_output))
    except ImportValidationError as error:
        for message in error.errors:
            print(f"ERROR: {message}")
        print(f"新規追加数: 0\n更新候補数: 0\n重複除外数: 0\nエラー数: {len(error.errors)}")
        raise SystemExit(1)
    if args.result_file:
        Path(args.result_file).write_text(json.dumps(result, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    print(f"CSV入力件数: {result['input']}\n新規追加数: {result['added']}\n更新数: {result['updated']}")
    print(f"変更なし件数: {result['unchanged']}\n更新候補数: {result['update_candidates']}")
    print(f"重複除外数: {result['duplicates']}\nエラー数: {result['errors']}")


if __name__ == "__main__":
    main()
