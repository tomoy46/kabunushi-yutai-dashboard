import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("merge_results", ROOT / "scripts/merge_discovery_results.py")
merge_results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_results)


class MergeDiscoveryResultsTests(unittest.TestCase):
    def test_remote_and_run_changes_survive_data_aware_reapply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir(); bundle = root / "bundle"
            (bundle / "base").mkdir(parents=True); (bundle / "result").mkdir()
            base = {
                "benefits.json": [{"code": "1000", "name": "old"}],
                "research-log.json": [{"code": "1000", "checked_at": "t0"}],
                "openai-api-usage.json": [{"executed_at": "t0", "calls": 1}],
                "verification-queue.json": [],
                "discovery-progress.json": {"processed_codes": ["1000"], "failed_codes": [], "next_index": 1, "updated_at": "t0", "uninvestigated_count": 9},
                "official-benefit-sources.json": {"1000": {"url": "old"}},
            }
            local = dict(base)
            local.update({
                "benefits.json": base["benefits.json"] + [{"code": "2000", "name": "run"}],
                "research-log.json": base["research-log.json"] + [{"code": "2000", "checked_at": "t2"}],
                "openai-api-usage.json": base["openai-api-usage.json"] + [{"executed_at": "t2", "calls": 2}],
                "discovery-progress.json": {"processed_codes": ["1000", "2000"], "failed_codes": [], "next_index": 2, "updated_at": "t2", "uninvestigated_count": 8},
                "official-benefit-sources.json": {"1000": {"url": "old"}, "2000": {"url": "run"}},
            })
            remote = dict(base)
            remote.update({
                "benefits.json": base["benefits.json"] + [{"code": "3000", "name": "remote"}],
                "research-log.json": base["research-log.json"] + [{"code": "3000", "checked_at": "t1"}],
                "openai-api-usage.json": base["openai-api-usage.json"] + [{"executed_at": "t1", "calls": 3}],
                "discovery-progress.json": {"processed_codes": ["1000", "3000"], "failed_codes": ["3000"], "next_index": 3, "updated_at": "t1", "uninvestigated_count": 7},
                "official-benefit-sources.json": {"1000": {"url": "old"}, "3000": {"url": "remote"}},
            })
            for name in base:
                for folder, value in ((bundle / "base", base[name]), (bundle / "result", local[name]), (root / "data", remote[name])):
                    (folder / name).write_text(json.dumps(value), encoding="utf-8")
            for folder, rows in ((bundle / "base", [{"code": "1000", "name": "old"}]),
                                 (bundle / "result", [{"code": "1000", "name": "old"}, {"code": "2000", "name": "run"}]),
                                 (root / "data", [{"code": "1000", "name": "old"}, {"code": "3000", "name": "remote"}])):
                with (folder / "benefits.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=["code", "name"]); writer.writeheader(); writer.writerows(rows)
            old = Path.cwd()
            try:
                os.chdir(root); merge_results.apply(bundle)
            finally:
                os.chdir(old)
            self.assertEqual({x["code"] for x in json.loads((root / "data/benefits.json").read_text())}, {"1000", "2000", "3000"})
            self.assertEqual({x["code"] for x in json.loads((root / "data/research-log.json").read_text())}, {"1000", "2000", "3000"})
            self.assertEqual(set(json.loads((root / "data/official-benefit-sources.json").read_text())), {"1000", "2000", "3000"})
            progress = json.loads((root / "data/discovery-progress.json").read_text())
            self.assertEqual(set(progress["processed_codes"]), {"1000", "2000", "3000"})
            self.assertEqual(progress["failed_codes"], ["3000"])


if __name__ == "__main__":
    unittest.main()
