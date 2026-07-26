#!/usr/bin/env python3
"""差し替え可能な株価取得層。現状はサンプルproviderのみ。"""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
@dataclass
class Quote: price: float; forecast_dividend: float|None; price_at: str; source: str
class SampleProvider:
    def __init__(self, seed): self.seed=seed
    def fetch(self, code):
        x=self.seed[code];return Quote(x['price'],x.get('forecast_dividend'),x['price_at'],'sample')
class JQuantsProvider:
    def fetch(self, code): raise NotImplementedError('J-Quants連携は未設定です')
def update_market_data(codes, provider, previous):
    result=dict(previous); failures=[]
    for code in codes:
        try: result[code]=vars(provider.fetch(code))
        except Exception as exc: failures.append({'code':code,'error':str(exc),'previous_data_retained':code in previous})
    return result,failures
def save_update_status(path, failures):
    Path(path).write_text(json.dumps({'updated_at':datetime.now(ZoneInfo('Asia/Tokyo')).isoformat(),'status':'partial' if failures else 'success','failures':failures},ensure_ascii=False,indent=2)+'\n')
