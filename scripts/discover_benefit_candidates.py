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
    default_inputs = [data / "official-benefit-sources.json", data / "research-log.json",
                      data / "unresolved.json", data / "discovery-progress.json",
                      data / "benefit-candidates.json", data / "review-queue.json"]
    for path in args.input or default_inputs:
        value = load(path, [])
        if isinstance(value, dict):
            value = [dict(item if isinstance(item, dict) else {}, code=code)
                     for code, item in value.items()]
        records.extend(value)
    output = data / "benefit-candidates.json"
    existing = load(output, [])
    if not existing:
        print("CANDIDATE DISCOVERY START: benefit-candidates.json is missing or empty")
    merged, added = merge_candidates(existing, records, companies)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {name: sum(x["priority"] == name for x in merged) for name in ("high", "medium", "low")}
    sources = {name: sum(x.get("candidate_source") == name for x in merged)
               for name in ("tdnet", "jpx")}
    research = sum(1 for record in load(data / "research-log.json", [])
                   if record.get("candidate_title") or record.get("title"))
    print(f"CANDIDATE DISCOVERY: new={added} tdnet={sources['tdnet']} jpx={sources['jpx']} "
          f"research_log={research} high={counts['high']} medium={counts['medium']} low={counts['low']}")
    # No network collector is wired into this workflow yet.  Say so explicitly
    # instead of making a zero look like a successfully queried empty period.
    for source in ("tdnet", "jpx"):
        print(f"SOURCE DIAGNOSTIC: source={source} executed=false status=source_not_implemented "
              "period=not_applicable http_status=not_applicable files=0 disclosures=0 "
              f"keyword_matches={sources[source]} duplicates_excluded=0")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"new_candidates={added}\n")
            stream.write(f"high_candidates={counts['high']}\n")
            stream.write(f"medium_candidates={counts['medium']}\n")
            stream.write(f"tdnet_discovered={sources['tdnet']}\n")
            stream.write(f"jpx_discovered={sources['jpx']}\n")
            stream.write(f"research_log_candidates={research}\n")
            for source in ("tdnet", "jpx"):
                stream.write(f"{source}_source_status=source_not_implemented\n")


if __name__ == "__main__": main()
