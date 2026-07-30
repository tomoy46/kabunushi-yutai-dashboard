#!/usr/bin/env python3
"""Capture and reapply discovery data without overwriting concurrent updates."""

import argparse
import csv
import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DATA_FILES = (
    "benefits.json", "benefits.csv", "verification-queue.json",
    "discovery-progress.json", "research-log.json", "openai-api-usage.json",
    "official-benefit-sources.json", "unresolved.json", "blocked-official-urls.json",
    "benefit-candidates.json",
)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def keyed(rows, keys=("code",)):
    return {tuple(str(row.get(key, "")) for key in keys): row for row in rows}


def merge_changed(base_rows, local_rows, remote_rows, keys=("code",)):
    base, local, remote = keyed(base_rows, keys), keyed(local_rows, keys), keyed(remote_rows, keys)
    for key in base.keys() | local.keys():
        if base.get(key) == local.get(key):
            continue
        if key in local:
            remote[key] = local[key]
        else:
            remote.pop(key, None)
    return list(remote.values())


def merge_progress(base, local, remote):
    result = dict(remote)
    for field in ("processed_codes", "failed_codes"):
        result[field] = list(dict.fromkeys(remote.get(field, []) + local.get(field, [])))
    for field in ("next_index", "total_companies", "uninvestigated_count"):
        if field in local:
            result[field] = max(remote.get(field, 0), local[field]) if field != "uninvestigated_count" else min(remote.get(field, local[field]), local[field])
    if local.get("updated_at", "") > remote.get("updated_at", ""):
        result["updated_at"] = local["updated_at"]
    return result


def capture(bundle, metadata=None):
    bundle.mkdir(parents=True, exist_ok=True)
    changed_codes = set()
    changes = {}
    for name in DATA_FILES:
        current = Path("data") / name
        try:
            content = subprocess.check_output(["git", "show", f"HEAD:data/{name}"])
        except subprocess.CalledProcessError:
            content = b"[]\n" if name != "discovery-progress.json" and name != "official-benefit-sources.json" else b"{}\n"
        if name == "benefits.csv":
            before = keyed(list(csv.DictReader(io.StringIO(content.decode()))))
            after = keyed(list(csv.DictReader(current.open(encoding="utf-8"))))
            file_changes = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
        elif name in ("benefits.json", "verification-queue.json", "unresolved.json",
                     "benefit-candidates.json"):
            keys = (("security_code", "candidate_url", "candidate_title")
                    if name == "benefit-candidates.json" else ("code",))
            before, after = keyed(json.loads(content), keys), keyed(load_json(current), keys)
            file_changes = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
        elif name == "research-log.json":
            before, after = keyed(json.loads(content), ("code", "checked_at")), keyed(load_json(current), ("code", "checked_at"))
            file_changes = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
        elif name == "blocked-official-urls.json":
            before, after = keyed(json.loads(content), ("url", "blocked_at")), keyed(load_json(current), ("url", "blocked_at"))
            file_changes = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
        elif name == "openai-api-usage.json":
            before, after = keyed(json.loads(content), ("executed_at",)), keyed(load_json(current), ("executed_at",))
            file_changes = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
        else:
            before, after = json.loads(content), load_json(current)
            file_changes = {key: {"before": before.get(key), "after": after.get(key)}
                            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}
        if file_changes:
            changes[name] = file_changes
            write_json(bundle / name.replace(".csv", ".json"), file_changes)
        if isinstance(file_changes, list) and name in ("benefits.json", "benefits.csv", "research-log.json", "verification-queue.json", "unresolved.json", "benefit-candidates.json"):
            changed_codes.update(change["key"][0] for change in file_changes if change["key"])
    manifest = {
        "version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
        "committed": False, "security_codes": sorted(code for code in changed_codes if code),
        "files": sorted(changes), "changes": changes,
    }
    if metadata:
        manifest.update(metadata)
    write_json(bundle / "recovery-manifest.json", manifest)
    write_json(bundle / "manifest.json", {key: manifest[key] for key in ("version", "created_at", "committed", "security_codes", "files")})


def apply(bundle):
    manifest = load_json(bundle / "recovery-manifest.json")
    for name, delta in manifest.get("changes", {}).items():
        target = Path("data") / name
        if isinstance(delta, list):
            keys = ("code",)
            if name == "research-log.json": keys = ("code", "checked_at")
            elif name == "blocked-official-urls.json": keys = ("url", "blocked_at")
            elif name == "openai-api-usage.json": keys = ("executed_at",)
            elif name == "benefit-candidates.json":
                keys = ("security_code", "candidate_url", "candidate_title")
            if name == "benefits.csv":
                with target.open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream); rows, fields = list(reader), reader.fieldnames
            else: rows, fields = load_json(target), None
            values = keyed(rows, keys)
            for change in delta:
                key = tuple(change["key"])
                if change["after"] is None: values.pop(key, None)
                else: values[key] = change["after"]
            if name == "benefits.csv":
                with target.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(values.values())
            else: write_json(target, list(values.values()))
        else:
            value = load_json(target)
            if name == "discovery-progress.json":
                base = dict(value)
                local = dict(value)
                for key, change in delta.items():
                    base[key] = change["before"]
                    local[key] = change["after"]
                write_json(target, merge_progress(base, local, value))
                continue
            for key, change in delta.items():
                if change["after"] is None: value.pop(key, None)
                else: value[key] = change["after"]
            write_json(target, value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "apply"))
    parser.add_argument("--bundle", type=Path, default=Path("recovery-results"))
    args = parser.parse_args()
    (capture if args.command == "capture" else apply)(args.bundle)
