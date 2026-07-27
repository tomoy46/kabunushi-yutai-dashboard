import copy
import gzip
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

    def test_schema_requires_benefit_tiers_and_storage_adds_compatibility_fields(self):
        self.assertIn("benefit_tiers", discovery.SCHEMA["required"])
        tier_schema = discovery.FIELDS["benefit_tiers"]
        self.assertEqual(tier_schema["type"], "array")
        item = self.item()
        item.update({"benefit_title": "自社製品", "benefit_description": "贈呈",
                     "annual_value_yen": 2500, "long_term_required": False,
                     "conditions": "毎年7月贈呈予定",
                     "benefit_tiers": [{"shares": 100, "maximum_shares": 299,
                         "description": "2,500円相当の自社製品", "annual_value_yen": 2500}]})
        stored = discovery.normalize_for_storage(item, {**self.company, "market": "プライム", "sector": "水産・農林業"})
        self.assertEqual(stored["data_confidence"], "official_confirmed")
        self.assertEqual(stored["market"], "プライム")
        self.assertEqual(stored["industry"], "水産・農林業")
        self.assertEqual(stored["minimum_shares"], 100)
        self.assertEqual(stored["long_term_condition"], "なし")
        self.assertEqual(stored["last_checked_at"], "2026-07-26")
        self.assertNotIn("undefined", json.dumps(stored))

    def test_company_name_normalizes_legal_form_spaces_and_width(self):
        self.assertTrue(discovery.same_company_name("極洋", "株式会社 極洋"))
        self.assertTrue(discovery.same_company_name("ＡＢＣ １２３", "株式会社ABC123"))
        self.assertTrue(discovery.same_company_name("テスト・ホールディングス", "テスト HD"))
        self.assertFalse(discovery.same_company_name("極洋", "日本取引所グループ"))

    def test_url_normalization_removes_tracking_and_fragment(self):
        url = "https://www.kyokuyo.co.jp/ir/concept?utm_source=test&gclid=secret#benefit"
        self.assertEqual(discovery.canonical_url(url), "https://www.kyokuyo.co.jp/ir/concept/")

    def test_www_and_trailing_slash_are_the_same_url_identity(self):
        self.assertEqual(discovery.url_identity("https://kyokuyo.co.jp/ir/concept"),
                         discovery.url_identity("https://www.kyokuyo.co.jp/ir/concept/"))

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

    def test_third_party_model_url_is_replaced_by_verified_corporate_source(self):
        corporate = "https://www.kyokuyo.co.jp/ir/benefit.html"
        item = self.item("https://kabutan.jp/stock/?code=1301")
        sources = {discovery.canonical_url(item["official_source_url"]): {}, corporate: {}}
        checked = []
        def fetcher(url, *_args):
            checked.append(url)
            return url
        item, reasons = discovery.validate(item, self.company, sources, fetcher=fetcher)
        self.assertFalse(reasons)
        self.assertEqual(item["official_source_url"], corporate)
        self.assertEqual(checked, [corporate])

    def test_official_candidates_are_limited_to_returned_search_sources(self):
        returned = "https://www.kyokuyo.co.jp/ir/benefit.html"
        sources = {returned: {}}
        self.assertEqual(discovery.candidate_urls(sources, self.company,
                         "https://www.kyokuyo.co.jp/invented.html"), [returned])

    def test_official_page_correction_has_no_web_search_and_is_bounded(self):
        payload = discovery.official_page_payload(
            self.company, self.item()["official_source_url"], "株主優待" + "長" * 25_000,
            self.item(), discovery.DEFAULT_MODEL)
        self.assertNotIn("tools", payload)
        self.assertLess(len(payload["input"]), 25_000)
        self.assertIn("initial_structured_result", payload["input"])

    def test_blocked_domains_rejected_and_official_domains_allowed(self):
        self.assertFalse(discovery.allowed_url("https://kabutan.jp/stock/?code=1301", "kyokuyo.co.jp"))
        self.assertFalse(discovery.allowed_url("https://x.com/example", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.kyokuyo.co.jp/ir/", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.jpx.co.jp/a.pdf", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.release.tdnet.info/a.pdf", "kyokuyo.co.jp"))
        self.assertTrue(discovery.allowed_url("https://www.jpx.co.jp/a.pdf", None))
        self.assertTrue(discovery.allowed_url("https://www.release.tdnet.info/a.pdf", None))

    def test_failed_validation_clears_model_url(self):
        item, reasons = discovery.validate(
            self.item("https://www.jpx.co.jp/corporate/investor-relations/shareholders/incentives/index.html"),
            self.company, {}, fetcher=lambda *args: (_ for _ in ()).throw(ValueError("wrong company")))
        self.assertIn("source_not_in_search_results", reasons)
        self.assertIsNone(item["official_source_url"])
        self.assertEqual(item["error_reason"], "official_source_validation_failed")

    def test_exchange_disclosure_requires_code_but_not_company_name(self):
        url = "https://release.tdnet.info/inbs/example.pdf"
        company = {"code": "1301", "name": "株式会社 極洋", "official_domain": None}
        class Response:
            status = 200
            def geturl(self): return url
            def read(self, _limit): return "極洋 株主優待制度".encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        with patch.object(discovery, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "exchange_disclosure_identity_mismatch"):
                discovery.fetch_official_page(url, company, {url})
        response = Response()
        response.read = lambda _limit: "旧社名・ブランド名 証券コード1301 株主優待制度".encode()
        with patch.object(discovery, "urlopen", return_value=response):
            self.assertEqual(discovery.fetch_official_page(url, company, {url})[0], url)

    def test_target_exchange_pdfs_accept_matching_code_in_disclosure_metadata(self):
        for code in ("7550", "9861", "8163", "7616", "7412"):
            with self.subTest(code=code):
                url = f"https://www.release.tdnet.info/inbs/{code}.pdf"
                company = {"code": code, "name": "現在の会社名", "official_domain": "example.co.jp"}
                class Response:
                    status = 200
                    def geturl(self): return url
                    def read(self, _limit): return "株主優待制度のお知らせ".encode()
                    def __enter__(self): return self
                    def __exit__(self, *_args): pass
                metadata = {url: {"title": f"旧社名（証券コード {code}）"}}
                with patch.object(discovery, "urlopen", return_value=Response()):
                    self.assertEqual(discovery.fetch_official_page(url, company, metadata)[0], url)

    def test_registered_source_html_ignores_name_variant_and_pdf_is_extracted(self):
        html_url = "https://example.co.jp/ir/benefit/"
        pdf_url = "https://example.co.jp/ir/benefit.pdf"
        company = {"code": "7550", "name": "ゼンショーホールディングス",
                   "official_domain": "example.co.jp"}
        class Headers:
            def __init__(self, value): self.value = value
            def get(self, _key, default=""): return self.value or default
        class Response:
            status = 200
            def __init__(self, url, body, content_type):
                self.url, self.body, self.headers = url, body, Headers(content_type)
            def geturl(self): return self.url
            def read(self, _limit): return self.body
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        html = "<main>ブランド表記のみ 株主優待制度 100株</main>".encode()
        with patch.object(discovery, "urlopen", return_value=Response(html_url, html, "text/html")):
            self.assertIn("100株", discovery.fetch_official_page(
                html_url, company, {}, registered=True)[1])
        pdf = "ブランド表記のみ 株主優待制度 100株 3月".encode()
        with patch.object(discovery, "urlopen", return_value=Response(pdf_url, pdf, "application/pdf")), \
             patch.object(discovery.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("3月", discovery.fetch_official_page(
                pdf_url, company, {}, registered=True)[1])

    def test_registered_source_404_has_dedicated_outcome(self):
        url = "https://example.co.jp/ir/benefit/"
        company = {"code": "7550", "name": "ゼンショーホールディングス",
                   "official_domain": "example.co.jp"}
        error = discovery.HTTPError(url, 404, "not found", {}, None)
        with patch.object(discovery, "urlopen", side_effect=error):
            with self.assertRaisesRegex(discovery.OfficialSourceNotFound, "official_source_http_404"):
                discovery.fetch_official_page(url, company, {}, registered=True)

    def test_registered_source_logs_http_metadata_preview_and_decodes_gzip(self):
        url = "https://example.co.jp/ir/benefit/"
        company = {"code": "7550", "name": "テスト", "official_domain": "example.co.jp"}
        html = "<main>株主優待制度 100株</main>".encode()
        class Headers(dict):
            def get(self, key, default=""): return super().get(key, default)
        class Response:
            status = 200
            headers = Headers({"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"})
            def geturl(self): return url
            def read(self, _limit): return gzip.compress(html)
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        output = StringIO()
        with patch.object(discovery, "urlopen", return_value=Response()), redirect_stdout(output):
            _, text = discovery.fetch_official_page(url, company, {}, registered=True)
        self.assertIn("株主優待制度", text)
        log = output.getvalue()
        self.assertIn("security_code=7550", log)
        self.assertIn("http_status=200", log)
        self.assertIn("final_url=" + url, log)
        self.assertIn("content_type=text/html; charset=utf-8", log)
        self.assertIn("document_type=HTML", log)
        self.assertIn("extracted_text_preview=株主優待制度 100株", log)
        self.assertIn("openai_body_characters=11", log)

    def test_pdf_conversion_failure_reports_exit_code_and_stderr(self):
        completed = type("Completed", (), {"returncode": 1, "stderr": b"syntax error"})()
        diagnostics = []
        with patch.object(discovery.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(discovery.OfficialSourceFetchError, "pdf_conversion_failure"):
                discovery.pdf_text(b"%PDF-broken", lambda **values: diagnostics.append(values))
        self.assertEqual(diagnostics, [{"returncode": 1, "stderr": "syntax error"}])

    def test_registered_sources_are_confirmed_without_web_search(self):
        targets = [
            ("7550", "ゼンショーホールディングス"), ("9861", "吉野家ホールディングス"),
            ("8163", "SRSホールディングス"), ("7616", "コロワイド"), ("7412", "アトム"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = json.loads((Path(__file__).parents[1] / "data/official-benefit-sources.json").read_text())
            fixtures = {"listed-companies.json": [{"code": code, "name": name} for code, name in targets],
                        "official-benefit-sources.json": sources, "company-domains.json": {},
                        "benefits.json": [], "verification-queue.json": [], "research-log.json": [],
                        "review-queue.json": [], "discovery-progress.json": {}, "openai-api-usage.json": []}
            for name, value in fixtures.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            (root / "benefit-universe.csv").write_text("code\n", encoding="utf-8")
            args = type("Args", (), {"diagnostic_mode": False, "security_codes": ",".join(x[0] for x in targets),
                "batch_size": 5, "daily_limit": 20, "retry_failed": False, "official_only": False})()
            extracted = {key: None for key in discovery.FIELDS}
            extracted.update({"benefit_status": "official_confirmed", "record_months": [3, 9],
                              "minimum_shares": 100, "benefit_description": "優待食事券",
                              "benefit_tiers": [{"shares": 100, "maximum_shares": None,
                                  "description": "優待食事券", "annual_value_yen": 2000}],
                              "long_term_required": False, "confidence_score": 95,
                              "valuation_type": "official_amount", "official_verified_at": "2026-07-26"})
            response = {"output_text": json.dumps(extracted), "output": [], "usage": {}}
            def fetched(url, _company, _sources, registered=False):
                self.assertTrue(registered)
                return url, "株主優待制度 100株 3月 9月 長期保有条件なし"
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "fetch_official_page", side_effect=fetched), \
                 patch.object(discovery, "request_response", return_value=response), \
                 patch.object(discovery, "build_payload", side_effect=AssertionError("web search must not run")):
                self.assertEqual(discovery.run(args), 0)
            benefits = json.loads((root / "benefits.json").read_text())
            self.assertEqual({item["code"] for item in benefits}, {code for code, _name in targets})
            self.assertTrue(all(item["benefit_status"] == "official_confirmed" for item in benefits))
            self.assertEqual(json.loads((root / "research-log.json").read_text()), [])

    def test_stale_candidate_falls_through_to_another_official_url(self):
        stale = "https://kyokuyo.co.jp/ir/old.pdf"
        current = "https://kyokuyo.co.jp/ir/current.pdf"
        checked = []
        def fetcher(url, *_args):
            checked.append(url)
            if url == stale:
                raise discovery.HTTPError(url, 404, "not found", {}, None)
            return url
        item = self.item(stale)
        selected, _ = discovery.select_verified_source(item, self.company,
                                                        {stale: {}, current: {}}, fetcher)
        self.assertEqual(selected, current)
        self.assertEqual(checked, [stale, current])

    def test_registered_official_domain_accepts_title_identity_without_code(self):
        requested = "https://www.kyokuyo.co.jp/ir/concept/"
        search_url = "https://kyokuyo.co.jp/ir/concept?utm_source=search"
        html = "<html><head><title>株主優待 | 株式会社 極洋</title></head><body>株主優待</body></html>"
        class Response:
            status = 200
            def geturl(self): return requested
            def read(self, _limit): return html.encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        with patch.object(discovery, "urlopen", return_value=Response()) as opener:
            final, _ = discovery.fetch_official_page(requested, self.company, {search_url})
        self.assertEqual(final, requested)
        self.assertIn("Mozilla/5.0", opener.call_args.args[0].headers["User-agent"])

    def test_meta_identity_is_searchable(self):
        html = '<meta property="og:site_name" content="株式会社 極洋"><main>株主優待</main>'
        text = discovery.page_text(html.encode())
        self.assertIn("株式会社 極洋", text)

    def test_json_ld_and_next_data_are_searchable(self):
        html = '''<script type="application/ld+json">{"description":"株主優待 100株"}</script>
                  <script id="__NEXT_DATA__" type="application/json">{"record":"3月末"}</script>
                  <script>document.write("discard me")</script>'''
        text = discovery.page_text(html.encode())
        self.assertIn("株主優待 100株", text)
        self.assertIn("3月末", text)
        self.assertNotIn("discard me", text)

    def test_registered_overview_follows_official_pdf_exactly_one_level(self):
        overview = "https://example.co.jp/ir/stockholder/"
        pdf = "https://example.co.jp/ir/library/benefit.pdf"
        company = {"code": "7616", "name": "コロワイド", "official_domain": "example.co.jp"}
        class Headers(dict):
            def get(self, key, default=""): return super().get(key, default)
        class Response:
            status = 200
            def __init__(self, url, body, content_type):
                self.url, self.body = url, body
                self.headers = Headers({"Content-Type": content_type})
            def geturl(self): return self.url
            def read(self, _limit): return self.body
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        responses = [
            Response(overview, f'<a href="{pdf}">株主優待制度 PDF</a>'.encode(), "text/html"),
            Response(pdf, "株主優待制度 500株 20,000ポイント 3月末 9月末 長期保有条件なし".encode(),
                     "application/pdf"),
        ]
        with patch.object(discovery, "urlopen", side_effect=responses), \
             patch.object(discovery.subprocess, "run", side_effect=FileNotFoundError):
            final, text = discovery.fetch_official_page(overview, company, {}, registered=True)
        self.assertEqual(final, pdf)
        self.assertIn("500株", text)
        self.assertEqual(len(responses), 2)

    def test_registered_redirect_accepts_explicit_official_alias(self):
        source = "https://yoshinoya-holdings.com/ir/stock/benefit/"
        final = "https://yoshinoya-holdings.jp/ir/stock/benefit/"
        company = {"code": "9861", "name": "吉野家ホールディングス",
                   "official_domain": "yoshinoya-holdings.com",
                   "official_domains": ["yoshinoya-holdings.com", "yoshinoya-holdings.jp"]}
        class Response:
            status = 200
            headers = {}
            def geturl(self): return final
            def read(self, _limit): return "株主優待制度 100株 2月 8月 長期保有条件なし".encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        with patch.object(discovery, "urlopen", return_value=Response()):
            self.assertEqual(discovery.fetch_official_page(
                source, company, {}, registered=True, follow_links=False)[0], final)

    def test_jpx_own_ir_page_is_never_company_disclosure(self):
        url = "https://jpx.co.jp/corporate/investor-relations/shareholders/incentives/index.html"
        company = {"code": "1301", "name": "極洋", "official_domain": None}
        class Response:
            status = 200
            def geturl(self): return url
            def read(self, _limit): return "極洋 1301 株主優待制度".encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        with patch.object(discovery, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "jpx_corporate_page"):
                discovery.fetch_official_page(url, company, {url})

    def test_jpx_overview_is_replaced_by_linked_corporate_ir_evidence(self):
        overview = "https://jpx.co.jp/corporate/investor-relations/example.html"
        disclosure = "https://kyokuyo.co.jp/ir/benefit.pdf"
        class Response:
            status = 200
            def geturl(self): return overview
            def read(self, _limit): return f'<a href="{disclosure}">公式IR</a>'.encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        checked = []
        with patch.object(discovery, "urlopen", return_value=Response()):
            selected, _ = discovery.select_verified_source(
                self.item(overview), self.company, {overview: {}},
                lambda url, *_args: checked.append(url) or url)
        self.assertEqual(selected, disclosure)
        self.assertEqual(checked, [disclosure])

    def test_kyokuyo_official_result_keeps_current_benefit_details(self):
        url = "https://www.kyokuyo.co.jp/ir/concept/"
        item = self.item(url)
        item.update({"name": "株式会社 極洋", "record_date": "3月31日", "annual_occurrences": 1,
                     "benefit_title": "自社製品", "annual_value_yen": 2500,
                     "conditions": "100株以上300株未満は2,500円相当、300株以上は6,000円相当。毎年7月贈呈予定"})
        validated, reasons = discovery.validate(item, self.company, {url: {}}, fetcher=lambda *args: url)
        self.assertFalse(reasons)
        self.assertEqual(validated["record_months"], [3])
        self.assertEqual(validated["annual_value_yen"], 2500)
        self.assertIn("300株以上は6,000円相当", validated["conditions"])
        self.assertGreaterEqual(validated["confidence_score"], 90)

    def test_other_company_abolition_cannot_mark_kyokuyo_abolished(self):
        item = self.item("https://www.jpx.co.jp/corporate/investor-relations/shareholders/incentives/index.html")
        item["benefit_status"] = "abolished"
        validated, reasons = discovery.validate(item, self.company, {}, fetcher=lambda *args: None)
        self.assertEqual(validated["benefit_status"], "candidate")
        self.assertIn("abolition_not_officially_confirmed", reasons)
        self.assertIsNone(validated["official_source_url"])

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

    def test_duplicate_search_call_ids_are_counted_once(self):
        response = {"output": [
            {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "queries": ["first"]}},
            {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "queries": ["first"]}},
        ]}
        stats = discovery.web_search_stats(response)
        self.assertEqual(stats["output_items"], 2)
        self.assertEqual(stats["unique_calls"], 1)
        self.assertEqual(stats["call_ids"], {"ws_1"})

    def test_idless_duplicate_search_actions_are_counted_once(self):
        action = {"type": "open_page", "status": "completed", "queries": [],
                  "sources": [{"url": "https://example.test/private"}]}
        response = {"output": [{"type": "web_search_call", "action": copy.deepcopy(action)},
                               {"type": "web_search_call", "action": copy.deepcopy(action)}]}
        self.assertEqual(discovery.web_search_stats(response)["unique_calls"], 1)

    def test_manual_selection_keeps_existing_confirmed_and_abolished_immutable(self):
        companies = [{"code": str(1000 + i), "name": str(i)} for i in range(15)]
        benefits = [{"code": str(1000 + i), "benefit_status": "official_confirmed"} for i in range(10)] + [
            {"code": "1010", "benefit_status": "abolished"}, {"code": "1011", "benefit_status": "abolished"}]
        args = type("Args", (), {"security_codes": "1010,1011,1012,1013,1014", "retry_failed": False,
                                  "batch_size": 10, "daily_limit": 20})()
        with patch.object(discovery, "DATA", Path("/nonexistent")):
            selected = discovery.choose(companies, args, {"next_index": 0}, benefits)
        self.assertEqual([x["code"] for x in selected], ["1012", "1013", "1014"])

    def test_no_eligible_source_means_no_range_style_master_scan(self):
        companies = [{"code": str(code), "name": str(code)} for code in range(1000, 1005)]
        args = type("Args", (), {"security_codes": "", "retry_failed": False,
                                  "batch_size": 2, "daily_limit": 20})()
        with tempfile.TemporaryDirectory() as directory, patch.object(discovery, "DATA", Path(directory)):
            selected = discovery.choose(companies, args, {"next_index": 3}, [])
        self.assertEqual(selected, [])

    def test_manual_codes_are_selected_in_order_including_a_code(self):
        companies = [{"code": "130A", "name": "A"}, {"code": "7550", "name": "B"},
                     {"code": "1375", "name": "C"}]
        args = type("Args", (), {"security_codes": "7550,130A", "batch_size": 5, "daily_limit": 20})()
        with tempfile.TemporaryDirectory() as directory, patch.object(discovery, "DATA", Path(directory)):
            selected = discovery.choose(companies, args, {}, [])
        self.assertEqual([item["code"] for item in selected], ["7550", "130A"])

    def test_tdnet_and_candidate_universe_are_eligible_but_unrelated_a_code_is_not(self):
        companies = [{"code": code, "name": code} for code in ("130A", "7550", "9861")]
        args = type("Args", (), {"security_codes": "", "batch_size": 5, "daily_limit": 20})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "benefit-universe.csv").write_text("code,name\n7550,test\n", encoding="utf-8")
            (root / "review-queue.json").write_text(json.dumps([
                {"title": "9861 株主優待制度の変更", "status": "pending"}
            ]), encoding="utf-8")
            with patch.object(discovery, "DATA", root):
                selected = discovery.choose(companies, args, {}, [])
        self.assertEqual([item["code"] for item in selected], ["7550", "9861"])

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

    def test_diagnostic_verification_required_exits_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {"listed-companies.json": [self.company], "company-domains.json": {"1301": "kyokuyo.co.jp"},
                        "benefits.json": [], "verification-queue.json": [],
                        "discovery-progress.json": {"next_index": 0}, "openai-api-usage.json": []}
            for name, value in fixtures.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            item = self.item(); item.update({"record_months": [], "record_date": None,
                                             "minimum_shares": None, "confidence_score": 70})
            response = {"output_text": json.dumps(item), "output": [{"type": "web_search_call", "action": {
                "sources": [{"url": item["official_source_url"]}]}}], "usage": {}}
            args = type("Args", (), {"diagnostic_mode": True, "batch_size": 10, "daily_limit": 20,
                "start_code": None, "end_code": None, "retry_failed": False, "official_only": False})()
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "request_response", return_value=response), \
                 patch.object(discovery, "fetch_and_validate", return_value=item["official_source_url"]), \
                 patch.object(discovery, "fetch_official_page", side_effect=ValueError("unavailable")), \
                 redirect_stdout(output):
                result = discovery.run(args)
            self.assertEqual(result, 0)
            self.assertIn("Diagnostic result: success_with_verification_required", output.getvalue())
            self.assertIn("Official validation: verification required", output.getvalue())

    def test_multiple_search_items_continue_through_official_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"listed-companies.json": [self.company], "company-domains.json": {},
                     "benefits.json": [], "verification-queue.json": [], "discovery-progress.json": {"next_index": 0},
                     "openai-api-usage.json": []}
            for name, value in files.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            url = self.item()["official_source_url"]
            search_response = {"output_text": json.dumps(self.item()), "output": [
                {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "sources": [{"url": url}]}},
                {"type": "web_search_call", "id": "ws_2", "action": {"type": "open_page", "sources": [{"url": url}]}},
            ], "usage": {}}
            args = type("Args", (), {"diagnostic_mode": True, "batch_size": 10, "daily_limit": 20,
                "start_code": None, "end_code": None, "retry_failed": False, "official_only": False})()
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-mock-secret-value"}), \
                 patch.object(discovery, "request_response", side_effect=[{"output": [], "usage": {}}, search_response]), \
                 patch.object(discovery, "fetch_and_validate", return_value=url), redirect_stdout(output):
                result = discovery.run(args)
            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Official URL validation: success", text)
            self.assertIn("Web-search Responses requests: 1", text)
            self.assertIn("Web-search output items: 2", text)
            self.assertIn("Unique web-search call IDs: 2", text)
            self.assertIn("Web-search action types: open_page, search", text)
            self.assertIn("Warning: multiple web_search_call output items", text)
            self.assertNotIn("more_than_one_search_call", text)
            self.assertNotIn(url, text)
            self.assertNotIn("sk-mock-secret-value", text)
            self.assertEqual({name: json.loads((root/name).read_text()) for name in files}, files)

    def test_usage_records_search_requests_and_output_items_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {"listed-companies.json": [self.company], "company-domains.json": {}, "benefits.json": [],
                        "verification-queue.json": [], "discovery-progress.json": {"next_index": 0},
                        "openai-api-usage.json": []}
            for name, value in fixtures.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            url = self.item()["official_source_url"]
            response = {"output_text": json.dumps(self.item()), "output": [
                {"type": "web_search_call", "id": "ws_same", "action": {"type": "search", "sources": [{"url": url}]}},
                {"type": "web_search_call", "id": "ws_same", "action": {"type": "search", "sources": [{"url": url}]}},
            ], "usage": {}}
            args = type("Args", (), {"diagnostic_mode": False, "batch_size": 10, "daily_limit": 20,
                "security_codes": "1301", "retry_failed": False, "official_only": False})()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "request_response", return_value=response), \
                 patch.object(discovery, "fetch_and_validate", return_value=url):
                self.assertEqual(discovery.run(args), 0)
            record = json.loads((root/"openai-api-usage.json").read_text())[-1]
            self.assertEqual(record["responses_with_web_search"], 1)
            self.assertEqual(record["web_search_output_items"], 2)
            self.assertEqual(record["web_search_calls"], 1)
            self.assertEqual(record["unique_web_search_call_ids"], 1)

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

    def test_429_is_persisted_as_a_failed_production_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); progress = {"next_index": 7, "processed_codes": ["9999"], "failed_codes": ["8888"]}
            fixtures = {"listed-companies.json": [self.company], "company-domains.json": {}, "benefits.json": [],
                        "verification-queue.json": [], "discovery-progress.json": progress, "openai-api-usage.json": []}
            for name, value in fixtures.items(): (root/name).write_text(json.dumps(value), encoding="utf-8")
            args = type("Args", (), {"diagnostic_mode": False, "batch_size": 10, "daily_limit": 20,
                "security_codes": "1301", "retry_failed": False, "official_only": False})()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "request_response", side_effect=discovery.APIError(429, "rate limited")):
                discovery.run(args)
            saved = json.loads((root/"discovery-progress.json").read_text())
            self.assertEqual(saved, progress)
            log = json.loads((root / "research-log.json").read_text())
            self.assertEqual(log[0]["code"], "1301")
            self.assertEqual(log[0]["result"], "api_failed")

    def test_five_production_targets_are_all_persisted_and_accounted_for(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companies = [{"code": str(1300 + index), "name": f"会社{index}",
                          "official_domain": "kyokuyo.co.jp"} for index in range(5)]
            fixtures = {"listed-companies.json": companies, "company-domains.json": {}, "benefits.json": [],
                        "verification-queue.json": [], "discovery-progress.json": {"next_index": 0},
                        "openai-api-usage.json": []}
            for name, value in fixtures.items():
                (root/name).write_text(json.dumps(value), encoding="utf-8")
            responses = []
            for company in companies:
                item = self.item()
                item.update({"code": company["code"], "name": company["name"]})
                responses.append({"output_text": json.dumps(item), "output": [{"type": "web_search_call", "action": {
                    "sources": [{"url": item["official_source_url"]}]}}], "usage": {}})
            args = type("Args", (), {"diagnostic_mode": False, "batch_size": 5, "daily_limit": 20,
                "security_codes": "1300,1301,1302,1303,1304", "retry_failed": False, "official_only": False})()
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "request_response", side_effect=responses), \
                 patch.object(discovery, "fetch_and_validate", side_effect=lambda url, *_args: url), \
                 redirect_stdout(output):
                self.assertEqual(discovery.run(args), 0)
            benefits = json.loads((root/"benefits.json").read_text())
            queue = json.loads((root/"verification-queue.json").read_text())
            progress = json.loads((root/"discovery-progress.json").read_text())
            confirmed = len(benefits)
            verification_queue = len(queue)
            failed = len(progress.get("failed_codes", []))
            self.assertEqual(confirmed + verification_queue + failed, 5)
            self.assertIn("Production targets (5):", output.getvalue())
            self.assertIn("confirmed=5 verification_queue=0 failed=0 skipped=0 selected=5", output.getvalue())
            for company in companies:
                self.assertIn(f'Result {company["code"]} {company["name"]}: confirmed', output.getvalue())


if __name__ == "__main__": unittest.main()
