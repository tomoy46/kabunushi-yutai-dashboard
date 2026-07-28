#!/usr/bin/env python3
"""Capture and reapply discovery data without overwriting concurrent updates."""

import argparse
import csv
import io
import json
import shutil
import subprocess
from pathlib import Path

DATA_FILES = (
    "benefits.json", "benefits.csv", "verification-queue.json",
    "discovery-progress.json", "research-log.json", "openai-api-usage.json",
    "official-benefit-sources.json",
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


def capture(bundle):
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "base").mkdir(exist_ok=True)
    (bundle / "result").mkdir(exist_ok=True)
    changed_codes = set()
    for name in DATA_FILES:
        current = Path("data") / name
        shutil.copy2(current, bundle / "result" / name)
        try:
            content = subprocess.check_output(["git", "show", f"HEAD:data/{name}"])
        except subprocess.CalledProcessError:
            content = b"[]\n" if name != "discovery-progress.json" and name != "official-benefit-sources.json" else b"{}\n"
        (bundle / "base" / name).write_bytes(content)
        if name == "benefits.csv":
            before = keyed(list(csv.DictReader(io.StringIO(content.decode()))))
            after = keyed(list(csv.DictReader(current.open(encoding="utf-8"))))
        elif name in ("benefits.json", "research-log.json", "verification-queue.json"):
            before, after = keyed(json.loads(content)), keyed(load_json(current))
        else:
            continue
        changed_codes.update(key[0] for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    write_json(bundle / "manifest.json", {"security_codes": sorted(code for code in changed_codes if code), "files": list(DATA_FILES)})


def apply(bundle):
    for name in DATA_FILES:
        base_path, local_path, target = bundle / "base" / name, bundle / "result" / name, Path("data") / name
        if name == "benefits.csv":
            def rows(path):
                with path.open(encoding="utf-8", newline="") as stream:
                    return list(csv.DictReader(stream))
            base, local, remote = rows(base_path), rows(local_path), rows(target)
            merged = merge_changed(base, local, remote)
            with local_path.open(encoding="utf-8", newline="") as stream:
                fieldnames = list(csv.DictReader(stream).fieldnames or [])
            with target.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames); writer.writeheader(); writer.writerows(merged)
            continue
        base, local, remote = load_json(base_path), load_json(local_path), load_json(target)
        if name == "research-log.json":
            merged = merge_changed([], local, remote, ("code", "checked_at"))
        elif name == "openai-api-usage.json":
            merged = merge_changed([], local, remote, ("executed_at",))
        elif name in ("benefits.json", "verification-queue.json"):
            merged = merge_changed(base, local, remote)
        elif name == "official-benefit-sources.json":
            merged = dict(remote)
            for code in set(base) | set(local):
                if base.get(code) != local.get(code):
                    if code in local: merged[code] = local[code]
                    else: merged.pop(code, None)
        else:
            merged = merge_progress(base, local, remote)
        write_json(target, merged)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "apply"))
    parser.add_argument("--bundle", type=Path, default=Path(".discovery-results"))
    args = parser.parse_args()
    (capture if args.command == "capture" else apply)(args.bundle)
