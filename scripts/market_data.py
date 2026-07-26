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
    FREE_PLAN_DELAY_DAYS = 84
    SEARCH_WINDOW_DAYS = 14

    def __init__(self, api_key, today=None):
        if not api_key:
            raise ValueError("JQUANTS_API_KEY is required")
        self.api_key = api_key
        self.today = today or date.today()

    def fetch(self, code):
        # Freeプランの日足は12週間遅延する。遅延後の基準日から過去に幅を
        # 持たせることで、土日祝や休場日でも直近取引日の終値を取得する。
        reference_date = self.today - timedelta(days=self.FREE_PLAN_DELAY_DAYS)
        query = urlencode({
            "code": str(code),
            "from": reference_date - timedelta(days=self.SEARCH_WINDOW_DAYS),
            "to": reference_date,
        })
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
