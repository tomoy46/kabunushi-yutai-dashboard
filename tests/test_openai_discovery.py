import copy
import csv
import gzip
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import discover_benefits_with_openai as discovery


class OpenAIDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.company = {"code": "1301", "name": "極洋", "official_domain": "kyokuyo.co.jp"}
        self.blocked_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.blocked_directory.cleanup)
        self.blocked_patch = patch.object(
            discovery, "BLOCKED_OFFICIAL_URLS", Path(self.blocked_directory.name) / "blocked.json")
        self.blocked_patch.start()
        self.addCleanup(self.blocked_patch.stop)
        discovery.ROBOTS_CACHE.clear()
        self.openai_preflight = patch.object(discovery, "verify_openai_access", return_value={"id": "model"})
        self.openai_preflight.start()
        self.addCleanup(self.openai_preflight.stop)

    def item(self, url="https://www.kyokuyo.co.jp/ir/benefit.html"):
        value = {key: None for key in discovery.FIELDS}
        value.update({"code": "1301", "name": "極洋", "benefit_status": "official_confirmed",
                      "record_months": [3], "minimum_shares": 100, "confidence_score": 95,
                      "official_source_url": url, "valuation_type": "official_amount",
                      "official_verified_at": "2026-07-26"})
        return value

    @staticmethod
    def successful_pdf_conversion(text="株主優待制度 100株 3月 長期保有条件なし"):
        """Return a pdftotext mock that writes deterministic extracted fixture text."""
        def convert(command, **_kwargs):
            Path(command[-1]).write_text(text, encoding="utf-8")
            return type("Completed", (), {
                "returncode": 0, "stdout": b"", "stderr": b"",
            })()
        return convert

    def legacy_test_default_model_and_request_contract(self):
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
                     "long_term_condition_verified": True,
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

    def test_openai_request_uses_only_official_host_and_bearer_header(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self): return b'{"id":"model"}'
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return Response()
        with patch.object(discovery, "urlopen", side_effect=opener):
            discovery.openai_request("https://api.openai.com/v1/models/test", "GET", "secret", max_retries=1)
        self.assertEqual(captured[0].host, "api.openai.com")
        self.assertEqual(captured[0].get_method(), "GET")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer secret")
        with self.assertRaises(ValueError):
            discovery.openai_request("https://example.com/v1/models/test", "GET", "secret")

    def test_403_preflight_is_not_retried_and_has_zero_usage(self):
        error_body = json.dumps({"error": {"type": "permission_error", "code": "model_not_allowed",
                                           "message": "model denied"}}).encode()
        from urllib.error import HTTPError
        failure = HTTPError("https://api.openai.com/v1/models/test", 403, "Forbidden",
                            {"x-request-id": "req_403"}, None)
        failure.read = lambda: error_body
        with patch.object(discovery, "urlopen", side_effect=failure) as opener:
            with self.assertRaises(discovery.APIError) as raised:
                discovery.openai_request("https://api.openai.com/v1/models/test", "GET", "secret",
                                         max_retries=3, stage="openai_auth_check")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.request_id, "req_403")
        self.assertEqual(raised.exception.stage, "openai_auth_check")
        output = "\n".join(discovery.safe_error_lines(raised.exception, "secret"))
        for field in ("Processing stage: openai_auth_check", "Security code: workflow",
                      "Request URL: https://api.openai.com/v1/models/test",
                      "Request host: api.openai.com", "HTTP method: GET", "HTTP status: 403",
                      "Response body (first 300 chars)", "Exception class: APIError",
                      "Request ID: req_403", "Error type: permission_error",
                      "Error code: model_not_allowed", "Error message: model denied"):
            self.assertIn(field, output)

    def test_official_site_403_is_logged_and_classified_separately(self):
        error_body = b"blocked by corporate WAF"
        failure = HTTPError("https://example.co.jp/ir/benefit", 403, "Forbidden",
                            {"Content-Type": "text/html"}, None)
        failure.read = lambda *_args: error_body
        company = {"code": "9999", "name": "Example", "official_domain": "example.co.jp"}
        output = StringIO()
        discovery.HTTP_403_EVENTS.clear()
        with patch.object(discovery, "urlopen", side_effect=failure), redirect_stdout(output):
            with self.assertRaises(discovery.OfficialSourceFetchError) as raised:
                discovery.fetch_official_page("https://example.co.jp/ir/benefit", company,
                                              ["https://example.co.jp/ir/benefit"])
        self.assertEqual(raised.exception.reason, "official_site_forbidden")
        self.assertEqual(discovery.HTTP_403_EVENTS[-1]["stage"], "official_site_fetch")
        self.assertIn("security_code=9999", output.getvalue())
        self.assertIn("response_body=blocked by corporate WAF", output.getvalue())
        self.assertNotIn("Authorization", output.getvalue())

    def test_browser_headers_and_same_origin_referer(self):
        headers = discovery.browser_headers("https://example.co.jp/ir/", "https://example.co.jp/")
        for name in ("User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "Connection", "Referer"):
            self.assertIn(name, headers)
        self.assertNotIn("Referer", discovery.browser_headers(
            "https://example.co.jp/ir/", "https://outside.example/"))

    def test_robots_disallow_and_403_cooldown(self):
        discovery.ROBOTS_CACHE["https://example.co.jp"] = "User-agent: *\nDisallow: /private/"
        self.assertFalse(discovery.robots_allowed("https://example.co.jp/private/benefit"))
        self.assertTrue(discovery.robots_allowed("https://example.co.jp/ir/benefit"))
        now = discovery.dt.datetime(2026, 7, 29, tzinfo=discovery.dt.timezone.utc)
        discovery.remember_official_403("https://example.co.jp/ir/blocked", "9999", now)
        self.assertTrue(discovery.official_request_blocked(
            "https://example.co.jp/ir/alternative", now + discovery.dt.timedelta(hours=23)))
        self.assertFalse(discovery.official_request_blocked(
            "https://example.co.jp/ir/alternative", now + discovery.dt.timedelta(hours=25)))

    def test_expanded_free_extraction_and_bounded_excerpt(self):
        text = "株主優待制度 保有株式数100株以上 自社商品を贈呈 毎年3月末現在の株主名簿に記載された株主"
        facts = discovery.evidence_facts(text)
        self.assertTrue(all(facts[name] for name in ("required_shares", "benefit_content", "record_month")))
        self.assertEqual(discovery.regex_official_facts("1単元(100株) 2月末日および8月末日"),
                         {"minimum_shares": 100, "record_months": [2, 8]})
        excerpt = discovery.benefit_excerpt("無関係" * 10000 + text + "末尾" * 10000)
        self.assertIn("株主優待", excerpt)
        self.assertLessEqual(len(excerpt), 20_000)

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

    def test_non_official_finance_media_and_blogs_have_auditable_rejection(self):
        for url in ("https://www.nikkei.com/company/1301/",
                    "https://finance.yahoo.co.jp/quote/1301.T",
                    "https://minkabu.jp/stock/1301/yutai",
                    "https://kabutan.jp/stock/yutai?code=1301",
                    "https://irbank.net/1301/yutai",
                    "https://example.wordpress.com/yutai"):
            with self.subTest(url=url):
                self.assertEqual(discovery.official_url_decision(url, self.company),
                                 (False, "rejected_non_official"))

    def test_corporate_homepage_is_accepted_without_benefit_word(self):
        html = """<html><title>株式会社極洋 公式サイト</title>
        <body><nav>企業情報 IR 投資家情報</nav><p>証券コード 1301</p></body></html>"""
        self.assertTrue(discovery.corporate_identity_matches(
            self.company, "https://www.kyokuyo.co.jp/", html.encode()))

    def test_identity_can_use_corporate_number_or_address(self):
        company = {**self.company, "corporate_number": "1234567890123"}
        html = "<html><title>企業情報</title><body>法人番号 1234567890123 company profile</body></html>"
        self.assertTrue(discovery.corporate_identity_matches(
            company, "https://example.co.jp/company/", html.encode()))

    def test_group_domain_must_name_listed_holding_company(self):
        company = {"code": "9999", "name": "サンプルホールディングス"}
        subsidiary = "<html><title>サンプル株式会社</title><body>会社概要 IR 証券コード9999</body></html>"
        listed = "<html><title>サンプルホールディングス</title><body>会社概要 IR</body></html>"
        self.assertFalse(discovery.corporate_identity_matches(
            company, "https://sample.example/", subsidiary.encode()))
        self.assertTrue(discovery.corporate_identity_matches(
            company, "https://sample.example/ir/", listed.encode()))

    def test_verified_company_domain_cache_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official-company-domains.json"
            discovery.save_company_domain(path, self.company, "https://www.kyokuyo.co.jp/")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["1301"]["domain"], "kyokuyo.co.jp")
            self.assertEqual(saved["1301"]["url"], "https://www.kyokuyo.co.jp/")

    def test_official_candidate_reasons_distinguish_company_ir_and_exchange(self):
        company = dict(self.company, official_domains=["ir.kyokuyo.co.jp"])
        self.assertEqual(discovery.official_url_decision(
            "https://kyokuyo.co.jp/ir/", company), (True, "official_company_domain"))
        self.assertEqual(discovery.official_url_decision(
            "https://ir.kyokuyo.co.jp/news.pdf", company), (True, "official_ir_subdomain"))
        self.assertEqual(discovery.official_url_decision(
            "https://www.release.tdnet.info/inbs/a.pdf", company),
            (True, "official_exchange_disclosure"))

    def test_share_wording_is_recognized_in_tables_ocr_and_pdf_text(self):
        for text in ("100株以上 優待券 基準日3月末日", "1単元以上 株主優待 9月末日",
                     "保有株式数100株 株主優待制度 3月末日",
                     "所有株式数に応じて 株主優待ポイント 基準日12月"):
            with self.subTest(text=text):
                self.assertTrue(discovery.evidence_facts(text)["required_shares"])
        self.assertEqual(discovery.regex_official_facts(
            "画像説明 保有株式数100株以上 株主優待 3月末日")["minimum_shares"], 100)
        self.assertEqual(discovery.regex_official_facts(
            "PDF本文 1単元以上 株主優待 9月末日")["minimum_shares"], 100)

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
        convert = self.successful_pdf_conversion()
        with patch.object(discovery, "urlopen", return_value=Response()), \
             patch.object(discovery.subprocess, "run", side_effect=convert):
            with self.assertRaisesRegex(ValueError, "exchange_disclosure_identity_mismatch"):
                discovery.fetch_official_page(url, company, {url})
        with patch.object(discovery, "urlopen", return_value=Response()), \
             patch.object(discovery.subprocess, "run", side_effect=convert):
            metadata = {url: {"title": "旧社名・ブランド名 証券コード1301"}}
            self.assertEqual(discovery.fetch_official_page(url, company, metadata)[0], url)

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
                with patch.object(discovery, "urlopen", return_value=Response()), \
                     patch.object(discovery.subprocess, "run", side_effect=
                                  self.successful_pdf_conversion()):
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
        pdf = b"%PDF-test-fixture"
        with patch.object(discovery, "urlopen", return_value=Response(pdf_url, pdf, "application/pdf")), \
             patch.object(discovery.subprocess, "run", side_effect=
                          self.successful_pdf_conversion("ブランド表記のみ 株主優待制度 100株 3月")):
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

    def test_colowide_and_atom_byte_response_metadata_does_not_reach_text_comparisons(self):
        """Real urllib metadata may be bytes; the two affected issuers must remain safe."""
        targets = (
            ("7616", "コロワイド", "https://www.colowide.co.jp/ir/stock_info/stockholder/"),
            ("7412", "アトム", "https://www.atom-corp.co.jp/ir/shareholder.html"),
        )

        class Headers(dict):
            def get(self, key, default=""): return super().get(key, default)

        class Response:
            status = 200
            headers = Headers({"Content-Type": b"text/html; charset=utf-8",
                               "Content-Encoding": b"identity"})
            def __init__(self, url): self.url = url
            def geturl(self): return self.url.encode("utf-8")
            def read(self, _limit): return bytearray("株主優待制度 100株 3月", "utf-8")
            def __enter__(self): return self
            def __exit__(self, *_args): pass

        for code, name, url in targets:
            with self.subTest(code=code), patch.object(
                    discovery, "urlopen", return_value=Response(url)):
                final, text = discovery.fetch_official_page(
                    url, {"code": code, "name": name,
                          "official_domain": discovery.normalized_host(url)},
                    {}, registered=True, follow_links=False)
                self.assertEqual(final, url)
                self.assertIn("株主優待制度", text)

    def test_registered_image_is_not_accepted_as_benefit_body(self):
        url = "https://example.co.jp/ir/images/shareholder-benefit.jpg"

        class Response:
            status = 200
            headers = {"Content-Type": "image/jpeg"}
            def geturl(self): return url
            def read(self, _limit): return b"stockholder benefit image"
            def __enter__(self): return self
            def __exit__(self, *_args): pass

        with patch.object(discovery, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "image_not_document"):
                discovery.fetch_official_page(
                    url, {"code": "9861", "name": "吉野家ホールディングス",
                          "official_domain": "example.co.jp"},
                    {}, registered=True, follow_links=False)

    def test_pdf_conversion_failure_reports_exit_code_and_stderr(self):
        completed = type("Completed", (), {"returncode": 1, "stderr": b"syntax error"})()
        diagnostics = []
        with patch.object(discovery.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(discovery.OfficialSourceFetchError, "pdf_conversion_failure"):
                discovery.pdf_text(b"%PDF-broken", lambda **values: diagnostics.append(values))
        self.assertEqual(diagnostics, [{"returncode": 1, "stderr": "syntax error"}])

    def test_real_pdf_with_empty_conversion_is_not_sent_as_binary_text(self):
        completed = type("Completed", (), {"returncode": 0, "stderr": b""})()
        with patch.object(discovery.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(discovery.OfficialSourceFetchError, "produced no text"):
                discovery.pdf_text(b"%PDF-1.7 binary only")

    def test_pdf_evidence_fact_diagnostics_cover_required_fields(self):
        facts = discovery.evidence_facts(
            "必要株数 100株 優待内容 お食事券2,000円 権利確定月 3月 長期保有条件 1年以上")
        self.assertEqual(facts, {"required_shares": True, "benefit_content": True,
                                 "record_month": True, "long_term_condition": True})

    def test_core_official_fixtures_confirm_without_long_term_text_or_openai(self):
        fixtures = {
            "7550": ("ゼンショーホールディングス", "100株 優待券1,000円 基準日 3月末"),
            "9861": ("吉野家ホールディングス", "100株 株主優待券 2月末および8月末"),
            "7616": ("コロワイド", "500株 優待ポイント 3月末・9月末"),
            "7412": ("アトム", "100株 優待ポイント 基準日 3月31日、9月30日"),
        }
        with patch.object(discovery, "request_response") as openai:
            for code, (name, evidence) in fixtures.items():
                with self.subTest(code=code):
                    item = self.item()
                    item.update({"code": code, "name": name, "benefit_status": "candidate",
                                 "confidence_score": 65, "long_term_required": None})
                    result, facts, stale = discovery.apply_official_evidence_policy(
                        item, evidence, f"https://example.co.jp/{code}/benefit.html")
                    self.assertTrue(all(facts[key] for key in
                                        ("required_shares", "benefit_content", "record_month")))
                    self.assertFalse(stale)
                    self.assertEqual(result["benefit_status"], "official_confirmed")
                    self.assertGreaterEqual(result["confidence_score"], 90)
                    self.assertFalse(result["long_term_required"])
                    self.assertFalse(result["long_term_condition_verified"])
                    stored = discovery.normalize_for_storage(result, {})
                    self.assertEqual(stored["long_term_condition"],
                                     "公式資料に長期保有条件の記載なし")
        openai.assert_not_called()

    def test_only_explicit_no_long_term_wording_is_verified(self):
        base = self.item()
        missing, _, _ = discovery.apply_official_evidence_policy(
            dict(base), "100株 優待券1,000円 基準日3月末", base["official_source_url"])
        explicit, _, _ = discovery.apply_official_evidence_policy(
            dict(base), "100株 優待券1,000円 基準日3月末 長期保有条件はありません",
            base["official_source_url"])
        self.assertFalse(missing["long_term_condition_verified"])
        self.assertTrue(explicit["long_term_condition_verified"])
        self.assertEqual(discovery.normalize_for_storage(explicit, {})["long_term_condition"], "なし")

    def test_old_year_pdf_is_not_current_program_evidence(self):
        item = self.item("https://example.co.jp/benefit-2024.pdf")
        item.update({"benefit_status": "candidate", "confidence_score": 70})
        result, _, stale = discovery.apply_official_evidence_policy(
            item, "2024年度 株主優待 100株 優待券1,000円 基準日3月末", item["official_source_url"])
        self.assertTrue(stale)
        self.assertEqual(result["benefit_status"], "candidate")

    def test_research_log_replaces_the_same_pdf_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(discovery, "DATA", Path(directory)):
                company = {"code": "7550", "name": "ゼンショー"}
                url = "https://example.co.jp/benefit.pdf"
                discovery.append_research_log(company, "failed", ["first"], url)
                discovery.append_research_log(company, "failed", ["second"], url)
                entries = json.loads((Path(directory) / "research-log.json").read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reasons"], ["second"])

    def test_production_preflight_stops_before_openai_when_pdftotext_is_missing(self):
        args = type("Args", (), {"diagnostic_mode": False})()
        with patch.object(discovery, "is_test_fixture", return_value=False), \
             patch.object(discovery, "pdf_extractor_available", return_value=False), \
             patch.object(discovery, "request_response") as request, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), redirect_stdout(StringIO()):
            self.assertEqual(discovery.run(args), 1)
        request.assert_not_called()

    def test_403_preflight_aborts_remaining_four_companies_without_research_log_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companies = [{"code": str(1301 + index), "name": f"会社{index}",
                          "official_domain": "example.co.jp"} for index in range(5)]
            fixtures = {"listed-companies.json": companies, "company-domains.json": {},
                        "benefits.json": [], "verification-queue.json": [], "research-log.json": [],
                        "review-queue.json": [], "discovery-progress.json": {}, "openai-api-usage.json": []}
            for name, value in fixtures.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            args = type("Args", (), {"diagnostic_mode": False,
                "security_codes": ",".join(company["code"] for company in companies),
                "companies_per_run": 5, "max_openai_calls_per_run": 5,
                "max_openai_calls_per_day": 100, "max_openai_budget_jpy_per_day": 100,
                "retry_failed": False, "retry_research_log": False, "official_only": False})()
            denial = discovery.APIError(403, "denied", "permission_error", "model_not_allowed",
                                        request_id="req_denied", method="GET",
                                        endpoint="https://api.openai.com/v1/models/gpt-5.4-nano",
                                        stage="openai_auth_check")
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "discover_verified_official_source",
                              return_value=("https://example.co.jp/benefit", "株主優待 100株 3月 優待券")), \
                 patch.object(discovery, "verify_openai_access", side_effect=denial) as preflight, \
                 patch.object(discovery, "request_response") as response, redirect_stdout(output):
                self.assertEqual(discovery.run(args), 1)
            preflight.assert_called_once()
            response.assert_not_called()
            self.assertEqual(json.loads((root / "research-log.json").read_text()), [])
            self.assertIn("deferred_companies=4", output.getvalue())
            workflow_error = json.loads((root / ".discovery-results" / "workflow-error.json").read_text())
            self.assertEqual(workflow_error["status"], 403)
            self.assertEqual(workflow_error["token_usage"]["input_tokens"], 0)

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
            output = StringIO()
            with patch.object(discovery, "DATA", root), patch.dict(os.environ, {"OPENAI_API_KEY": "mock"}), \
                 patch.object(discovery, "fetch_official_page", side_effect=fetched), \
                 patch.object(discovery, "discover_corporate_candidates", return_value=[]), \
                 patch.object(discovery, "request_response", return_value=response), \
                 patch.object(discovery, "build_payload", side_effect=AssertionError("web search must not run")), \
                 redirect_stdout(output):
                self.assertEqual(discovery.run(args), 0)
            benefits = json.loads((root / "benefits.json").read_text())
            self.assertEqual({item["code"] for item in benefits}, {code for code, _name in targets})
            with (root / "benefits.csv").open(encoding="utf-8", newline="") as stream:
                csv_codes = {row["code"] for row in csv.DictReader(stream)}
            self.assertEqual(csv_codes, {code for code, _name in targets})
            self.assertTrue(all(item["benefit_status"] == "official_confirmed" for item in benefits))
            self.assertEqual(json.loads((root / "research-log.json").read_text()), [])
            log = output.getvalue()
            self.assertTrue(log.startswith("TEST FIXTURE\n"))
            self.assertNotIn("PRODUCTION TARGETS", log)
            self.assertNotIn("PRODUCTION RESULT", log)
            self.assertNotIn("PRODUCTION SUMMARY", log)
            self.assertIn("FIXTURE SUMMARY: confirmed=5", log)

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
            Response(pdf, b"%PDF-test-fixture",
                     "application/pdf"),
        ]
        with patch.object(discovery, "urlopen", side_effect=responses), \
             patch.object(discovery.subprocess, "run", side_effect=self.successful_pdf_conversion(
                 "株主優待制度 500株 20,000ポイント 3月末 9月末 長期保有条件なし")):
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

    def test_auto_selection_filters_ineligible_and_recent_outcomes(self):
        now = discovery.dt.datetime(2026, 7, 28, tzinfo=discovery.dt.timezone.utc)
        companies = [
            {"code": "1000", "name": "new", "market": "プライム"},
            {"code": "1001", "name": "recent research", "market": "スタンダード"},
            {"code": "1002", "name": "recent failure", "market": "グロース"},
            {"code": "1003", "name": "上場ETF", "market": "ETF"},
            {"code": "1004", "name": "delisted", "market": "プライム", "delisted": True},
        ]
        args = type("Args", (), {"security_codes": "", "auto_select": True,
            "retry_research_log": False, "retry_failed": False, "batch_size": 20})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research-log.json").write_text(json.dumps([
                {"code": "1001", "result": "not_officially_verified", "checked_at": "2026-07-10T00:00:00Z"},
                {"code": "1002", "result": "failed", "checked_at": "2026-07-25T00:00:00Z"},
            ]), encoding="utf-8")
            with patch.object(discovery, "DATA", root):
                selected = discovery.choose(companies, args, {}, [], now=now)
        self.assertEqual([item["code"] for item in selected], ["1000"])

    def test_free_priority_uses_official_titles_paths_and_disclosure_titles(self):
        companies = [{"code": str(1000 + i), "name": f"company{i}"} for i in range(30)]
        companies[20]["page_title"] = "株主様ご優待制度"
        review = [{"code": "1021", "title": "株主優待カードの贈呈", "url": "https://tdnet.info/a"}]
        selected = discovery.quota_order(companies, 25, review, {}, set())
        self.assertEqual({c["code"] for c in selected[:2]}, {"1020", "1021"})
        self.assertEqual(sum(c["candidate_priority"] == "low" for c in selected), 23)
        self.assertGreaterEqual(sum(c["candidate_priority"] == "low" for c in selected[-3:]), 3)

    def test_free_priority_fixtures_produce_high_medium_and_low(self):
        fixtures = [
            {"code": "2001", "name": "high", "h1": " 株主\n優待のご案内 "},
            {"code": "2002", "name": "medium", "meta_description": "株主ご優待&#21046;度"},
            {"code": "2003", "name": "low"},
        ]
        priorities = [discovery.free_priority(item)[1] for item in fixtures]
        self.assertEqual(priorities, ["high", "medium", "low"])

    def test_pdf_or_tdnet_shareholder_benefit_program_title_is_high(self):
        company = {"code": "2004", "name": "pdf", "official_pdf_name":
                   "株主優待制度のお知らせ.pdf"}
        self.assertEqual(discovery.free_priority(company)[1], "high")
        review = [{"code": "2005", "title": "株主優待制度の新設について"}]
        self.assertEqual(discovery.free_priority({"code": "2005", "name": "tdnet"}, review)[1],
                         "high")

    def test_page_metadata_and_regex_facts_are_normalized(self):
        html = """<title> 株主\n優待 </title><meta name="description" content="株主&amp;ご優待">
        <h1>株主優待</h1><a href="benefit.pdf">優待券</a>１００株 1,000株 毎年３月３１日 ８月末日"""
        text = discovery.page_text(html.encode())
        self.assertIn("PAGE_TITLE[株主 優待]", text)
        self.assertIn("H1[株主優待]", text)
        self.assertIn("LINK_TEXT[優待券]", text)
        self.assertEqual(discovery.regex_official_facts(text),
                         {"minimum_shares": 100, "record_months": [3, 8]})

    def test_sparse_official_evidence_reason_classification(self):
        facts = {"required_shares": True, "benefit_content": False, "record_month": False}
        self.assertEqual(discovery.classified_reasons([], facts),
                         ["benefit_content_missing", "record_month_missing"])
        self.assertEqual(discovery.classified_reasons(["redirect_host_not_verified"]),
                         ["redirect_domain_rejected"])

    def test_manual_codes_take_priority_over_auto_order_and_batch_is_capped_at_twenty(self):
        companies = [{"code": str(1000 + i), "name": str(i)} for i in range(30)]
        args = type("Args", (), {"security_codes": "1025,1002", "auto_select": True,
            "retry_research_log": False, "retry_failed": False, "batch_size": 99})()
        with tempfile.TemporaryDirectory() as directory, patch.object(discovery, "DATA", Path(directory)):
            selected = discovery.choose(companies, args, {}, [])
        self.assertEqual([item["code"] for item in selected], ["1025", "1002"])

    def test_daily_openai_call_counter_uses_only_current_utc_day(self):
        now = discovery.dt.datetime(2026, 7, 28, 12, tzinfo=discovery.dt.timezone.utc)
        records = [{"executed_at": "2026-07-28T01:00:00Z", "responses_api_calls": 12},
                   {"executed_at": "2026-07-27T23:00:00Z", "responses_api_calls": 20}]
        self.assertEqual(discovery.calls_today(records, now), 12)

    def test_jpy_cost_uses_uncached_cached_and_output_rates(self):
        pricing = {"input_usd_per_million": 1, "cached_input_usd_per_million": .1,
                   "output_usd_per_million": 2, "usd_to_jpy": 100}
        cost = discovery.estimated_cost_jpy(
            {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}, pricing)
        self.assertEqual(cost, 0.104)

    def test_daily_cost_uses_only_current_utc_day(self):
        now = discovery.dt.datetime(2026, 7, 28, 12, tzinfo=discovery.dt.timezone.utc)
        records = [{"executed_at": "2026-07-28T01:00:00Z", "estimated_cost_jpy": 12.5},
                   {"executed_at": "2026-07-27T23:00:00Z", "estimated_cost_jpy": 80}]
        self.assertEqual(discovery.cost_today(records, now), 12.5)

    def test_parser_has_budget_defaults(self):
        args = discovery.parser().parse_args([])
        self.assertEqual(args.companies_per_run, 25)
        self.assertEqual(args.max_openai_calls_per_run, 25)
        self.assertEqual(args.max_openai_calls_per_day, 100)
        self.assertEqual(args.max_openai_budget_jpy_per_day, 100)

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
                 patch.object(discovery, "discover_corporate_candidates", return_value=[]), \
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
                 patch.object(discovery, "discover_corporate_candidates", return_value=[]), \
                 patch.object(discovery, "fetch_and_validate", return_value=item["official_source_url"]), \
                 patch.object(discovery, "fetch_official_page", side_effect=ValueError("unavailable")), \
                 redirect_stdout(output):
                result = discovery.run(args)
            self.assertEqual(result, 0)
            self.assertIn("Diagnostic result: success_with_verification_required", output.getvalue())
            self.assertIn("Official validation: verification required", output.getvalue())

    def test_diagnostic_outcomes_fail_only_for_actual_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "diagnostic-outcomes.json"
            cases = [
                ({"verification_required": 1, "failures": 0}, None,
                 "success_with_verification_required", 0),
                ({"verification_required": 0, "failures": 0}, None, "success", 0),
                ({"verification_required": 0, "failures": 1}, None, "failure", 1),
                ({"verification_required": 1, "failures": 0}, "official_discovery", "failure", 1),
            ]
            fixture.write_text(json.dumps(cases), encoding="utf-8")
            for totals, failed_stage, expected_result, expected_exit_code in json.loads(
                    fixture.read_text(encoding="utf-8")):
                with self.subTest(result=expected_result, failed_stage=failed_stage):
                    self.assertEqual(
                        discovery.diagnostic_outcome(totals, failed_stage),
                        (expected_result, expected_exit_code),
                    )

    def test_nine_company_failures_with_one_confirmed_is_partial_success(self):
        totals = {"successes": 1, "research_log_saved": 9, "unresolved": 0,
                  "failures": 9}
        self.assertEqual(discovery.production_outcome(totals, 10), (True, False, 0))

    def test_all_companies_failed_without_saved_result_is_failure(self):
        totals = {"successes": 0, "research_log_saved": 0, "unresolved": 0,
                  "failures": 9}
        self.assertEqual(discovery.production_outcome(totals, 9), (False, False, 1))

    def test_broken_benefits_csv_is_a_structural_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benefits.csv"
            path.write_text("wrong,columns\nvalue,other\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                discovery.validate_benefits_csv(path)

    def legacy_test_multiple_search_items_continue_through_official_validation(self):
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

    def legacy_test_usage_records_search_requests_and_output_items_separately(self):
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

    def legacy_test_diagnostic_failure_prints_safe_error_and_final_summary(self):
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

    def legacy_test_429_is_persisted_as_a_failed_production_outcome(self):
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

    def legacy_test_five_production_targets_are_all_persisted_and_accounted_for(self):
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

class GenericOfficialDiscoveryTests(unittest.TestCase):
    """Regression coverage uses invented issuers, never production exceptions."""

    def test_openai_payload_has_no_search_or_fetch_tool(self):
        company = {"code": "4321", "name": "新規テスト", "official_domain": "new.example"}
        payload = discovery.build_payload(company, "株主優待 100株")
        self.assertNotIn("tools", payload)
        self.assertIn("株主優待 100株", payload["input"])

    def test_twenty_five_company_regression_keeps_all_priority_bands_eligible(self):
        """Workflow #40 must not collapse a 25-company batch to one API call."""
        fixtures = ([('high', 1)] + [('medium', 1)] + [('low', 0)] * 23)
        sent = []
        low_sent = 0
        for priority, facts in fixtures:
            if discovery.openai_eligible(priority, [f"https://{priority}.example/ir"], facts, low_sent):
                sent.append(priority)
                low_sent += priority == "low"
        self.assertGreaterEqual(len(sent), 7)
        self.assertGreaterEqual(sent.count("low"), 5)
        self.assertEqual({"high", "medium", "low"}, set(sent))

    def test_openai_gate_requires_a_url_or_pdf_not_two_extracted_fields(self):
        self.assertTrue(discovery.openai_eligible("low", ["https://issuer.example/"], 0, 0))
        self.assertTrue(discovery.openai_eligible("high", ["https://issuer.example/notice.pdf"], 1, 0))
        self.assertFalse(discovery.openai_eligible("high", [], 3, 0))

    def test_unresolved_is_separate_from_research_log(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(discovery, "DATA", Path(directory)):
                discovery.append_unresolved({"code": "4321", "name": "新規テスト"},
                                            ["official_site_discovery_failed"],
                                            ["https://new.example/", "https://new.example/ir/"])
            unresolved = json.loads((Path(directory) / "unresolved.json").read_text())
            self.assertEqual(unresolved[0]["result"], "official_site_discovery_failed")
            self.assertFalse((Path(directory) / "research-log.json").exists())

    def test_static_html_javascript_json_and_pdf_share_one_crawler(self):
        company = {"code": "4321", "name": "新規テスト", "official_domain": "new.example"}
        home = "https://new.example/"
        static = "https://new.example/investors/reward.html"
        javascript = "https://new.example/assets/state.json"
        pdf = "https://new.example/documents/notice.pdf"
        documents = {
            home: (home, f'<a href="{static}">株主優待</a><script type="application/json">{{"api":"{javascript}"}}</script>'.encode(), "text/html"),
            "https://new.example/sitemap.xml": ("https://new.example/sitemap.xml", f'<loc>{pdf}</loc>'.encode(), "application/xml"),
            static: (static, f'株主優待制度 100株 <a href="{pdf}">公式PDF</a>'.encode(), "text/html"),
            javascript: (javascript, b'{"title":"shareholder benefit","shares":100}', "application/json"),
        }
        def fetch(url, _domains):
            if url not in documents: raise ValueError("fixture absent")
            return documents[url]
        candidates = discovery.discover_corporate_candidates(company, fetcher=fetch)
        self.assertIn(static, candidates)
        self.assertIn(javascript, candidates)
        self.assertIn(pdf, candidates)
        self.assertLess(candidates.index(static), candidates.index(pdf))

    def test_priority_404_falls_back_then_exchange_pdf(self):
        company = {"code": "4321", "name": "新規テスト", "official_domain": "new.example"}
        stale = "https://new.example/old"
        corporate = "https://new.example/current"
        exchange = "https://www.release.tdnet.info/inbs/current.pdf"
        calls = []
        def fetch(url, *_args, **_kwargs):
            calls.append(url)
            if url in (stale, corporate): raise discovery.OfficialSourceNotFound()
            return url, "4321 株主優待制度 100株"
        found, _ = discovery.discover_verified_official_source(
            company, {"url": stale}, [{"code": "4321", "pdf_url": exchange}],
            page_fetcher=fetch, crawler=lambda _company: [corporate])
        self.assertEqual(found, exchange)
        self.assertEqual(calls, [stale, corporate, exchange])

    def test_discovery_logs_every_attempt_and_adopted_url(self):
        company = {"code": "4321", "name": "新規テスト", "official_domain": "new.example"}
        stale = "https://new.example/old"
        current = "https://new.example/current"
        def fetch(url, *_args, **_kwargs):
            if url == stale:
                raise discovery.OfficialSourceNotFound("official_source_http_404")
            return current, "株主優待制度 100株"
        output = StringIO()
        with redirect_stdout(output):
            found, _ = discovery.discover_verified_official_source(
                company, {"url": stale}, page_fetcher=fetch,
                crawler=lambda _company: [current])
        self.assertEqual(found, current)
        log = output.getvalue()
        self.assertIn("exploration_url=" + stale, log)
        self.assertIn("exploration_url=" + current, log)
        self.assertIn("adopted_url=" + current, log)

    def test_regex_inputs_are_safely_coerced(self):
        for value in (None, b"code 4321", {"code": "4321"}, ["4321"]):
            with self.subTest(value=value):
                result = discovery.security_code_found(value, "4321")
                self.assertIsInstance(result, bool)
        self.assertTrue(discovery.security_code_found(b"code 4321", "4321"))
        self.assertIn("株主優待", discovery.page_text({"title": "株主優待"}))

    def test_page_text_uses_content_type_bom_meta_and_encoding_fallbacks(self):
        html = '<meta charset="shift_jis"><p>株主優待制度</p>'
        self.assertIn("株主優待制度", discovery.page_text(html.encode("cp932")))
        utf16 = '<meta charset="utf-16-le"><p>株主優待制度</p>'.encode("utf-16-le")
        self.assertIn("株主優待制度", discovery.page_text(b"\xff\xfe" + utf16))
        # A lying UTF-16LE header must fall through to UTF-8.
        self.assertIn("株主優待制度", discovery.page_text(
            "<p>株主優待制度</p>x".encode(), "text/html; charset=utf-16-le"))
        # Even wholly invalid input is retained using replacement characters.
        self.assertTrue(discovery.page_text(b"<p>\x81</p>", "text/html; charset=unknown"))

    def test_implementation_has_no_target_company_exception(self):
        source = Path(discovery.__file__).read_text(encoding="utf-8")
        forbidden = ("7550", "9861", "8163", "7616", "7412",
                     "ゼンショー", "吉野家", "SRSホールディングス", "コロワイド", "アトム")
        self.assertFalse([value for value in forbidden if value in source])
