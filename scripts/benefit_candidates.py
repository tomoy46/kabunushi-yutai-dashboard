#!/usr/bin/env python3
"""Build and schedule the durable, free shareholder-benefit candidate queue."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlparse

KEYWORDS = (
    "株主優待制度の導入", "株主優待制度の変更", "株主優待制度の廃止", "株主優待制度",
    "株主優待カード", "株主優待ポイント", "株主ご優待", "株主優待", "優待券",
    "株主特典", "株主還元", "記念優待",
)
CHANGE_WORDS = ("変更", "拡充", "一部変更")
NEW_WORDS = ("導入", "新設")
ABOLISHED_WORDS = ("廃止", "休止", "中止")
EXCHANGE_HOSTS = ("jpx.co.jp", "tdnet.info")


def source_kind(url, declared=""):
    host = (urlparse(str(url)).hostname or "").lower()
    declared = str(declared).lower()
    if "tdnet" in host or "tdnet" in declared:
        return "tdnet"
    if "jpx" in host or "jpx" in declared:
        return "jpx"
    if declared in ("official_ir", "official_pdf"):
        return declared
    return "other"


def candidate_from_record(record, companies):
    title = str(record.get("candidate_title") or record.get("title") or "").strip()
    url = str(record.get("candidate_url") or record.get("official_source_url") or
              record.get("pdf_url") or record.get("source_url") or
              record.get("url") or record.get("link") or "").strip()
    searchable = " ".join(str(record.get(field) or "") for field in (
        "candidate_title", "title", "reason", "result", "notes", "matched_keyword",
    )) + " " + url
    keyword = next((word for word in KEYWORDS if word in searchable), None)
    if not keyword:
        return None
    raw = str(record.get("security_code") or record.get("code") or "")
    code_match = re.search(r"(?<!\d)(\d{4})(?!\d)", raw + " " + title)
    code = code_match.group(1) if code_match else ""
    if code not in companies:
        return None
    kind = source_kind(url, record.get("candidate_source") or record.get("source"))
    priority = "high" if kind in ("tdnet", "jpx") else (
        "medium" if kind in ("official_ir", "official_pdf") else "low")
    return {
        "security_code": code,
        "company_name": companies[code].get("name", ""),
        "candidate_source": kind,
        "candidate_url": url,
        "candidate_title": title,
        "candidate_date": str(record.get("candidate_date") or record.get("published_at") or
                              record.get("date") or ""),
        "candidate_keyword": keyword,
        "priority": priority,
        "verification_status": str(record.get("verification_status") or "pending"),
    }


def merge_candidates(existing, records, companies):
    """Merge without reprocessing the same issuer/disclosure pair."""
    merged = [dict(item) for item in existing if isinstance(item, dict)]
    identities = {(str(x.get("security_code")), str(x.get("candidate_url")),
                   str(x.get("candidate_title"))) for x in merged}
    added = 0
    for record in records:
        item = candidate_from_record(record, companies)
        if not item:
            continue
        identity = (item["security_code"], item["candidate_url"], item["candidate_title"])
        if identity in identities:
            continue
        merged.append(item); identities.add(identity); added += 1
    rank = {"high": 0, "medium": 1, "low": 2}
    merged.sort(key=lambda x: (rank.get(x.get("priority"), 9), x.get("candidate_date", ""),
                               x.get("security_code", "")), reverse=False)
    return merged, added


def is_new_disclosure(candidate, unresolved):
    """An unresolved issuer is retried only for a disclosure newer than its check."""
    prior = [x for x in unresolved if str(x.get("code")) == candidate["security_code"]]
    if not prior:
        return True
    candidate_date = candidate.get("candidate_date") or ""
    return any(candidate_date and candidate_date > str(x.get("checked_at") or "")[:10] for x in prior)


def official_discovery_cooldown(candidate, unresolved, now=None):
    """Suppress a failed issuer for 30 days unless a newer official lead exists."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for item in unresolved:
        if str(item.get("code")) != candidate["security_code"]:
            continue
        reasons = item.get("reasons") or [item.get("result")]
        checked = str(item.get("checked_at") or "")
        if "official_site_discovery_failed" not in reasons:
            continue
        try:
            checked_at = dt.datetime.fromisoformat(checked.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - checked_at < dt.timedelta(days=30):
            candidate_date = str(candidate.get("candidate_date") or "")
            return bool(candidate_date and candidate_date > checked[:10])
    return True


def select_candidates(candidates, unresolved, limit=25, now=None):
    pending = [x for x in candidates if x.get("verification_status", "pending") == "pending"
               and is_new_disclosure(x, unresolved)
               and official_discovery_cooldown(x, unresolved, now)]
    high = [x for x in pending if x.get("priority") == "high"][:min(20, limit)]
    medium = [x for x in pending if x.get("priority") == "medium"][:min(5, limit-len(high))]
    return high + medium


def disclosure_action(text):
    if any(word in text for word in ABOLISHED_WORDS): return "abolished"
    if any(word in text for word in NEW_WORDS): return "new"
    if any(word in text for word in CHANGE_WORDS): return "updated_candidate"
    return "verify"


def weekly_fallback_allowed(now=None):
    """The exhaustive queue runs only on Monday (UTC/JST are both Monday most of the day)."""
    return (now or dt.datetime.now(dt.timezone.utc)).weekday() == 0
