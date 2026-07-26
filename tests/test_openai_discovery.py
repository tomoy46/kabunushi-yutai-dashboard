import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import discover_benefits_with_openai as discovery


class OpenAIDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.company = {"code": "1301", "name": "極洋", "official_domain": "kyokuyo.co.jp"}

    def item(self, url="https://www.kyokuyo.co.jp/ir/benefit.html"):
        value = {key: None for key in discovery.FIELDS}
        value.update({"code": "1301", "name": "極洋", "benefit_status": "official_confirmed",
                      "record_months": [3], "minimum_shares": 100, "confidence_score": 95,
                      "official_source_url": url, "valuation_type": "official_amount",
                      "official_verified_at": "2026-07-26"})
        return value

    def test_default_model_and_request_contract(self):
        payload = discovery.build_payload(self.company)
        self.assertEqual(payload["model"], "gpt-5.4-nano")
        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertEqual(payload["tools"][0]["search_context_size"], "low")
        self.assertEqual(payload["max_tool_calls"], 1)
        self.assertEqual(payload["reasoning"]["effort"], "none")
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertEqual(payload["tools"][0]["filters"]["allowed_domains"],
                         ["kyokuyo.co.jp", "jpx.co.jp", "tdnet.info"])

    def test_api_error_diagnostic_fields_are_allowlisted_and_redacted(self):
        secret = "sk-this-is-a-secret-value"
        error = discovery.APIError(400, f"bad value {secret}", error_type="invalid_request_error",
                                   code="unsupported_parameter", param="reasoning.effort",
                                   request_id="req_123")
        output = "\n".join(discovery.safe_error_lines(error, secret))
        self.assertIn("HTTP status: 400", output)
        self.assertIn("Error type: invalid_request_error", output)
        self.assertIn("Error code: unsupported_parameter", output)
        self.assertIn("Error param: reasoning.effort", output)
        self.assertIn("Request ID: req_123", output)
        self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)

    def test_non_api_exception_is_bounded_and_redacted(self):
        secret = "sk-another-secret-value"
        output = "\n".join(discovery.safe_error_lines(ValueError(secret + "x" * 600), secret))
        self.assertIn("Exception type: ValueError", output)
        self.assertNotIn(secret, output)
        self.assertLessEqual(len(output.split("Error message: ", 1)[1]), 500)

    def test_only_returned_search_urls_are_accepted(self):
        item, reasons = discovery.validate(self.item(), self.company, {}, fetcher=lambda *x: x[0])
        self.assertEqual(item["benefit_status"], "candidate")
        self.assertIn("source_not_in_search_results", reasons)

    def test_blocked_domains_rejected_and_official_domains_allowed(self):
        self.assertFalse(discovery.allowed_url("https://kabutan.jp/stock/?code=1301", "kyokuyo.co.jp"))
        self.assertFalse(discovery.allowed_url("https://x.com/example", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.kyokuyo.co.jp/ir/", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.jpx.co.jp/a.pdf", None))
        self.assertTrue(discovery.allowed_url("https://www.release.tdnet.info/a.pdf", None))

    def test_discount_is_never_invented_as_cash_value(self):
        item = self.item(); item.update({"benefit_description": "購入額を10%割引", "annual_value_yen": 9999})
        item, reasons = discovery.validate(item, self.company,
            {discovery.canonical_url(item["official_source_url"]): {}}, fetcher=lambda *x: x[0])
        self.assertFalse(reasons); self.assertIsNone(item["annual_value_yen"])
        self.assertEqual(item["valuation_type"], "not_calculated")

    def test_search_tool_call_count_comes_from_output(self):
        response = {"output": [{"type": "web_search_call", "action": {"sources": []}},
                               {"type": "message", "content": []}]}
        self.assertEqual(discovery.web_search_calls(response), 1)

    def test_existing_confirmed_and_abolished_are_immutable(self):
        companies = [{"code": str(i), "name": str(i)} for i in range(15)]
        benefits = [{"code": str(i), "benefit_status": "official_confirmed"} for i in range(10)] + [
            {"code": "10", "benefit_status": "abolished"}, {"code": "11", "benefit_status": "abolished"}]
        args = type("Args", (), {"start_code": None, "end_code": None, "retry_failed": False,
                                  "batch_size": 10, "daily_limit": 20})()
        selected = discovery.choose(companies, args, {"next_index": 0}, benefits)
        self.assertEqual([x["code"] for x in selected], ["12", "13", "14"])

    def test_diagnostic_does_not_write_any_data_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"listed-companies.json": [self.company], "company-domains.json": {},
                     "benefits.json": [], "verification-queue.json": [], "discovery-progress.json": {"next_index": 0},
                     "openai-api-usage.json": []}
            for name, value in files.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            response = {"output_text": json.dumps(self.item()), "output": [{"type": "web_search_call", "action": {
                "sources": [{"url": self.item()["official_source_url"]}]}}], "usage": {}}
            args = type("Args", (), {"diagnostic_mode": True, "batch_size": 10, "daily_limit": 20,
                "start_code": None, "end_code": None, "retry_failed": False, "official_only": False})()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock-secret"}), \
                 patch.object(discovery, "request_response", return_value=response), \
                 patch.object(discovery, "fetch_and_validate", return_value=self.item()["official_source_url"]):
                discovery.run(args)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_diagnostic_failure_prints_safe_error_and_final_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in {"listed-companies.json": [self.company], "company-domains.json": {},
                    "benefits.json": [], "verification-queue.json": [],
                    "discovery-progress.json": {"next_index": 0}, "openai-api-usage.json": []}.items():
                (root/name).write_text(json.dumps(value), encoding="utf-8")
            args = type("Args", (), {"diagnostic_mode": True, "batch_size": 10, "daily_limit": 20,
                "start_code": None, "end_code": None, "retry_failed": False, "official_only": False})()
            secret = "sk-diagnostic-secret"
            failure = discovery.APIError(400, f"unsupported {secret}", "invalid_request_error",
                                         "unsupported_parameter", "reasoning.effort", "req_test")
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": secret}), \
                 patch.object(discovery, "request_response", side_effect=failure), redirect_stdout(output):
                result = discovery.run(args)
            text = output.getvalue()
            self.assertEqual(result, 1)
            self.assertIn("Plain Responses API: failure", text)
            self.assertIn("HTTP status: 400", text)
            self.assertIn("Diagnostic result: failure", text)
            self.assertIn("Failed stage: plain", text)
            self.assertIn("Responses API calls: 2", text)
            self.assertNotIn(secret, text)

    def test_api_key_is_not_in_error_or_payload_files_and_gemini_not_required(self):
        self.assertNotIn("GEMINI_API_KEY", Path(discovery.__file__).read_text())
        secret = "TOP-SECRET"
        self.assertNotIn(secret, json.dumps(discovery.build_payload(self.company)))
        with patch.dict(os.environ, {}, clear=True):
            args = type("Args", (), {"diagnostic_mode": False})()
            self.assertEqual(discovery.run(args), 2)

    def test_429_does_not_mutate_progress_or_failed_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); progress = {"next_index": 7, "processed_codes": ["9999"], "failed_codes": ["8888"]}
            fixtures = {"listed-companies.json": [self.company], "company-domains.json": {}, "benefits.json": [],
                        "verification-queue.json": [], "discovery-progress.json": progress, "openai-api-usage.json": []}
            for name, value in fixtures.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            args = type("Args", (), {"diagnostic_mode": False, "batch_size": 10, "daily_limit": 20,
                "start_code": None, "end_code": None, "retry_failed": False, "official_only": False})()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "request_response", side_effect=discovery.APIError(429, "rate limited")):
                discovery.run(args)
            self.assertEqual(json.loads((root/"discovery-progress.json").read_text()), progress)


if __name__ == "__main__": unittest.main()
