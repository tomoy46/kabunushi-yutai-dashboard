#!/usr/bin/env python3
"""Verify a dated import batch against the local, pre-existing data set."""

import argparse
import csv
import json
from pathlib import Path


def codes_from_csv(path: Path, code_column: str, date_column: str, batch_date: str):
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    batch = {row[code_column].strip().upper() for row in rows
             if row.get(date_column, "").strip() == batch_date}
    baseline = {row[code_column].strip().upper() for row in rows
                if row.get(date_column, "").strip() != batch_date}
    return rows, batch, baseline


def verify(import_path: Path, master_path: Path, batch_date: str, expected: int):
    rows, batch, import_baseline = codes_from_csv(
        import_path, "security_code", "source_checked_date", batch_date
    )
    _, master_batch, master_baseline = codes_from_csv(
        master_path, "code", "official_verified_at", batch_date
    )
    duplicate_rows = len(rows) - len({row["security_code"].strip().upper() for row in rows})
    overlap = sorted(batch & (import_baseline | master_baseline))
    missing_from_master = sorted(batch - master_batch)
    errors = []
    if len(batch) != expected:
        errors.append(f"新規バッチ件数が不正です: expected={expected}, actual={len(batch)}")
    if duplicate_rows:
        errors.append(f"import CSV内に重複があります: {duplicate_rows}件")
    if overlap:
        errors.append("既存証券コードとの重複があります: " + ", ".join(overlap))
    if missing_from_master:
        errors.append("マスター未反映の証券コードがあります: " + ", ".join(missing_from_master))
    return {
        "batch_date": batch_date,
        "expected_new_companies": expected,
        "actual_new_companies": len(batch),
        "local_baseline_companies": len(import_baseline | master_baseline),
        "duplicate_import_rows": duplicate_rows,
        "existing_code_overlap": len(overlap),
        "missing_from_master": len(missing_from_master),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-date", required=True)
    parser.add_argument("--expected", type=int, default=200)
    parser.add_argument("--import-file", type=Path, default=Path("data/import-benefits.csv"))
    parser.add_argument("--master-file", type=Path, default=Path("data/benefits.csv"))
    args = parser.parse_args()
    result = verify(args.import_file, args.master_file, args.batch_date, args.expected)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
