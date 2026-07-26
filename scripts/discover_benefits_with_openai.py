#!/usr/bin/env python3
"""Discover shareholder benefits with Responses API search and strict JSON output.

Only URLs returned by the search tool are eligible evidence.  This program deliberately
uses the standard library and never persists an API key or a complete API response.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import socket
import sys
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENDPOINT = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
OFFICIAL_HOSTS = ("jpx.co.jp", "tdnet.info")
JPX_BLOCKED_PATHS = ("/corporate/investor-relations/", "/corporate/about-jpx/")
BLOCKED_HOSTS = (
    "yahoo.co.jp", "minkabu.jp", "kabutan.jp", "rakuten-sec.co.jp",
    "sbisec.co.jp", "monex.co.jp", "note.com", "x.com", "facebook.com",
    "instagram.com", "youtube.com", "yutai-guide.daiwair.co.jp",
)
FIELDS = {
    "code": {"type": "string"}, "name": {"type": "string"},
    "benefit_status": {"type": "string", "enum": ["official_confirmed", "candidate", "abolished"]},
    "record_months": {"type": "array", "items": {"type": "integer"}},
    "record_date": {"type": ["string", "null"]}, "annual_occurrences": {"type": ["integer", "null"]},
    "minimum_shares": {"type": ["integer", "null"]}, "maximum_shares": {"type": ["integer", "null"]},
    "benefit_tiers": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "shares": {"type": "integer"},
            "maximum_shares": {"type": ["integer", "null"]},
            "description": {"type": "string"},
            "annual_value_yen": {"type": ["integer", "null"]},
        },
        "required": ["shares", "maximum_shares", "description", "annual_value_yen"],
        "additionalProperties": False,
    }},
    "benefit_title": {"type": ["string", "null"]}, "benefit_description": {"type": ["string", "null"]},
    "category": {"type": ["string", "null"]}, "annual_value_yen": {"type": ["integer", "null"]},
    "valuation_type": {"type": "string", "enum": ["official_amount", "not_calculated"]},
    "long_term_required": {"type": ["boolean", "null"]}, "holding_period_months": {"type": ["integer", "null"]},
    "conditions": {"type": ["string", "null"]}, "official_source_url": {"type": ["string", "null"]},
    "official_source_title": {"type": ["string", "null"]}, "official_verified_at": {"type": "string"},
    "abolished_at": {"type": ["string", "null"]}, "last_record_date": {"type": ["string", "null"]},
    "change_or_abolition_note": {"type": ["string", "null"]},
    "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
    "evidence_text": {"type": ["string", "null"]}, "error_reason": {"type": ["string", "null"]},
}
SCHEMA = {"type": "object", "properties": FIELDS, "required": list(FIELDS), "additionalProperties": False}
BENEFIT_WORDS = ("株主優待", "優待制度", "株主優待制度")
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36")
REEXTRACT_REASONS = {"record_date_unknown", "minimum_shares_unknown", "low_confidence", "current_program_not_confirmed"}


def safe_message(value, key=None):
    """Return a log-safe, bounded exception message."""
    message = str(value or "")
    if key:
        message = message.replace(key, "[REDACTED]")
    # API keys have a recognizable prefix. Redact one even when it is not the
    # exact key supplied to this process (for example, a server echo).
    message = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
    return message[:500]


class APIError(Exception):
    def __init__(self, status, message, error_type=None, code=None, param=None, request_id=None):
        self.status = status
        self.error_type = error_type
        self.code = code
        self.param = param
        self.request_id = request_id
        self.message = safe_message(message)
        super().__init__(f"Responses API HTTP {status}: {self.message}")


def safe_error_lines(error, key=None):
    """Format only the allow-listed API error fields for diagnostic output."""
    if isinstance(error, APIError):
        fields = (
            ("HTTP status", error.status), ("Error type", error.error_type),
            ("Error code", error.code), ("Error param", error.param),
            ("Error message", safe_message(error.message, key)),
            ("Request ID", error.request_id),
        )
        return [f"{label}: {value}" for label, value in fields if value not in (None, "")]
    return [f"Exception type: {type(error).__name__}",
            f"Error message: {safe_message(error, key)}"]


def diagnostic_stage(label, state, error=None, key=None, detail=None):
    print(f"{label}: {state}" + (f" ({detail})" if detail else ""))
    if error:
        for line in safe_error_lines(error, key):
            print(line)


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def canonical_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        path = parsed.path or "/"
        # Directory pages compare consistently while file URLs retain their form.
        if not path.endswith("/") and not re.search(r"/[^/]+\.[A-Za-z0-9]{1,8}$", path):
            path += "/"
        query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                           if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS])
        return urlunparse(("https", host + port, path, "", query, ""))
    except (TypeError, ValueError):
        return None


def hostname(value):
    normalized = canonical_url(value)
    return urlparse(normalized).hostname if normalized else None


def normalized_host(value):
    """Return a host identity where the conventional ``www`` alias is ignored."""
    host = hostname(value) if "://" in str(value or "") else str(value or "").lower()
    return host.removeprefix("www.") if host else None


def url_identity(value):
    """Build a comparison key without changing the host spelling we persist."""
    normalized = canonical_url(value)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    return (normalized_host(normalized), parsed.path.rstrip("/") or "/", parsed.query)


def is_subdomain(host, parent):
    host, parent = normalized_host(host), normalized_host(parent)
    return bool(host and parent and (host == parent or host.endswith("." + parent)))


def normalize_company_name(value):
    """Normalize harmless legal/width variants without fuzzy matching companies."""
    value = str(value or "").strip()
    # NFKC maps full-width Latin letters/digits and （株） to their ASCII forms.
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("株式会社", "").replace("(株)", "")
    return re.sub(r"[\s\u3000]+", "", value).strip()


def same_company_name(left, right):
    normalized = normalize_company_name(left)
    return bool(normalized and normalized == normalize_company_name(right))


def allowed_url(url, official_domain):
    host = hostname(url)
    if not host or any(is_subdomain(host, bad) for bad in BLOCKED_HOSTS):
        return False
    # Once a corporate domain is known, ordinary discovery must not escape to
    # JPX/TDnet (or any other company). Exchange disclosures are a fallback only
    # for companies whose official domain has not yet been identified.
    domains = ((official_domain.lower().removeprefix("www."),) if official_domain else OFFICIAL_HOSTS)
    return any(is_subdomain(host, domain) for domain in domains)


def company_prompt(company):
    queries = [
        f'{company["name"]} {company["code"]} 株主優待 公式', f'{company["name"]} 株主優待 IR',
        f'{company["name"]} 株主優待制度 PDF', f'{company["name"]} 株主優待 廃止', f'{company["name"]} 株主優待 変更',
    ]
    return f"""日本企業「{company['name']}」（証券コード {company['code']}）の現在の株主優待を調査する。
検索語: {' / '.join(queries)}
企業公式IR、JPX、TDnetだけを根拠にする。証券会社、まとめサイト、ブログ、SNSは根拠にしない。
検索結果にないURLや値を作らない。必要株数、権利月・基準日、有効性が不明ならcandidateとする。
株数ごとの全区分をbenefit_tiersへ必ず配列で記録し、1区分だけの場合も配列にする。
minimum_sharesはbenefit_tiersの最小shares、annual_value_yenはその区分の年間価値と一致させる。
割引率や利用額で価値が変動する優待はannual_value_yen=null、valuation_type=not_calculatedとする。
evidence_textは根拠を200字以内で要約する。"""


def normalize_for_storage(item, company):
    """Add the dashboard's compatibility fields before persisting API output."""
    item = dict(item)
    tiers = item.get("benefit_tiers") if isinstance(item.get("benefit_tiers"), list) else []
    tiers = [tier for tier in tiers if isinstance(tier, dict) and isinstance(tier.get("shares"), int)]
    if not tiers and isinstance(item.get("minimum_shares"), int):
        description = item.get("benefit_description") or item.get("benefit_title") or "優待内容未取得"
        tiers = [{"shares": item["minimum_shares"], "maximum_shares": item.get("maximum_shares"),
                  "description": description, "annual_value_yen": item.get("annual_value_yen")}]
    tiers.sort(key=lambda tier: tier["shares"])
    item["benefit_tiers"] = tiers
    if tiers:
        item["minimum_shares"] = tiers[0]["shares"]
        item["annual_value_yen"] = tiers[0].get("annual_value_yen")
    item["data_confidence"] = ("official_confirmed" if item.get("benefit_status") == "official_confirmed"
                               else item.get("data_confidence") or item.get("benefit_status"))
    title, description = item.get("benefit_title"), item.get("benefit_description")
    summary = title or description or "優待内容未取得"
    if title and description and description not in title:
        summary = f"{title} {description}"
    value = item.get("annual_value_yen")
    if isinstance(value, int) and f"{value:,}" not in summary:
        summary = f"{summary} {value:,}円相当"
    item["benefit_summary"] = summary
    item["last_checked_at"] = item.get("official_verified_at")
    if item.get("long_term_required") is False:
        item["long_term_condition"] = "なし"
    elif item.get("long_term_required") is True:
        months = item.get("holding_period_months")
        item["long_term_condition"] = (f"継続保有{months}か月以上" if months else
                                         item.get("conditions") or "長期保有条件あり")
    else:
        item["long_term_condition"] = None
    item["notes"] = item.get("conditions") or item.get("change_or_abolition_note")
    item["market"] = company.get("market") or None
    item["sector"] = company.get("sector") or None
    item["industry"] = company.get("industry") or company.get("sector") or None
    return item


def response_format():
    return {"type": "json_schema", "name": "shareholder_benefit", "strict": True, "schema": SCHEMA}


def build_payload(company, model=DEFAULT_MODEL):
    tool = {"type": "web_search", "search_context_size": "low"}
    domain = company.get("official_domain")
    if domain:
        tool["filters"] = {"allowed_domains": [domain]}
    return {"model": model, "input": company_prompt(company), "tools": [tool], "max_tool_calls": 1,
            "include": ["web_search_call.action.sources"], "store": False,
            "reasoning": {"effort": "none"}, "text": {"format": response_format()}}


_last_call = 0.0
def request_response(payload, key, max_retries=3):
    global _last_call
    body = json.dumps(payload).encode()
    request = Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    for attempt in range(max_retries):
        delay = 1 - (time.monotonic() - _last_call)
        if delay > 0:
            time.sleep(delay)
        _last_call = time.monotonic()
        try:
            with urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except HTTPError as error:
            detail = {}
            try:
                parsed = json.loads(error.read().decode("utf-8", "replace"))
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                    detail = parsed["error"]
            except (ValueError, AttributeError, TypeError):
                pass
            headers = getattr(error, "headers", None)
            request_id = None
            if headers:
                request_id = headers.get("x-request-id") or headers.get("request-id")
            api_error = APIError(
                error.code, safe_message(detail.get("message", error.reason), key),
                error_type=detail.get("type"), code=detail.get("code"),
                param=detail.get("param"), request_id=request_id,
            )
            if error.code in (401, 403, 404, 429) or error.code not in (500, 502, 503, 504) or attempt == max_retries - 1:
                raise api_error from None
        except (URLError, socket.timeout) as error:
            if attempt == max_retries - 1:
                message = error.reason if isinstance(error, URLError) else error
                raise APIError(0, safe_message(message, key)) from None
        time.sleep(2 ** attempt + random.random())


def output_text(response):
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    return "".join(part.get("text", "") for item in response.get("output", [])
                   for part in item.get("content", []) if part.get("type") == "output_text")


def search_sources(response):
    found = {}
    for item in response.get("output", []):
        if item.get("type") == "web_search_call":
            for source in item.get("action", {}).get("sources", []) or []:
                url = source.get("url")
                if canonical_url(url): found[canonical_url(url)] = source
        for part in item.get("content", []) or []:
            for annotation in part.get("annotations", []) or []:
                url = annotation.get("url")
                if canonical_url(url): found[canonical_url(url)] = annotation
    return found


def web_search_stats(response):
    """Summarize search output without exposing queries, sources, or URLs.

    Responses can contain duplicate representations of one tool call.  Prefer the
    server-provided call ID for identity; older/id-less representations use only
    the action fields that describe the call as a stable fallback identity.
    """
    items = [item for item in response.get("output", []) if item.get("type") == "web_search_call"]
    unique, call_ids, action_types = set(), set(), set()
    for item in items:
        call_id = item.get("id")
        if call_id:
            call_ids.add(str(call_id))
            identity = ("id", str(call_id))
        else:
            action = item.get("action") or {}
            identity = ("action", json.dumps({
                key: action.get(key) for key in ("type", "status", "queries", "sources")
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
        unique.add(identity)
        action_type = (item.get("action") or {}).get("type")
        # Never echo arbitrary response content (including a URL) into diagnostics.
        if isinstance(action_type, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", action_type):
            action_types.add(action_type)
    return {
        "output_items": len(items), "unique_calls": len(unique), "call_ids": call_ids,
        "action_types": sorted(action_types),
    }


def web_search_calls(response):
    """Return the de-duplicated number of web-search tool calls."""
    return web_search_stats(response)["unique_calls"]


def usage(response):
    value = response.get("usage", {})
    details = value.get("input_tokens_details", {}) or {}
    return {"input_tokens": value.get("input_tokens", 0), "cached_input_tokens": details.get("cached_tokens", 0),
            "output_tokens": value.get("output_tokens", 0)}


def structured_without_search(company, evidence, model):
    return {"model": model, "input": "以下のWeb検索結果だけをスキーマへ変換する。値やURLを作らない。\n" +
            json.dumps({"company": company, "search_result": evidence}, ensure_ascii=False),
            "store": False, "reasoning": {"effort": "none"}, "text": {"format": response_format()}}


def official_page_payload(company, url, text, initial, model):
    """Build the single, tool-free official-page correction request."""
    prompt = {
        "company": {"name": company["name"], "code": str(company["code"])},
        "verified_official_url": url,
        "official_page_shareholder_benefit_excerpt": text[:20_000],
        "initial_structured_result": initial,
    }
    instructions = ("検証済み企業公式ページの本文だけを使い、株主優待情報を再抽出する。"
                    "本文にない値は推測しない。株数ごとの全区分と贈呈時期はconditionsに残す。")
    return {"model": model, "input": instructions + "\n" + json.dumps(prompt, ensure_ascii=False),
            "store": False, "reasoning": {"effort": "none"}, "text": {"format": response_format()}}


def page_text(body):
    """Decode a bounded page and turn HTML into searchable plain text."""
    for codec in ("utf-8", "shift_jis", "utf-16"):
        try:
            decoded = body.decode(codec)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        decoded = body.decode("utf-8", "ignore")
    # Meta values are not text nodes, but are important corporate identity
    # evidence (description, og:title and og:site_name).
    meta = " ".join(match.group(1) for tag in re.findall(r"(?is)<meta\b[^>]*>", decoded)
                    if (match := re.search(r'''(?is)\bcontent\s*=\s*["']([^"']*)["']''', tag)))
    decoded = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", decoded)
    decoded = meta + " " + decoded
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", decoded)).strip()


def fetch_official_page(url, company, source_urls):
    normalized = canonical_url(url)
    if not any(url_identity(normalized) == url_identity(source) for source in source_urls):
        raise ValueError("source_url_not_in_search_results")
    if not allowed_url(normalized, company.get("official_domain")):
        raise ValueError("source_url_not_allowed")
    request = Request(normalized, headers={"User-Agent": BROWSER_USER_AGENT,
                                           "Accept": "text/html,application/xhtml+xml"})
    with urlopen(request, timeout=25) as response:
        if response.status != 200: raise ValueError(f"HTTP_status_{response.status}")
        final = canonical_url(response.geturl())
        expected = company.get("official_domain")
        if not final or (expected and not is_subdomain(hostname(final), expected)):
            raise ValueError(f"redirect_host_not_official_domain:{hostname(final) or 'invalid'}")
        if not expected and hostname(final) != hostname(normalized):
            raise ValueError("source_redirected_outside_candidate_domain")
        body = response.read(2_000_000)
    text = page_text(body)
    host, path = hostname(final), urlparse(final).path.lower()
    if is_subdomain(host, "jpx.co.jp") and any(path.startswith(value) for value in JPX_BLOCKED_PATHS):
        raise ValueError("jpx_corporate_page_not_company_disclosure")
    normalized_text = normalize_company_name(text)
    name_found = normalize_company_name(company["name"]) in normalized_text
    code_found = str(company["code"]) in text
    if any(is_subdomain(host, exchange) for exchange in OFFICIAL_HOSTS):
        if not (name_found and code_found):
            raise ValueError("exchange_disclosure_identity_mismatch")
    elif not name_found:
        raise ValueError("company_identity_not_found")
    if not any(word in text for word in BENEFIT_WORDS):
        raise ValueError("shareholder_benefit_text_not_found")
    return final, text


def fetch_and_validate(url, company, source_urls):
    return fetch_official_page(url, company, source_urls)[0]


def candidate_urls(sources, company, selected=None):
    """Rank only URLs actually returned by search; corporate benefit/IR pages win."""
    domain = company.get("official_domain")
    urls = list(sources)
    def rank(url):
        host, path = hostname(url), urlparse(url).path.lower()
        corporate = bool(domain and is_subdomain(host, domain))
        relevant = bool(re.search(r"ir|benefit|yutai|株主|優待|shareholder", path, re.I))
        authority = 2 if is_subdomain(host, "jpx.co.jp") else 3 if is_subdomain(host, "tdnet.info") else 4
        return (0 if corporate and relevant else 1 if corporate else authority,
                0 if canonical_url(selected) == url else 1)
    return [url for url in sorted(urls, key=rank) if allowed_url(url, domain)][:5]


def select_verified_source(item, company, sources, page_fetcher=None):
    page_fetcher = page_fetcher or fetch_and_validate
    for url in candidate_urls(sources, company, item.get("official_source_url")):
        try:
            result = page_fetcher(url, company, set(sources))
            return result if isinstance(result, tuple) else (result, "")
        except Exception as error:
            # The exception values produced by fetch_official_page are bounded
            # reason codes and never contain the URL or response body.
            print(f"Official source rejected: {safe_message(error)}", file=sys.stderr)
            continue
    return None, ""


def validate(item, company, sources, fetcher=None):
    fetcher = fetcher or fetch_and_validate
    reasons = []
    if str(item.get("code")) != str(company["code"]) or not same_company_name(item.get("name"), company["name"]):
        reasons.append("company_identity_mismatch")
    original = canonical_url(item.get("official_source_url"))
    url, _ = select_verified_source(item, company, sources, fetcher)
    if url:
        item["official_source_url"] = url
    elif not original or not any(url_identity(original) == url_identity(source) for source in sources):
        reasons.append("source_not_in_search_results")
    elif not allowed_url(original, company.get("official_domain")):
        reasons.append("source_domain_not_allowed")
    else:
        reasons.append("official_source_validation_failed")
    source_failed = any(reason in reasons for reason in (
        "source_not_in_search_results", "source_domain_not_allowed", "official_source_validation_failed"))
    if source_failed:
        # Never expose a model-supplied, unverified URL as an official source.
        item["official_source_url"] = None
        item["error_reason"] = "official_source_validation_failed"
    if item.get("minimum_shares") is None: reasons.append("minimum_shares_unknown")
    if not item.get("record_months") and not item.get("record_date"): reasons.append("record_date_unknown")
    if item.get("confidence_score", 0) < 90: reasons.append("low_confidence")
    if item.get("benefit_status") == "abolished" and source_failed:
        reasons.append("abolition_not_officially_confirmed")
    if item.get("benefit_status") not in ("official_confirmed", "abolished"):
        reasons.append("current_program_not_confirmed")
    description = " ".join(str(item.get(k) or "") for k in ("benefit_title", "benefit_description", "conditions"))
    if re.search(r"割引|％|%|利用額", description):
        item["annual_value_yen"], item["valuation_type"] = None, "not_calculated"
    if reasons:
        item["benefit_status"] = "candidate"
        item["error_reason"] = ("official_source_validation_failed" if source_failed else
                                ",".join(dict.fromkeys(reasons)))
    return item, list(dict.fromkeys(reasons))


def learn_domain_candidate(company, sources, page_fetcher=None):
    """Conservatively identify an unmapped corporate host from returned sources."""
    if company.get("official_domain"): return None
    page_fetcher = page_fetcher or fetch_official_page
    bare_name = company["name"].replace("株式会社", "").replace("（株）", "")
    for url, metadata in list(sources.items())[:5]:
        host = hostname(url)
        if not host or any(is_subdomain(host, value) for value in (*BLOCKED_HOSTS, *OFFICIAL_HOSTS)):
            continue
        title = str(metadata.get("title") or "")
        if bare_name not in title and str(company["code"]) not in title: continue
        try:
            # Candidate fetches are constrained to their search-returned host.
            final, text = page_fetcher(url, dict(company, official_domain=host), set(sources))
        except Exception:
            continue
        if hostname(final) == host and re.search(r"会社概要|企業情報|\bIR\b|投資家", text, re.I):
            return host
    return None


def choose(companies, args, progress, benefits, queue=None):
    immutable = {x["code"] for x in benefits if x.get("benefit_status") in ("official_confirmed", "abolished")}
    failed = set(progress.get("failed_codes", []))
    candidates = [x for x in companies if x["code"] not in immutable]
    if args.start_code: candidates = [x for x in candidates if x["code"] >= args.start_code]
    if args.end_code: candidates = [x for x in candidates if x["code"] <= args.end_code]
    pending = {x.get("code") for x in (queue or []) if x.get("result") == "pending"}
    queued = {x.get("code") for x in (queue or [])}
    if queued:
        candidates.sort(key=lambda x: (x["code"] not in pending, x["code"] not in queued))
    elif args.retry_failed: candidates.sort(key=lambda x: x["code"] not in failed)
    else:
        start = progress.get("next_index", 0) % max(1, len(candidates)); candidates = candidates[start:] + candidates[:start]
    return candidates[:min(args.batch_size, args.daily_limit)]


def append_benefit_csv(path, item):
    """Append one officially verified result without rewriting existing CSV rows."""
    if not path.exists():
        # Test/standalone data directories may start without the derived CSV.
        fieldnames = ["code", "name", "market", "industry", "category", "record_months",
                      "long_term_condition", "benefit_status", "official_verified_at",
                      "official_source_url", "abolished_at", "last_record_date", "data_confidence",
                      "annual_occurrences", "change_or_abolition_note", "benefit_tiers_json"]
        with path.open("w", encoding="utf-8", newline="") as output:
            csv.DictWriter(output, fieldnames=fieldnames).writeheader()
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames
        existing = {row["code"] for row in reader}
    if item["code"] in existing:
        return
    row = {name: "" for name in fieldnames}
    row.update({name: item.get(name) for name in fieldnames if name in item})
    row["record_months"] = "|".join(map(str, item.get("record_months") or []))
    row["benefit_tiers_json"] = json.dumps(item.get("benefit_tiers") or [], ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="") as output:
        csv.DictWriter(output, fieldnames=fieldnames).writerow(row)


def run(args):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        if args.diagnostic_mode:
            diagnostic_stage("API key check", "start")
            diagnostic_stage("API key check", "failure", ValueError("OPENAI_API_KEY is not set"))
            print("Diagnostic result: failure\nFailed stage: plain\nResponses API calls: 0\nWeb-search Responses requests: 0\nWeb-search output items: 0\nUnique web-search call IDs: 0\nWeb-search action types: none\nInput tokens: 0\nOutput tokens: 0")
            return 1
        print("OPENAI_API_KEY is required", file=sys.stderr); return 2
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    companies = load(DATA / "listed-companies.json", [])
    domains = load(DATA / "company-domains.json", {})
    for company in companies:
        if not company.get("official_domain"): company["official_domain"] = domains.get(company["code"])
    if args.diagnostic_mode:
        companies = [x for x in companies if x.get("code") == "1301"]
        if not companies: companies = [{"code": "1301", "name": "極洋", "official_domain": domains.get("1301")}]
    benefits = load(DATA / "benefits.json", []); queue = load(DATA / "verification-queue.json", [])
    progress = load(DATA / "discovery-progress.json", {"next_index": 0, "processed_codes": [], "failed_codes": []})
    selected = companies if args.diagnostic_mode else choose(companies, args, progress, benefits, queue)
    totals = {"processed_companies": 0, "responses_api_calls": 0, "responses_with_web_search": 0,
              "web_search_output_items": 0, "web_search_calls": 0,
              "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "successes": 0, "verification_required": 0, "failures": 0}
    unique_web_search_call_ids = set()
    web_search_action_types = set()
    errors=[]; started=time.monotonic(); failed_stage = None
    if args.diagnostic_mode:
        print(f"OpenAI model: {model}")
        diagnostic_stage("API key check", "start")
        diagnostic_stage("API key check", "success")
        diagnostic_stage("Plain Responses API", "start")
        try:
            plain = {"model": model, "input": "OKとだけ回答してください。", "store": False,
                     "reasoning": {"effort": "none"}}
            totals["responses_api_calls"] += 1; plain_response = request_response(plain, key)
            for key_name, value in usage(plain_response).items(): totals[key_name] += value
            diagnostic_stage("Plain Responses API", "success")
        except Exception as error:
            failed_stage = "plain"
            diagnostic_stage("Plain Responses API", "failure", error, key)
    for company in selected:
        before_progress = json.loads(json.dumps(progress))
        active_label = "Web search + Structured Outputs"
        active_failed_stage = "web_search"
        failure_logged = False
        try:
            if args.diagnostic_mode: diagnostic_stage("Web search + Structured Outputs", "start")
            totals["responses_api_calls"] += 1
            totals["responses_with_web_search"] += 1
            response = request_response(build_payload(company, model), key)
            search_stats = web_search_stats(response)
            totals["web_search_output_items"] += search_stats["output_items"]
            totals["web_search_calls"] += search_stats["unique_calls"]
            unique_web_search_call_ids.update(search_stats["call_ids"])
            web_search_action_types.update(search_stats["action_types"])
            if args.diagnostic_mode and search_stats["output_items"] > 1:
                print("Warning: multiple web_search_call output items were returned in one Responses API response.")
                print("The response will continue because max_tool_calls=1 was requested and the API returned HTTP 200.")
            raw = output_text(response)
            if args.diagnostic_mode: diagnostic_stage("Web search + Structured Outputs", "success")
            try:
                item = json.loads(raw)
                if args.diagnostic_mode:
                    diagnostic_stage("Fallback structured output", "start")
                    diagnostic_stage("Fallback structured output", "success", detail="not required")
            except (ValueError, TypeError):
                # Compatibility fallback: it contains no search tool, so search still ran only once.
                if args.diagnostic_mode: diagnostic_stage("Fallback structured output", "start")
                totals["responses_api_calls"] += 1
                try:
                    response2 = request_response(structured_without_search(company, raw, model), key)
                    item = json.loads(output_text(response2))
                except Exception as error:
                    if args.diagnostic_mode:
                        failed_stage = failed_stage or "structured_output"
                        diagnostic_stage("Fallback structured output", "failure", error, key)
                        failure_logged = True
                    raise
                for key_name, value in usage(response2).items(): totals[key_name] += value
                if args.diagnostic_mode: diagnostic_stage("Fallback structured output", "success")
            for key_name, value in usage(response).items(): totals[key_name] += value
            active_label = "Search source extraction"
            if args.diagnostic_mode: diagnostic_stage(active_label, "start")
            sources = search_sources(response)
            if args.diagnostic_mode: diagnostic_stage(active_label, "success", detail=f"{len(sources)} sources")
            learned = learn_domain_candidate(company, sources) if not company.get("official_domain") else None
            if learned:
                company["official_domain"] = learned
                if args.diagnostic_mode:
                    print(f"Learned official domain candidate: {learned}")
                else:
                    domains[company["code"]] = learned
                    atomic(DATA / "company-domains.json", domains)
            active_label = "Official URL validation"
            active_failed_stage = "official_validation"
            if args.diagnostic_mode: diagnostic_stage(active_label, "start")
            item, reasons = validate(item, company, sources)
            retry_reasons = set(reasons) & REEXTRACT_REASONS
            verified_url = canonical_url(item.get("official_source_url"))
            validation_failed = any(reason in reasons for reason in (
                "source_not_in_search_results", "source_domain_not_allowed", "official_source_validation_failed"))
            if retry_reasons and verified_url and not validation_failed:
                # Fetch only the already validated, search-returned page. The follow-up
                # request deliberately has no tools and receives a bounded excerpt.
                try:
                    final_url, official_text = fetch_official_page(verified_url, company, set(sources))
                except Exception:
                    # A page that changed between checks remains a verification item;
                    # source verification trouble is not an API/program failure.
                    official_text = ""
                if official_text:
                    totals["responses_api_calls"] += 1
                    correction = request_response(official_page_payload(
                        company, final_url, official_text, item, model), key)
                    for key_name, value in usage(correction).items(): totals[key_name] += value
                    corrected = json.loads(output_text(correction))
                    corrected["official_source_url"] = final_url
                    item, reasons = validate(corrected, company, sources,
                                             fetcher=lambda url, _company, _sources: url)
            if args.diagnostic_mode:
                if reasons:
                    diagnostic_stage("Official URL validation", "verification required",
                                     detail=",".join(reasons))
                else: diagnostic_stage("Official URL validation", "success")
            item = normalize_for_storage(item, company)
            totals["processed_companies"] += 1
            if reasons: totals["verification_required"] += 1
            else: totals["successes"] += 1
            if args.diagnostic_mode:
                continue
            if reasons: queue = [x for x in queue if x.get("code") != company["code"]] + [item]
            else:
                if not any(x.get("code") == company["code"] for x in benefits):
                    benefits.append(item)
                    append_benefit_csv(DATA / "benefits.csv", item)
                queue = [x for x in queue if x.get("code") != company["code"]]
            progress["processed_codes"] = list(dict.fromkeys(progress.get("processed_codes", []) + [company["code"]]))
            progress["failed_codes"] = [x for x in progress.get("failed_codes", []) if x != company["code"]]
            progress["next_index"] = progress.get("next_index", 0) + 1
            progress["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic(DATA / "benefits.json", benefits); atomic(DATA / "verification-queue.json", queue); atomic(DATA / "discovery-progress.json", progress)
        except APIError as error:
            if args.diagnostic_mode and not failure_logged:
                failed_stage = failed_stage or active_failed_stage
                diagnostic_stage(active_label, "failure", error, key)
            if error.status == 429:
                progress = before_progress; errors.append({"code": company["code"], "status": 429, "error": error.message}); break
            totals["failures"] += 1; errors.append({"code": company["code"], "status": error.status, "error": error.message})
            if error.status in (401, 403, 404): break
        except Exception as error:
            if args.diagnostic_mode and not failure_logged:
                failed_stage = failed_stage or active_failed_stage
                diagnostic_stage(active_label, "failure", error, key)
            totals["failures"] += 1; errors.append({"code": company["code"], "error": str(error)[:300]})
    if not args.diagnostic_mode:
        totals["unique_web_search_call_ids"] = len(unique_web_search_call_ids)
        record = {"executed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "model": model,
                  "diagnostic_mode": False, **totals, "duration_seconds": round(time.monotonic()-started, 3), "errors": errors}
        usage_log=load(DATA / "openai-api-usage.json", []); usage_log.append(record); atomic(DATA / "openai-api-usage.json", usage_log)
    if args.diagnostic_mode:
        verification_required = totals["verification_required"] > 0 and not failed_stage
        result = "failure" if failed_stage else "success_with_verification_required" if verification_required else "success"
        print(f"Diagnostic result: {result}")
        if verification_required: print("Official validation: verification required")
        print(f"Failed stage: {failed_stage or 'none'}")
        print(f"Responses API calls: {totals['responses_api_calls']}")
        print(f"Web-search Responses requests: {totals['responses_with_web_search']}")
        print(f"Web-search output items: {totals['web_search_output_items']}")
        print(f"Unique web-search call IDs: {len(unique_web_search_call_ids)}")
        print(f"Web-search action types: {', '.join(sorted(web_search_action_types)) or 'none'}")
        print(f"Input tokens: {totals['input_tokens']}")
        print(f"Output tokens: {totals['output_tokens']}")
        return 1 if failed_stage else 0
    return 1 if totals["failures"] else 0


def parser():
    result=argparse.ArgumentParser(); result.add_argument("--batch-size", type=int, default=5); result.add_argument("--daily-limit", type=int, default=20)
    result.add_argument("--start-code"); result.add_argument("--end-code"); result.add_argument("--retry-failed", action="store_true")
    result.add_argument("--official-only", action="store_true"); result.add_argument("--diagnostic-mode", action="store_true"); return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
