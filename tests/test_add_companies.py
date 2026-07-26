import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import add_companies


class AddCompaniesTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "listed-companies.json").write_text(json.dumps([
            {"code": "1301", "name": "極洋", "market": "プライム", "sector": "水産・農林業"},
            {"code": "1302", "name": "範囲内", "market": "スタンダード", "sector": "食品"},
            {"code": "1999", "name": "範囲終端", "market": "プライム", "sector": "建設業"},
            {"code": "2000", "name": "範囲外", "market": "プライム", "sector": "建設業"},
            {"code": "ABC1", "name": "非4桁", "market": "プライム", "sector": "その他"},
            {"code": "7550", "name": "ゼンショー", "market": "プライム", "sector": "小売業"},
            {"code": "9861", "name": "吉野家", "market": "プライム", "sector": "小売業"},
        ]), encoding="utf-8")
        (root / "benefits.json").write_text('[{"code":"9861","name":"既存"}]', encoding="utf-8")
        (root / "verification-queue.json").write_text("[]", encoding="utf-8")
        return temporary, root

    def test_adds_master_metadata_and_deduplicates(self):
        temporary, root = self.fixture()
        with temporary:
            self.assertEqual(add_companies.add_companies(["7550", "7550", "9861"], root), 0)
            queue = json.loads((root / "verification-queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue, [{"code": "7550", "name": "ゼンショー", "market": "プライム",
                "sector": "小売業", "result": "pending", "verification_reasons": ["not_investigated"]}])

    def test_invalid_and_unknown_codes_are_errors_without_guessing(self):
        temporary, root = self.fixture()
        with temporary, patch("sys.stderr") as stderr:
            self.assertEqual(add_companies.add_companies(["123", "9999"], root), 1)
            self.assertEqual(json.loads((root / "verification-queue.json").read_text()), [])
            self.assertGreaterEqual(stderr.write.call_count, 2)

    def test_adds_only_four_digit_master_codes_in_inclusive_range(self):
        temporary, root = self.fixture()
        output = StringIO()
        with temporary, redirect_stdout(output):
            self.assertEqual(
                add_companies.add_companies([], root, from_code="1301", to_code="1999"), 0
            )
            queue = json.loads((root / "verification-queue.json").read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in queue], ["1301", "1302", "1999"])
            self.assertIn("調査対象への追加: 3社", output.getvalue())
            self.assertIn("追加した証券コード: 1301, 1302, 1999", output.getvalue())

    def test_range_skips_existing_codes_and_preserves_benefits(self):
        temporary, root = self.fixture()
        with temporary:
            benefits_before = (root / "benefits.json").read_bytes()
            (root / "verification-queue.json").write_text(
                '[{"code":"1302","name":"登録済み"}]', encoding="utf-8"
            )
            self.assertEqual(
                add_companies.add_companies([], root, from_code="1301", to_code="1999"), 0
            )
            queue = json.loads((root / "verification-queue.json").read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in queue], ["1302", "1301", "1999"])
            self.assertEqual((root / "benefits.json").read_bytes(), benefits_before)

    def test_reversed_range_is_an_error_without_writing(self):
        temporary, root = self.fixture()
        with temporary, patch("sys.stderr"):
            queue_before = (root / "verification-queue.json").read_bytes()
            self.assertEqual(
                add_companies.add_companies([], root, from_code="2000", to_code="1000"), 1
            )
            self.assertEqual((root / "verification-queue.json").read_bytes(), queue_before)

    def test_parser_accepts_individual_codes_or_a_range(self):
        individual = add_companies.parser().parse_args(["7550", "9861"])
        self.assertEqual(individual.codes, ["7550", "9861"])
        ranged = add_companies.parser().parse_args(["--from", "1301", "--to", "1999"])
        self.assertEqual((ranged.from_code, ranged.to_code), ("1301", "1999"))


if __name__ == "__main__":
    unittest.main()
