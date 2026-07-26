#!/usr/bin/env python3
"""J-Quants から株価を取得する、差し替え可能な株価取得層。"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


@dataclass
class Quote:
    price: float
    forecast_dividend: float | None
    price_at: str
    source: str


class JQuantsProvider:
    API_URL = "https://api.jquants.com/v2/equities/bars/daily"

    def __init__(self, api_key, today=None):
        if not api_key:
            raise ValueError("JQUANTS_API_KEY is required")
        self.api_key = api_key
        self.today = today or date.today()

    def fetch(self, code):
        # 休場日を含めて十分な幅を持たせ、返却された最新の終値を使う。
        query = urlencode({"code": str(code), "from": self.today - timedelta(days=14), "to": self.today})
        request = Request(f"{self.API_URL}?{query}", headers={"x-api-key": self.api_key})
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        rows = payload.get("data") or payload.get("daily_quotes") or []
        if not rows:
            raise ValueError("J-Quants returned no daily bars")
        row = max(rows, key=lambda item: item.get("Date") or item.get("date") or "")
        price = row.get("C", row.get("Close", row.get("close")))
        if not isinstance(price, (int, float)):
            raise ValueError("J-Quants response has no closing price")
        price_date = row.get("Date") or row.get("date")
        return Quote(float(price), None, str(price_date), "jquants")


def update_market_data(codes, provider, previous):
    result = dict(previous)
    failures = []
    for code in codes:
        try:
            result[code] = vars(provider.fetch(code))
        except Exception as exc:
            failures.append({"code": code, "error": str(exc), "previous_data_retained": code in previous})
    return result, failures


def save_update_status(path, failures):
    Path(path).write_text(json.dumps({"updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(), "status": "partial" if failures else "success", "failures": failures}, ensure_ascii=False, indent=2) + "\n")
