import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from import_benefits import IMPORT_COLUMNS, ImportValidationError, import_benefits
from verify_pages_deployment import compare_data


class ImportBenefitsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.source = root / "import.csv"
        self.master_csv = root / "benefits.csv"
        self.master_json = root / "benefits.json"
        self.existing = {
            "code": "1111", "name": "確認済株式会社", "market": "プライム",
            "industry": "小売業", "category": "商品", "record_months": "3",
            "long_term_condition": "なし", "benefit_status": "official_confirmed",
            "official_verified_at": "2026-01-01",
            "official_source_url": "https://example.com/old", "abolished_at": "",
            "last_record_date": "", "data_confidence": "official_confirmed",
            "annual_occurrences": "1", "change_or_abolition_note": "",
            "benefit_tiers_json": '[{"shares":100,"description":"既存","annual_value_yen":null}]',
        }
        with self.master_csv.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=self.existing)
            writer.writeheader()
            writer.writerow(self.existing)
        self.master_json.write_text("[]\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def write_import(self, rows, columns=IMPORT_COLUMNS):
        with self.source.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def row(code="2222", status="confirmed", url="https://example.com/benefit"):
        return {
            "security_code": code, "company_name": "新規株式会社",
            "benefit_summary": "100株で商品", "required_shares": "100",
            "record_month": "3|9", "long_term_condition": "なし",
            "official_url": url, "status": status,
            "source_checked_date": "2026-07-30",
        }

    def test_adds_valid_row_to_both_outputs(self):
        self.write_import([self.row()])
        result = import_benefits(self.source, self.master_csv, self.master_json)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["after_confirmed"], 2)
        items = json.loads(self.master_json.read_text(encoding="utf-8"))
        added = next(item for item in items if item["code"] == "2222")
        self.assertEqual(added["benefit_status"], "official_confirmed")
        self.assertEqual(added["record_months"], [3, 9])
        self.assertEqual(added["minimum_shares"], 100)

    def test_existing_confirmed_is_not_overwritten_and_is_update_candidate(self):
        row = self.row("1111")
        row["benefit_summary"] = "変更内容"
        self.write_import([row])
        before = self.master_csv.read_text(encoding="utf-8")
        result = import_benefits(self.source, self.master_csv, self.master_json)
        self.assertEqual(result["update_candidates"], 1)
        self.assertIn("既存", self.master_csv.read_text(encoding="utf-8"))
        self.assertNotIn("変更内容", self.master_csv.read_text(encoding="utf-8"))
        self.assertEqual(before, self.master_csv.read_text(encoding="utf-8"))

    def test_second_import_is_idempotent(self):
        self.write_import([self.row()])
        import_benefits(self.source, self.master_csv, self.master_json)
        before = self.master_csv.read_text(encoding="utf-8")
        result = import_benefits(self.source, self.master_csv, self.master_json)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.master_csv.read_text(encoding="utf-8"), before)

    def test_abolished_is_preserved(self):
        self.write_import([self.row(status="abolished")])
        import_benefits(self.source, self.master_csv, self.master_json)
        item = next(item for item in json.loads(self.master_json.read_text())
                    if item["code"] == "2222")
        self.assertEqual(item["benefit_status"], "abolished")

    def test_public_comparison_detects_count_and_code_mismatch(self):
        expected = [{"code": "1111", "benefit_status": "official_confirmed"}]
        actual = [{"code": "2222", "benefit_status": "abolished"}]
        result = compare_data(expected, actual)
        self.assertFalse(result["matches"])
        self.assertEqual(result["missing_codes"], ["1111"])
        self.assertEqual(result["extra_codes"], ["2222"])

    def test_rejects_invalid_rows_before_changing_outputs(self):
        invalid = self.row(code="12")
        invalid["status"] = "active"
        self.write_import([invalid, invalid])
        before = self.master_csv.read_text(encoding="utf-8")
        with self.assertRaises(ImportValidationError) as raised:
            import_benefits(self.source, self.master_csv, self.master_json)
        self.assertGreaterEqual(len(raised.exception.errors), 3)
        self.assertEqual(self.master_csv.read_text(encoding="utf-8"), before)
        self.assertEqual(self.master_json.read_text(encoding="utf-8"), "[]\n")

    def test_requires_exact_columns(self):
        self.write_import([], IMPORT_COLUMNS[:-1])
        with self.assertRaisesRegex(ImportValidationError, "CSV列"):
            import_benefits(self.source, self.master_csv, self.master_json)

    def test_manual_workflow_has_no_api_or_schedule(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/import-benefits.yml").read_text()
        self.assertIn("Import shareholder benefits CSV", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("OPENAI", workflow.upper())

        workflow_directory = Path(__file__).parents[1] / ".github/workflows"
        for name in ("discover-benefits.yml", "discover-benefits-with-openai.yml"):
            discovery = (workflow_directory / name).read_text()
            self.assertIn("workflow_dispatch:", discovery)
            self.assertNotIn("schedule:", discovery)

    def test_integrated_workflow_orders_import_deploy_and_verification(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/import-and-deploy-benefits.yml").read_text()
        expected = ["Validate import CSV", "Run regression tests", "Import shareholder benefits",
                    "Collect changes", "Commit and safely push main",
                    "Create GitHub Pages artifact", "Deploy GitHub Pages",
                    "Verify published benefits"]
        positions = [workflow.index(name) for name in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("path: .", workflow)
        self.assertNotIn("OPENAI", workflow.upper())
        self.assertIn("for attempt in 1 2 3", (root / "scripts/push_import_with_retry.sh").read_text())


if __name__ == "__main__":
    unittest.main()
