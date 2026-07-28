#!/usr/bin/env python3
"""Build the listed-company master from JPX's official TSE Excel workbook."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "listed-companies.json"
PROGRESS = ROOT / "data" / "discovery-progress.json"
ALLOWED_MARKETS = {
    "プライム（内国株式）": "プライム",
    "スタンダード（内国株式）": "スタンダード",
    "グロース（内国株式）": "グロース",
}
CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
# JPX includes class securities in the domestic-stock market categories.  Their
# names identify them before their (usually five-character) code is validated.
NON_COMMON_NAME_PATTERN = re.compile(
    r"(?:優先株式|種類株式|出資証券|ＥＴＦ|ETF|ＥＴＮ|ETN|"
    r"不動産投資法人|投資法人|ＲＥＩＴ|REIT|インフラファンド)"
)


def download(url, destination):
    request = Request(
        url, headers={"User-Agent": "kabunushi-yutai-dashboard/1.0 (JPX master updater)"}
    )
    with urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"JPX download returned HTTP {response.status}")
        destination.write_bytes(response.read())


def normalize_code(raw):
    return str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw).strip().upper()


def parse_workbook(path, previous=None, excluded=None):
    """Return domestic common stocks, optionally collecting excluded class securities."""
    try:
        import xlrd
    except ImportError as error:
        raise RuntimeError("xlrd is required: python -m pip install xlrd") from error

    sheet = xlrd.open_workbook(path).sheet_by_index(0)
    headings = [str(sheet.cell_value(0, column)).strip() for column in range(sheet.ncols)]
    required = {"コード", "銘柄名", "市場・商品区分", "33業種区分"}
    if not required.issubset(headings):
        raise ValueError(f"unexpected JPX columns: {headings}")

    column = {name: headings.index(name) for name in required}
    retained = {str(item["code"]): {
        key: item.get(key) for key in ("official_domain", "official_url",
                                      "official_url_candidate", "official_url_candidates")
        if item.get(key)
    } for item in (previous or [])}
    companies = []
    excluded = excluded if excluded is not None else []
    for row in range(1, sheet.nrows):
        product = str(sheet.cell_value(row, column["市場・商品区分"])).strip()
        if product not in ALLOWED_MARKETS:
            continue

        code = normalize_code(sheet.cell_value(row, column["コード"]))
        name = str(sheet.cell_value(row, column["銘柄名"])).strip()
        # This test deliberately precedes code validation: a class security must
        # be skipped rather than aborting the complete update because of its code.
        if name and NON_COMMON_NAME_PATTERN.search(name):
            excluded.append({"code": code, "name": name})
            continue
        if not CODE_PATTERN.fullmatch(code) or not name:
            raise ValueError(f"invalid JPX company row {row + 1}: {code!r}, {name!r}")

        companies.append(
            {
                "code": code,
                "name": name,
                "market": ALLOWED_MARKETS[product],
                "sector": str(sheet.cell_value(row, column["33業種区分"])).strip(),
                "official_domain": retained.get(code, {}).get("official_domain"),
                **{key: value for key, value in retained.get(code, {}).items()
                   if key != "official_domain"},
            }
        )

    if len(companies) < 3000:
        raise ValueError(f"JPX domestic common-stock count is unexpectedly low: {len(companies)}")
    if len(companies) != len({item["code"] for item in companies}):
        raise ValueError("JPX workbook contains duplicate security codes")
    return companies


def update_progress(companies, path=PROGRESS):
    progress = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    codes = {item["code"] for item in companies}
    processed = list(
        dict.fromkeys(code for code in progress.get("processed_codes", []) if code in codes)
    )
    failed = list(dict.fromkeys(code for code in progress.get("failed_codes", []) if code in codes))
    progress.update(
        {
            "processed_codes": processed,
            "failed_codes": failed,
            "total_companies": len(companies),
            "uninvestigated_count": len(companies) - len(processed),
        }
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def update(url=JPX_URL, output=OUTPUT, source=None, progress_path=PROGRESS, excluded=None):
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
    with tempfile.TemporaryDirectory() as directory:
        workbook = source or Path(directory) / "data_j.xls"
        if source is None:
            download(url, workbook)
        companies = parse_workbook(workbook, previous, excluded)

    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(companies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    update_progress(companies, progress_path)
    return companies


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=JPX_URL)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--progress", type=Path, default=PROGRESS)
    args = parser.parse_args()
    excluded = []
    companies = update(args.url, args.output, args.source, args.progress, excluded)
    print(
        json.dumps(
            {
                "total": len(companies),
                "markets": {
                    market: sum(item["market"] == market for item in companies)
                    for market in ALLOWED_MARKETS.values()
                },
                "excluded_non_common_count": len(excluded),
                "excluded_non_common_samples": excluded[:10],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
