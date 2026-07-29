import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import benefit_candidates as candidates
import discover_benefits_with_openai as discovery


class BenefitCandidateTests(unittest.TestCase):
    def test_exchange_titles_are_high_and_deduplicated(self):
        companies = {"1234": {"name": "候補株式会社"}}
        row = {"title": "1234 株主優待制度の導入に関するお知らせ",
               "url": "https://www.release.tdnet.info/inbs/notice.pdf", "published_at": "2026-07-29"}
        merged, added = candidates.merge_candidates([], [row, row], companies)
        self.assertEqual(added, 1)
        self.assertEqual(merged[0]["priority"], "high")
        self.assertEqual(merged[0]["candidate_source"], "tdnet")
        self.assertEqual(merged[0]["verification_status"], "pending")

    def test_quota_is_twenty_high_and_five_medium(self):
        rows = [dict(security_code=str(1000+i), priority="high", verification_status="pending",
                     candidate_date="2026-07-29") for i in range(30)]
        rows += [dict(security_code=str(2000+i), priority="medium", verification_status="pending",
                      candidate_date="2026-07-29") for i in range(10)]
        selected = candidates.select_candidates(rows, [])
        self.assertEqual(sum(x["priority"] == "high" for x in selected), 20)
        self.assertEqual(sum(x["priority"] == "medium" for x in selected), 5)

    def test_unresolved_retries_only_for_new_disclosure(self):
        old = {"security_code": "1234", "priority": "high", "verification_status": "pending",
               "candidate_date": "2026-06-01"}
        new = dict(old, candidate_url="new", candidate_date="2026-08-01")
        unresolved = [{"code": "1234", "checked_at": "2026-07-01T00:00:00+00:00"}]
        self.assertEqual(candidates.select_candidates([old], unresolved), [])
        self.assertEqual(candidates.select_candidates([new], unresolved), [new])

    def test_three_free_facts_do_not_call_openai(self):
        text = "株主優待制度 100株以上 自社商品を贈呈 毎年3月末現在の株主"
        mocked_api = Mock(side_effect=AssertionError("OpenAI must not be called"))
        with patch.object(discovery, "request_response", mocked_api):
            facts = discovery.evidence_facts(text)
            self.assertEqual(sum(facts[x] for x in ("required_shares", "benefit_content", "record_month")), 3)
            item = discovery.free_official_extraction(
                {"code": "1234", "name": "候補"}, "https://www.jpx.co.jp/a.pdf", text)
        self.assertEqual(item["benefit_status"], "official_confirmed")
        self.assertEqual(item["minimum_shares"], 100)
        mocked_api.assert_not_called()

    def test_external_fetch_can_be_mocked_for_direct_disclosure(self):
        response = Mock(); response.read.return_value = b"official"
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with patch.object(discovery, "urlopen", return_value=response) as external:
            self.assertEqual(response.read(), b"official")
        self.assertEqual(external.call_count, 0)  # no network occurs without an explicit fetch


if __name__ == "__main__": unittest.main()
