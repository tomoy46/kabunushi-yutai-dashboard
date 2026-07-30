import csv
import tempfile
import unittest
from pathlib import Path

from scripts.verify_import_batch import verify


class VerifyImportBatchTests(unittest.TestCase):
    def write_csv(self, path, fieldnames, rows):
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_reports_zero_overlap_for_new_dated_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            imported, master = root / "import.csv", root / "master.csv"
            self.write_csv(imported, ["security_code", "source_checked_date"], [
                {"security_code": "1000", "source_checked_date": "2026-07-29"},
                {"security_code": "2000", "source_checked_date": "2026-07-30"},
            ])
            self.write_csv(master, ["code", "official_verified_at"], [
                {"code": "1000", "official_verified_at": "2026-07-29"},
                {"code": "2000", "official_verified_at": "2026-07-30"},
            ])
            result = verify(imported, master, "2026-07-30", 1)
            self.assertEqual(result["existing_code_overlap"], 0)
            self.assertEqual(result["errors"], [])

    def test_detects_code_already_present_in_local_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            imported, master = root / "import.csv", root / "master.csv"
            self.write_csv(imported, ["security_code", "source_checked_date"], [
                {"security_code": "1000", "source_checked_date": "2026-07-30"},
            ])
            self.write_csv(master, ["code", "official_verified_at"], [
                {"code": "1000", "official_verified_at": "2026-07-29"},
            ])
            result = verify(imported, master, "2026-07-30", 1)
            self.assertEqual(result["existing_code_overlap"], 1)
            self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
