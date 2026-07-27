"""Opt-in network integration test for maintained shareholder-benefit URLs.

Run with RUN_LIVE_OFFICIAL_SOURCES=1.  CI environments without outbound HTTP
keep the deterministic unit suite usable, while scheduled/production checks use
the exact same downloader, redirect policy, one-level link traversal and PDF
extraction as discovery.
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import discover_benefits_with_openai as discovery


@unittest.skipUnless(os.environ.get("RUN_LIVE_OFFICIAL_SOURCES") == "1",
                     "set RUN_LIVE_OFFICIAL_SOURCES=1 to access corporate sites")
class LiveOfficialSourceTests(unittest.TestCase):
    def test_all_urls_respond_and_at_least_one_has_confirmable_current_terms(self):
        sources = discovery.load_official_sources(ROOT / "data/official-benefit-sources.json")
        companies = {item["code"]: item for item in json.loads(
            (ROOT / "data/listed-companies.json").read_text(encoding="utf-8"))}
        confirmed = []
        for code, source in sources.items():
            company = dict(companies[code])
            company["official_domain"] = discovery.normalized_host(source["url"])
            company["official_domains"] = source.get("allowed_domains", [])
            with self.subTest(code=code, url=source["url"]):
                _final, text = discovery.fetch_official_page(
                    source["url"], company, {source["url"]: source}, registered=True)
                self.assertTrue(text, "official source returned no extractable evidence")
                minimum_shares = discovery.re.search(r"\d[\d,]*\s*株", text)
                benefit = discovery.re.search(r"\d[\d,]*\s*(?:円|ポイント)|優待券|食事券", text)
                record_date = discovery.re.search(r"(?:権利確定|基準日|\d{1,2}月末)", text)
                if minimum_shares and benefit and record_date:
                    confirmed.append(code)
        self.assertTrue(confirmed, "none of the five production sources reached confirmed evidence")


if __name__ == "__main__":
    unittest.main()
