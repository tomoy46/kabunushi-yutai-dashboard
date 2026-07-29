#!/usr/bin/env python3
"""Populate benefit-candidates.json from already-free discovery artifacts."""
import argparse
import json
import os
from pathlib import Path

from benefit_candidates import merge_candidates


def load(path, default):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError: return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--input", action="append", default=[])
    args = parser.parse_args()
    data = Path(args.data_dir)
    companies = {str(x["code"]): x for x in load(data / "listed-companies.json", [])}
    records = []
    for path in args.input or [data / "review-queue.json", data / "research-log.json",
                               data / "unresolved.json", data / "official-benefit-sources.json"]:
        value = load(path, [])
        if isinstance(value, dict):
            value = [dict(item if isinstance(item, dict) else {}, code=code)
                     for code, item in value.items()]
        records.extend(value)
    output = data / "benefit-candidates.json"
    merged, added = merge_candidates(load(output, []), records, companies)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {name: sum(x["priority"] == name for x in merged) for name in ("high", "medium", "low")}
    print(f"CANDIDATE DISCOVERY: new={added} high={counts['high']} medium={counts['medium']} low={counts['low']}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"new_candidates={added}\n")
            stream.write(f"high_candidates={counts['high']}\n")
            stream.write(f"medium_candidates={counts['medium']}\n")


if __name__ == "__main__": main()
