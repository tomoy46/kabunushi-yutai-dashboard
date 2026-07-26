#!/usr/bin/env python3
import json
from pathlib import Path
from market_data import SampleProvider,update_market_data,save_update_status
root=Path(__file__).parents[1]; market_path=root/'data/market-data.json'; benefits=json.loads((root/'data/benefits.json').read_text()); previous=json.loads(market_path.read_text())
updated,failures=update_market_data([x['code'] for x in benefits],SampleProvider(previous),previous);market_path.write_text(json.dumps(updated,ensure_ascii=False,indent=2)+'\n');save_update_status(root/'data/update-status.json',failures)
