#!/usr/bin/env python3
"""Build the listed-company master from JPX's official TSE Excel workbook."""
from __future__ import annotations
import argparse, json, re, tempfile
from pathlib import Path
from urllib.request import Request, urlopen

JPX_URL="https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
ROOT=Path(__file__).resolve().parents[1]; OUTPUT=ROOT/"data"/"listed-companies.json"
PROGRESS=ROOT/"data"/"discovery-progress.json"
ALLOWED_MARKETS={"プライム（内国株式）":"プライム","スタンダード（内国株式）":"スタンダード","グロース（内国株式）":"グロース"}
CODE_PATTERN=re.compile(r"^[0-9A-Z]{4}$")

def download(url,destination):
 request=Request(url,headers={"User-Agent":"kabunushi-yutai-dashboard/1.0 (JPX master updater)"})
 with urlopen(request,timeout=60) as response:
  if response.status!=200: raise RuntimeError(f"JPX download returned HTTP {response.status}")
  destination.write_bytes(response.read())

def parse_workbook(path,previous=None):
 try: import xlrd
 except ImportError as error: raise RuntimeError("xlrd is required: python -m pip install xlrd") from error
 sheet=xlrd.open_workbook(path).sheet_by_index(0)
 headings=[str(sheet.cell_value(0,c)).strip() for c in range(sheet.ncols)]
 required={"コード","銘柄名","市場・商品区分","33業種区分"}
 if not required.issubset(headings): raise ValueError(f"unexpected JPX columns: {headings}")
 column={name:headings.index(name) for name in required}; domains={str(x["code"]):x.get("official_domain") for x in (previous or [])}; companies=[]
 for row in range(1,sheet.nrows):
  product=str(sheet.cell_value(row,column["市場・商品区分"])).strip()
  if product not in ALLOWED_MARKETS: continue
  raw=sheet.cell_value(row,column["コード"]); code=str(int(raw)) if isinstance(raw,float) and raw.is_integer() else str(raw).strip().upper(); name=str(sheet.cell_value(row,column["銘柄名"])).strip()
  if not CODE_PATTERN.fullmatch(code) or not name: raise ValueError(f"invalid JPX company row {row+1}: {code!r}, {name!r}")
  companies.append({"code":code,"name":name,"market":ALLOWED_MARKETS[product],"sector":str(sheet.cell_value(row,column["33業種区分"])).strip(),"official_domain":domains.get(code)})
 if len(companies)<3000: raise ValueError(f"JPX domestic common-stock count is unexpectedly low: {len(companies)}")
 if len(companies)!=len({x["code"] for x in companies}): raise ValueError("JPX workbook contains duplicate security codes")
 return companies

def update_progress(companies,path=PROGRESS):
 progress=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
 codes={x["code"] for x in companies}; processed=list(dict.fromkeys(x for x in progress.get("processed_codes",[]) if x in codes)); failed=list(dict.fromkeys(x for x in progress.get("failed_codes",[]) if x in codes))
 progress.update({"processed_codes":processed,"failed_codes":failed,"total_companies":len(companies),"uninvestigated_count":len(companies)-len(processed)})
 temporary=path.with_suffix(".json.tmp");temporary.write_text(json.dumps(progress,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");temporary.replace(path)

def update(url=JPX_URL,output=OUTPUT,source=None,progress_path=PROGRESS):
 previous=json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
 with tempfile.TemporaryDirectory() as directory:
  workbook=source or Path(directory)/"data_j.xls"
  if source is None: download(url,workbook)
  companies=parse_workbook(workbook,previous)
 temporary=output.with_suffix(".json.tmp"); temporary.write_text(json.dumps(companies,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); temporary.replace(output)
 update_progress(companies,progress_path)
 return companies

def main():
 parser=argparse.ArgumentParser(); parser.add_argument("--url",default=JPX_URL); parser.add_argument("--source",type=Path); parser.add_argument("--output",type=Path,default=OUTPUT); parser.add_argument("--progress",type=Path,default=PROGRESS); args=parser.parse_args(); companies=update(args.url,args.output,args.source,args.progress)
 print(json.dumps({"total":len(companies),"markets":{m:sum(x["market"]==m for x in companies) for m in ALLOWED_MARKETS.values()}},ensure_ascii=False))
if __name__=="__main__": main()
