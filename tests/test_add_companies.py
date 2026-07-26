import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
