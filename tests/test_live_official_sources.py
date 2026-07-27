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
    def test_report_urls_returning_200_with_benefit_evidence(self):
        sources = discovery.load_official_sources(ROOT / "data/official-benefit-sources.json")
        companies = {item["code"]: item for item in json.loads(
            (ROOT / "data/listed-companies.json").read_text(encoding="utf-8"))}
        confirmed = []
        warnings = []
        for code, source in sources.items():
            company = dict(companies[code])
            company["official_domain"] = discovery.normalized_host(source["url"])
            company["official_domains"] = source.get("allowed_domains", [])
            try:
                _final, text = discovery.fetch_official_page(
                    source["url"], company, {source["url"]: source}, registered=True)
                if not any(word in text for word in discovery.BENEFIT_WORDS):
                    raise AssertionError("shareholder-benefit evidence was not found")
            except Exception as error:  # A stale issuer URL must not block the other issuers.
                message = f"{code} {source['url']}: {type(error).__name__}: {error}"
                warnings.append(message)
                print(f"::warning title=Official source unavailable::{message}")
                continue
            confirmed.append(code)

        print(f"Live official sources: confirmed={len(confirmed)} warning={len(warnings)}")
        print("Successfully fetched companies: " + (", ".join(confirmed) or "none"))
        for code in ("7550", "7616", "7412"):
            self.assertIn(code, confirmed, f"official PDF/detail extraction failed for {code}")


if __name__ == "__main__":
    unittest.main()
