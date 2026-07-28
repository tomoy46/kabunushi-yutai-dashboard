#!/usr/bin/env python3
"""Discover shareholder benefits with Responses API search and strict JSON output.

Only URLs returned by the search tool are eligible evidence.  This program deliberately
uses the standard library and never persists an API key or a complete API response.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import importlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from html import unescape

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENDPOINT = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
PRICING_CONFIG = ROOT / "config" / "openai-pricing.json"
OFFICIAL_HOSTS = ("jpx.co.jp", "tdnet.info")
JPX_BLOCKED_PATHS = ("/corporate/investor-relations/", "/corporate/about-jpx/")
BLOCKED_HOSTS = (
    "nikkei.com", "yahoo.co.jp", "finance.yahoo.co.jp", "minkabu.jp", "kabutan.jp",
    "irbank.net", "ir-bank.net", "yutai.net", "yutai-guide.jp", "kabuyutai.com",
    "ameblo.jp", "hatenablog.com", "hatena.ne.jp", "wordpress.com", "blog.jp",
    "rakuten-sec.co.jp",
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
    "long_term_condition_verified": {"type": ["boolean", "null"]},
    "conditions": {"type": ["string", "null"]}, "official_source_url": {"type": ["string", "null"]},
    "official_source_title": {"type": ["string", "null"]}, "official_verified_at": {"type": "string"},
    "abolished_at": {"type": ["string", "null"]}, "last_record_date": {"type": ["string", "null"]},
    "change_or_abolition_note": {"type": ["string", "null"]},
    "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
    "evidence_text": {"type": ["string", "null"]}, "error_reason": {"type": ["string", "null"]},
    "source_published_at": {"type": ["string", "null"]},
    "source_updated_at": {"type": ["string", "null"]},
}
SCHEMA = {"type": "object", "properties": FIELDS, "required": list(FIELDS), "additionalProperties": False}
BENEFIT_WORDS = ("株主優待", "株主ご優待", "株主優待制度", "優待券",
                 "株主優待カード", "株主優待ポイント", "株主様ご優待",
                 "株主特典", "株主還元", "商品贈呈", "割引券", "クーポン")
PRIORITY_MEDIUM_TERMS = ("株主優待", "株主ご優待", "株主様ご優待", "優待制度", "優待券")
RESEARCH_REASONS = ("official_site_discovery_failed", "required_shares_missing",
                    "benefit_content_missing", "record_month_missing", "confidence_low",
                    "redirect_domain_rejected", "pdf_parse_failed")
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36")
REEXTRACT_REASONS = {"record_date_unknown", "minimum_shares_unknown", "low_confidence", "current_program_not_confirmed"}
DISCOVERY_TERMS = re.compile(
    r"株主優待|優待制度|shareholder.?benefit|stockholder.?benefit|complimentary|yutai",
    re.I,
)


class OfficialSourceNotFound(Exception):
    """A maintained official URL returned HTTP 404."""


class OfficialSourceFetchError(Exception):
    """A classified failure while downloading or decoding a maintained URL."""

    def __init__(self, reason, error):
        self.reason = reason
        self.original = error
        super().__init__(f"{reason}: {type(error).__name__}: {safe_message(error)}")


class OpenAICallBudgetExhausted(Exception):
    """Signal a safely skipped candidate after the per-run/day budget is spent."""

    def __init__(self, item, reasons, url):
        self.item, self.reasons, self.url = item, reasons, url
        super().__init__(reasons[0])


class SparseOfficialEvidence(Exception):
    def __init__(self, reasons, url):
        self.reasons, self.url = reasons, url
        super().__init__(",".join(reasons))


def official_source_log(company, url, **fields):
    """Emit grep-friendly production diagnostics for a maintained source URL."""
    values = {"security_code": company["code"], "registered_url": url, **fields}
    print("Official source diagnostic: " + " | ".join(
        f"{key}={safe_message(value)}" for key, value in values.items()))


def decode_content(body, encoding):
    """Decode HTTP content codings advertised by the server."""
    encoding = str(encoding or "").lower().strip()
    if not encoding or encoding == "identity":
        return body
    # Decode in reverse order when a server applied multiple codings.
    for coding in reversed([value.strip() for value in encoding.split(",")]):
        if coding == "gzip":
            body = gzip.decompress(body)
        elif coding == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        elif coding == "br":
            try:
                brotli = importlib.import_module("brotli")
            except ImportError as error:
                raise OfficialSourceFetchError("brotli_decoder_unavailable", error) from error
            body = brotli.decompress(body)
        else:
            raise OfficialSourceFetchError("unsupported_content_encoding", ValueError(coding))
    return body


def safe_message(value, key=None):
    """Return a log-safe, bounded exception message."""
    message = str(value or "")
    if key:
        message = message.replace(key, "[REDACTED]")
    # API keys have a recognizable prefix. Redact one even when it is not the
    # exact key supplied to this process (for example, a server echo).
    message = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
    return message[:500]


def safe_text(value):
    """Coerce untrusted HTTP/API values to text before regex processing."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def normalize_japanese_text(value):
    """Normalize width, entities and whitespace before Japanese matching."""
    value = unescape(safe_text(value))
    value = unicodedata.normalize("NFKC", value).replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


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
    value = safe_text(value)
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
    host, parent = safe_text(host), safe_text(parent)
    return bool(host and parent and (host == parent or host.endswith("." + parent)))


def normalize_company_name(value):
    """Normalize harmless legal/width variants without fuzzy matching companies."""
    value = str(value or "").strip()
    # NFKC maps full-width Latin letters/digits and （株） to their ASCII forms.
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("株式会社", "").replace("(株)", "")
    value = re.sub(r"ホールディングス|holdings", "HD", value, flags=re.I)
    return re.sub(r"[\s\u3000・·•・‐‑‒–—−_-]+", "", value).strip().casefold()


def same_company_name(left, right):
    normalized = normalize_company_name(left)
    return bool(normalized and normalized == normalize_company_name(right))


def allowed_url(url, official_domain):
    host = hostname(url)
    if not host or any(is_subdomain(host, bad) for bad in BLOCKED_HOSTS):
        return False
    # Exchange disclosures remain first-party evidence even when the corporate
    # domain is known.  Their issuer identity is checked against the security
    # code after download.
    domains = ((official_domain.lower().removeprefix("www."),) + OFFICIAL_HOSTS
               if official_domain else OFFICIAL_HOSTS)
    return any(is_subdomain(host, domain) for domain in domains)


def official_url_decision(url, company, origin="search_result"):
    """Classify a URL before fetching it and return a stable audit reason.

    Search engines are locators, never authorities: a result is eligible only
    when its host was registered in the listed-company master, learned by
    following that registered site, or is an exchange disclosure whose issuer
    metadata contains the security code.
    """
    normalized, host = canonical_url(url), hostname(url)
    if not normalized or not host or any(is_subdomain(host, bad) for bad in BLOCKED_HOSTS):
        return False, "rejected_non_official"
    if any(is_subdomain(host, exchange) for exchange in OFFICIAL_HOSTS):
        return True, "official_exchange_disclosure"
    domains = registered_domains(company)
    matching = next((domain for domain in domains if is_subdomain(host, domain)), None)
    if not matching:
        return False, "rejected_non_official"
    if host != normalized_host(matching):
        return True, "official_ir_subdomain"
    return True, "official_company_domain"


def log_url_decision(company, url, accepted, reason, origin):
    print("Official URL candidate: " + " | ".join((
        f"security_code={safe_message(company.get('code'))}",
        f"url={safe_message(url)}", f"origin={origin}",
        f"accepted={'yes' if accepted else 'no'}", f"reason={reason}")))


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
        item["long_term_condition"] = ("なし" if item.get("long_term_condition_verified") else
                                       "公式資料に長期保有条件の記載なし")
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


def build_payload(company, evidence="", model=DEFAULT_MODEL):
    """Build a tool-free structuring request from locally acquired evidence."""
    prompt = {"company": {"code": company["code"], "name": company["name"]},
              "official_document_text": str(evidence)[:20_000]}
    return {"model": model,
            "input": "取得済み公式資料だけを構造化し、値やURLを推測しない。\n" +
                     json.dumps(prompt, ensure_ascii=False),
            "store": False, "reasoning": {"effort": "none"},
            "text": {"format": response_format()}}


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
        "official_candidate_metadata": company.get("official_candidate_metadata") or {},
        "official_page_shareholder_benefit_excerpt": text[:20_000],
        "initial_structured_result": initial,
    }
    instructions = ("検証済み企業公式ページの本文だけを使い、株主優待情報を再抽出する。"
                    "本文にない値は推測しない。株数ごとの全区分と贈呈時期はconditionsに残す。"
                    "公開日・更新日を確認できればsource_published_at・source_updated_atへ保存する。"
                    "長期保有条件なしという明記がある場合だけlong_term_condition_verified=trueとする。")
    return {"model": model, "input": instructions + "\n" + json.dumps(prompt, ensure_ascii=False),
            "max_output_tokens": 4_000,
            "store": False, "reasoning": {"effort": "none"}, "text": {"format": response_format()}}


def empty_extraction(company, url):
    """Supply schema-shaped context when a fixed source replaces web search."""
    item = {key: None for key in FIELDS}
    item.update({"code": str(company["code"]), "name": company["name"],
                 "benefit_status": "candidate", "record_months": [], "benefit_tiers": [],
                 "valuation_type": "not_calculated", "official_source_url": url,
                 "official_verified_at": dt.date.today().isoformat(), "confidence_score": 0})
    return item


def page_text(body, content_type=""):
    """Decode a bounded page and turn HTML into searchable plain text."""
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)
    if not isinstance(body, bytes):
        body = safe_text(body).encode("utf-8")
    content_type = safe_text(content_type)
    declared = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
    bom_codec = ("utf-8-sig" if body.startswith(b"\xef\xbb\xbf") else
                 "utf-16-le" if body.startswith(b"\xff\xfe") else
                 "utf-16-be" if body.startswith(b"\xfe\xff") else None)
    # Meta charset can itself be UTF-16, so inspect both the byte-compatible and
    # UTF-16 views.  Failed declarations then use the mandated Japanese order.
    probes = [body[:8192].decode("ascii", "ignore")]
    for probe_codec in ("utf-16-le", "utf-16-be"):
        try:
            probes.append(body[:8192].decode(probe_codec))
        except UnicodeError:
            pass
    meta_codec = None
    for probe in probes:
        meta = re.search(r"(?is)<meta\b[^>]*(?:charset\s*=\s*['\"]?\s*([^\s'\"/>;]+)|content\s*=\s*['\"][^'\"]*charset\s*=\s*([^\s'\";]+))", probe)
        if meta:
            meta_codec = next((part for part in meta.groups() if part), None)
            break
    codecs = [value for value in (declared.group(1) if declared else None, bom_codec, meta_codec,
                                   "utf-8", "shift_jis", "cp932", "euc_jp") if value]
    codecs = list(dict.fromkeys(codec.lower().replace("shift-jis", "shift_jis") for codec in codecs))
    for codec in codecs:
        if codec.replace("_", "-").startswith("utf-16") and not bom_codec and b"\x00" not in body[:512]:
            # A surprisingly common broken header says UTF-16LE for ordinary
            # UTF-8.  Decoding may technically succeed into CJK gibberish, so
            # reject it unless the bytes have a UTF-16 signal.
            continue
        try:
            decoded = body.decode(codec)
            break
        except (LookupError, UnicodeDecodeError, UnicodeError):
            continue
    else:
        # Never discard a successfully downloaded body solely due to encoding.
        decoded = body.decode("utf-8", errors="replace")
    title = " ".join(re.findall(r"(?is)<title\b[^>]*>(.*?)</title>", decoded))
    heading = " ".join(re.findall(r"(?is)<h1\b[^>]*>(.*?)</h1>", decoded))
    links = " ".join(re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", decoded))
    title, heading, links = (normalize_japanese_text(re.sub(r"(?is)<[^>]+>", " ", value))
                             for value in (title, heading, links))
    # Meta values are not text nodes, but are important corporate identity
    # evidence (description, og:title and og:site_name).
    meta = " ".join(match.group(1) for tag in re.findall(r"(?is)<meta\b[^>]*>", decoded)
                    if (match := re.search(r'''(?is)\bcontent\s*=\s*["']([^"']*)["']''', tag)))
    # Hydrated IR sites often put the only useful copy in JSON-LD, a framework
    # bootstrap object, or an application/json script.  Keep those payloads as
    # evidence while still dropping executable JavaScript and CSS.
    embedded = " ".join(re.findall(
        r'''(?is)<script\b[^>]*type\s*=\s*["'](?:application/ld\+json|application/json)["'][^>]*>(.*?)</script>''',
        decoded))
    next_data = " ".join(re.findall(
        r'''(?is)<script\b[^>]*id\s*=\s*["']__NEXT_DATA__["'][^>]*>(.*?)</script>''', decoded))
    decoded = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", decoded)
    decoded = embedded + " " + next_data + " " + decoded
    structured = (f"PAGE_TITLE[{title}] H1[{heading}] META_DESCRIPTION[{meta}] "
                  f"LINK_TEXT[{links}] ") if any((title, heading, meta, links)) else ""
    decoded = structured + decoded
    return normalize_japanese_text(re.sub(r"(?s)<[^>]+>", " ", decoded))


def pdf_text(body, diagnostic=None):
    """Extract PDF text with the system utility, retaining a safe fixture fallback."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "source.txt"
            source.write_bytes(body)
            completed = subprocess.run(
                ["pdftotext", "-layout", str(source), str(output)], capture_output=True,
                timeout=20, check=False,
            )
            if diagnostic:
                diagnostic(returncode=completed.returncode,
                           stderr=safe_message(completed.stderr.decode("utf-8", "replace")))
            if completed.returncode == 0 and output.exists():
                text = output.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return re.sub(r"\s+", " ", text).strip()
            if completed.returncode != 0:
                raise OfficialSourceFetchError(
                    "pdf_conversion_failure",
                    RuntimeError(completed.stderr.decode("utf-8", "replace")))
            if body.startswith(b"%PDF"):
                raise OfficialSourceFetchError(
                    "pdf_conversion_failure", RuntimeError("pdftotext produced no text"))
    except OfficialSourceFetchError:
        raise
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as error:
        if diagnostic:
            diagnostic(returncode="not_available", stderr=safe_message(error))
        # Preserve the readable fixture fallback, but never hide a real PDF
        # conversion failure.
        if body.startswith(b"%PDF"):
            raise OfficialSourceFetchError("pdf_conversion_failure", error) from error
    # Unit fixtures and a subset of simple PDFs expose readable text directly.
    return page_text(body)


def pdf_extractor_available():
    """Return whether the mandatory production PDF extractor is executable."""
    return shutil.which("pdftotext") is not None


def evidence_facts(text):
    """Report the four facts required before structured extraction."""
    text = normalize_japanese_text(text)
    return {
        "required_shares": bool(re.search(
            r"\d[\d,]*\s*株(?:以上)?|1\s*単元(?:以上)?|保有株式数|所有株式数に応じて", text)),
        "benefit_content": bool(re.search(
            r"株主(?:ご|様ご)?優待|優待(?:制度|券|内容|品|ポイント|食事)|\d[\d,]*\s*(?:円|ポイント)", text)),
        "record_month": bool(re.search(r"権利確定|基準日|(?:毎年\s*)?\d{1,2}\s*月(?:末日|\d{1,2}\s*日)?", text)),
        "long_term_condition": bool(re.search(r"継続保有|長期保有|保有期間|保有条件", text)),
    }


def regex_official_facts(text):
    """Re-extract shares/months from flattened HTML tables, lists and PDF text."""
    text = normalize_japanese_text(text)
    shares = [int(value.replace(",", "")) for value in re.findall(
        r"(?:保有|所有)?株式数?\s*(?<!\d)(\d{1,3}(?:,\d{3})*|\d+)\s*株(?:以上)?|"
        r"(?<!\d)(\d{1,3}(?:,\d{3})*|\d+)\s*株(?:以上)?", text)
              for value in value if value]
    # Japanese listed-company trading units are 100 shares.  The explicit
    # wording is useful in image-adjacent/OCR text even when the table omits it.
    if re.search(r"1\s*単元(?:以上)?", text):
        shares.append(100)
    months = [int(value) for value in re.findall(
        r"(?:毎年\s*)?(1[0-2]|0?[1-9])\s*月(?:末日|\d{1,2}\s*日)?", text)]
    return {"minimum_shares": min(shares) if shares else None,
            "record_months": list(dict.fromkeys(months))}


def apply_regex_official_facts(item, text):
    extracted = regex_official_facts(text)
    if item.get("minimum_shares") is None and extracted["minimum_shares"] is not None:
        item["minimum_shares"] = extracted["minimum_shares"]
    if not item.get("record_months") and extracted["record_months"]:
        item["record_months"] = extracted["record_months"]
        item["annual_occurrences"] = len(extracted["record_months"])
    return item


def apply_official_evidence_policy(item, text, url):
    """Apply registration policy to already downloaded first-party evidence."""
    facts = evidence_facts(text)
    core_ready = all(facts[name] for name in ("required_shares", "benefit_content", "record_month"))
    normalized = unicodedata.normalize("NFKC", safe_text(text))
    explicit_no_long_term = bool(re.search(
        r"(?:長期|継続)保有(?:条件)?(?:は|：|:)?(?:ありません|なし|不要)|保有期間(?:の)?条件(?:は|：|:)?なし",
        normalized))
    ended = bool(re.search(r"株主優待.{0,30}(?:廃止|終了)|(?:廃止|終了).{0,30}株主優待|過去の(?:株主)?優待", normalized))
    years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})年度?", normalized)]
    stale_pdf = urlparse(safe_text(url)).path.lower().endswith(".pdf") and bool(years) and max(years) < dt.date.today().year

    if not facts["long_term_condition"]:
        item["long_term_required"] = False
        item["long_term_condition_verified"] = False
    elif explicit_no_long_term:
        item["long_term_required"] = False
        item["long_term_condition_verified"] = True
    elif item.get("long_term_required") is True:
        item["long_term_condition_verified"] = True
    else:
        item["long_term_condition_verified"] = False

    # A current official benefit page is affirmative evidence of the current
    # programme; a model need not find a separate sentence saying "current".
    if core_ready and not ended and not stale_pdf:
        item["benefit_status"] = "official_confirmed"
        item["confidence_score"] = max(90, int(item.get("confidence_score") or 0))
    return item, facts, stale_pdf


def source_metadata_text(source_urls, url):
    """Return bounded search/disclosure metadata associated with a URL."""
    if not isinstance(source_urls, dict):
        return ""
    wanted = url_identity(url)
    metadata = next((value for key, value in source_urls.items()
                     if url_identity(key) == wanted and isinstance(value, dict)), {})
    return " ".join(str(metadata.get(key) or "")[:1_000]
                    for key in ("title", "description", "snippet", "security_code", "code"))


def security_code_found(text, code):
    """Match a listed code without confusing it with part of a longer number."""
    return bool(re.search(rf"(?<![0-9A-Z]){re.escape(str(code).upper())}(?![0-9A-Z])",
                          unicodedata.normalize("NFKC", safe_text(text)).upper()))


def registered_domains(company):
    """Return the issuer domains explicitly approved for redirects and links."""
    values = [company.get("official_domain"), company.get("official_url"),
              company.get("official_url_candidate"),
              *(company.get("official_url_candidates") or []),
              *(company.get("official_domains") or [])]
    return tuple(dict.fromkeys(normalized_host(value) for value in values if normalized_host(value)))


def registered_link_candidates(body, base_url, company):
    """Extract official detail/PDF/API URLs from one HTML response only."""
    if not isinstance(body, bytes):
        body = safe_text(body).encode("utf-8")
    html = body.decode("utf-8", "ignore")
    raw = re.findall(r'''(?is)\b(?:href|src)\s*=\s*["']([^"'#]+)["']''', html)
    # JSON strings cover JSON-LD, Next/Nuxt state and explicit API endpoints.
    raw += [value.replace(r"\/", "/") for value in re.findall(
        r'''(?i)["']((?:https?:)?\\?/\\?/[^"']+|/[^"']+)["']''', html)]
    domains = registered_domains(company)
    result = []
    for value in raw:
        linked = canonical_url(urljoin(base_url, value))
        host, path = hostname(linked), safe_text(urlparse(linked).path).lower()
        official = any(is_subdomain(host, domain) for domain in domains)
        relevant = (path.endswith(".pdf") or bool(re.search(
            r"benefit|yutai|complimentary|shareholder|stockholder|kabunushi|株主|優待|/api/", value, re.I)))
        is_document = not re.search(
            r"\.(?:png|jpe?g|gif|webp|svg|ico|avif)(?:$|\?)", path, re.I)
        if linked and official and relevant and is_document and linked != canonical_url(base_url):
            result.append(linked)
    return list(dict.fromkeys(result))[:10]


def document_links(body, base_url):
    """Return URLs advertised by HTML, JSON-LD and embedded application JSON.

    This is intentionally structure-agnostic: discovery never guesses an issuer's
    IR directory.  Static links, escaped JSON URLs, and API endpoints emitted by a
    hydrated application all go through the same canonicalisation step.
    """
    if not isinstance(body, bytes):
        body = safe_text(body).encode("utf-8")
    html = body.decode("utf-8", "ignore")
    values = re.findall(r'''(?is)\b(?:href|src|action)\s*=\s*["']([^"'#]+)["']''', html)
    values += re.findall(r"(?is)<loc>\s*([^<\s]+)\s*</loc>", html)
    values += re.findall(r"(?im)^\s*Sitemap\s*:\s*(https?://\S+)", html)
    values += re.findall(r'''(?i)["']((?:https?:)?(?:\\?/){2}[^"']+|/[^"']+)["']''', html)
    links = []
    for value in values:
        value = unescape(value).replace(r"\/", "/")
        normalized = canonical_url(urljoin(base_url, value))
        if normalized:
            links.append(normalized)
    return list(dict.fromkeys(links))


def fetch_discovery_document(url, allowed_domains):
    """Download one discovery document and reject unrelated redirects."""
    request = Request(url, headers={"User-Agent": BROWSER_USER_AGENT,
                                    "Accept": "text/html,application/xhtml+xml,application/xml,application/json,application/pdf"})
    with urlopen(request, timeout=25) as response:
        if response.status != 200:
            raise ValueError(f"HTTP_status_{response.status}")
        final = canonical_url(response.geturl())
        if not final or not any(is_subdomain(hostname(final), domain) for domain in allowed_domains):
            raise ValueError("redirect_host_not_verified")
        return final, response.read(2_000_000), str(getattr(response, "headers", {}).get("Content-Type", ""))


def discover_corporate_candidates(company, fetcher=None, max_pages=24):
    """Crawl official navigation/sitemaps and return HTML then PDF candidates.

    The company domain is the sole seed.  Paths are learned from documents, not
    assembled from company-specific templates.  JSON application state and API
    links are included by :func:`document_links`.
    """
    domains = registered_domains(company)
    if not domains:
        return []
    fetcher = fetcher or fetch_discovery_document
    seeds = [canonical_url(f"https://{domains[0]}{path}") for path in
             ("/", "/ir/", "/ir/stock/", "/ir/shareholder/", "/company/ir/",
              "/company/", "/company/profile/", "/corporate/profile/", "/stock/",
              "/sitemap.xml", "/robots.txt")]
    queue, seen, relevant = [url for url in seeds if url], set(), []
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            final, body, content_type = fetcher(url, domains)
        except Exception:
            continue
        text = page_text(body, content_type)
        links = [link for link in document_links(body, final)
                 if any(is_subdomain(hostname(link), domain) for domain in domains)]
        if DISCOVERY_TERMS.search(text) or DISCOVERY_TERMS.search(final):
            relevant.append(final)
        for link in links:
            path = safe_text(urlparse(link).path).lower()
            label_relevant = DISCOVERY_TERMS.search(link) or re.search(r"(?:^|/)(?:ir|investor|stock|shareholder)(?:/|$)", path)
            if path.endswith(".pdf"):
                relevant.append(link)
            elif link not in seen and not re.search(
                    r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|mp4|webm)$", path):
                queue.append(link)
    unique = list(dict.fromkeys(relevant))
    # Required global order: corporate HTML before corporate PDF.
    return sorted(unique, key=lambda url: (
        safe_text(urlparse(safe_text(url)).path).lower().endswith(".pdf"), len(safe_text(url))))


def exchange_candidates(company, review_items):
    """Extract matching TDnet/JPX disclosure URLs gathered by the local feed job."""
    result = []
    for item in review_items or []:
        haystack = " ".join(str(item.get(key) or "") for key in ("code", "title", "description"))
        if not security_code_found(haystack, company["code"]):
            continue
        for key in ("pdf_url", "url", "source_url", "link"):
            url = canonical_url(item.get(key))
            if url and any(is_subdomain(hostname(url), host) for host in OFFICIAL_HOSTS):
                result.append(url)
    return list(dict.fromkeys(result))


def save_official_source(path, company, url, allowed_domains=()):
    """Atomically cache a verified source; the file remains only a URL priority map."""
    values = load(path, {})
    entry = {"url": canonical_url(url)}
    aliases = [normalized_host(value) for value in allowed_domains
               if normalized_host(value) and normalized_host(value) != normalized_host(company.get("official_domain"))]
    if aliases:
        entry["allowed_domains"] = list(dict.fromkeys(aliases))
    values[str(company["code"]).upper()] = entry
    atomic(path, values)


def discover_verified_official_source(company, registered=None, review_items=None,
                                        page_fetcher=None, crawler=None, explored_out=None):
    """Resolve official evidence in one issuer-independent priority pipeline."""
    page_fetcher = page_fetcher or fetch_official_page
    crawler = crawler or discover_corporate_candidates
    # Keep this lazy: the corporate crawl must start *after* a maintained URL
    # has actually returned 404/invalid content, rather than prefetching it.
    attempts = [(registered["url"], True)] if registered else []
    explored = []
    index = 0
    corporate_added = False
    exchange_added = False
    while True:
        if index >= len(attempts):
            if not corporate_added:
                corporate_added = True
                try:
                    attempts.extend((url, True) for url in crawler(company))
                except (OfficialSourceFetchError, ValueError, TypeError):
                    pass
                continue
            if not exchange_added:
                exchange_added = True
                attempts.extend((url, False) for url in exchange_candidates(company, review_items))
                continue
            break
        url, trusted = attempts[index]
        index += 1
        explored.append(url)
        official_source_log(company, registered["url"] if registered else url,
                            exploration_url=url, exploration_state="trying")
        try:
            final, text = page_fetcher(url, company, {url: {}}, registered=trusted)
            if explored_out is not None:
                explored_out.extend(explored)
            official_source_log(company, registered["url"] if registered else url,
                                explored_urls=explored, adopted_url=final)
            return final, text
        except (OfficialSourceNotFound, OfficialSourceFetchError, ValueError, TypeError) as error:
            # A stale priority URL deliberately falls through to the same-domain
            # crawler and then exchange disclosures.
            official_source_log(company, registered["url"] if registered else url,
                                exploration_url=url, exploration_state="rejected",
                                exception_class=type(error).__name__, exception_message=error)
            continue
    official_source_log(company, registered["url"] if registered else "none",
                        explored_urls=explored, adopted_url="none")
    if explored_out is not None:
        explored_out.extend(explored)
    return None, ""


def free_search_official_source(company, opener=None, decisions_out=None, previously_rejected=()):
    """Use a free HTML search result only to locate and verify an issuer page.

    Search snippets are never evidence.  Every returned URL is downloaded and
    must contain the issuer identity and a benefit term before it is accepted.
    """
    opener = opener or urlopen
    query = urlencode({"q": f'{company["code"]} {company["name"]} 株主優待'})
    request = Request(f"https://html.duckduckgo.com/html/?{query}", headers={"User-Agent": BROWSER_USER_AGENT})
    try:
        with opener(request, timeout=20) as response:
            html = safe_text(response.read(1_000_000))
    except (HTTPError, URLError, socket.timeout, TimeoutError, ValueError):
        return None, ""
    links = re.findall(r'''(?is)href=["']([^"']+)["']''', html)
    for raw in links[:30]:
        parsed = urlparse(unescape(raw))
        target = dict(parse_qsl(parsed.query)).get("uddg") if "duckduckgo.com" in (parsed.hostname or "") else raw
        url = canonical_url(target)
        host = normalized_host(url)
        if url_identity(url) in {url_identity(value) for value in previously_rejected}:
            log_url_decision(company, url, False, "rejected_non_official", "durable_rejection_cache")
            continue
        # Unknown search hosts may be identity-probed, but are not evidence and
        # are never sent to OpenAI unless both issuer name and security code are
        # present on the downloaded corporate page.
        if (not url or not host or is_subdomain(host, "duckduckgo.com") or
                any(is_subdomain(host, blocked) for blocked in BLOCKED_HOSTS)):
            if decisions_out is not None:
                decisions_out.append((url, False, "rejected_non_official"))
            log_url_decision(company, url, False, "rejected_non_official", "search_result")
            continue
        candidate = dict(company, official_domain=host, official_domains=[host])
        try:
            final, text = fetch_official_page(url, candidate, {url: {}}, registered=False)
            identity = normalize_company_name(text[:20_000])
            if not (normalize_company_name(company["name"]) in identity and
                    security_code_found(text[:20_000], company["code"])):
                if decisions_out is not None:
                    decisions_out.append((url, False, "rejected_non_official"))
                log_url_decision(company, url, False, "rejected_non_official", "identity_mismatch")
                continue
            log_url_decision(company, url, True, "official_company_domain", "identity_verified_search_result")
            if decisions_out is not None:
                decisions_out.append((url, True, "official_company_domain"))
            return final, text
        except (OfficialSourceNotFound, OfficialSourceFetchError, ValueError, TypeError):
            continue
    return None, ""


def fetch_official_page(url, company, source_urls, registered=False, follow_links=True):
    normalized = canonical_url(url)
    if not registered and not any(url_identity(normalized) == url_identity(source) for source in source_urls):
        raise ValueError("source_url_not_in_search_results")
    if not any(allowed_url(normalized, domain) for domain in registered_domains(company) or (None,)):
        raise ValueError("source_url_not_allowed")
    request = Request(normalized, headers={"User-Agent": BROWSER_USER_AGENT,
                                           "Accept": "text/html,application/xhtml+xml,application/pdf",
                                           "Accept-Encoding": "gzip, br, deflate"})
    status = None
    try:
        with urlopen(request, timeout=25) as response:
            status = response.status
            if response.status == 404:
                raise OfficialSourceNotFound("official_source_http_404")
            if response.status != 200: raise ValueError(f"HTTP_status_{response.status}")
            final = canonical_url(safe_text(response.geturl()))
            expected_domains = registered_domains(company)
            final_is_exchange = final and any(is_subdomain(hostname(final), exchange)
                                              for exchange in OFFICIAL_HOSTS)
            redirected_alias = bool(final and expected_domains and
                                    not any(is_subdomain(hostname(final), value) for value in expected_domains) and
                                    not final_is_exchange)
            if not final:
                raise ValueError("redirect_host_not_official_domain:invalid")
            if not expected_domains and hostname(final) != hostname(normalized):
                raise ValueError("source_redirected_outside_candidate_domain")
            content_type = safe_text(response.headers.get("Content-Type", "")).lower() if hasattr(response, "headers") else ""
            body = response.read(2_000_000)
            if isinstance(body, (bytearray, memoryview)):
                body = bytes(body)
            elif not isinstance(body, bytes):
                body = safe_text(body).encode("utf-8")
            content_encoding = (safe_text(response.headers.get("Content-Encoding", ""))
                                if hasattr(response, "headers") else "")
            if not all(value.strip().lower() in ("gzip", "br", "deflate", "identity")
                       for value in content_encoding.split(",") if value.strip()):
                content_encoding = ""
            body = decode_content(body, content_encoding)
    except HTTPError as error:
        if registered:
            official_source_log(company, normalized, http_status=error.code, final_url=error.geturl(),
                                reason="http_404" if error.code == 404 else ("http_403" if error.code == 403 else f"http_{error.code}"),
                                exception_class=type(error).__name__, exception_message=error)
        if error.code == 404:
            raise OfficialSourceNotFound("official_source_http_404") from None
        reason = "http_403" if error.code == 403 else f"http_{error.code}"
        raise OfficialSourceFetchError(reason, error) from error
    except OfficialSourceNotFound:
        if registered:
            official_source_log(company, normalized, http_status=404, final_url=normalized,
                                reason="http_404")
        raise
    except (socket.timeout, TimeoutError) as error:
        if registered:
            official_source_log(company, normalized, http_status=status or "unavailable",
                                final_url="unavailable", reason="timeout",
                                exception_class=type(error).__name__, exception_message=error)
        raise OfficialSourceFetchError("timeout", error) from error
    except URLError as error:
        reason = "tls_failure" if isinstance(error.reason, Exception) and "ssl" in type(error.reason).__name__.lower() else "network_failure"
        if registered:
            official_source_log(company, normalized, http_status=status or "unavailable",
                                final_url="unavailable", reason=reason,
                                exception_class=type(error).__name__, exception_message=error)
        raise OfficialSourceFetchError(reason, error) from error
    except OfficialSourceFetchError as error:
        if registered:
            official_source_log(company, normalized, http_status=status or "unavailable",
                                final_url=locals().get("final") or "unavailable", reason=error.reason,
                                exception_class=type(error.original).__name__,
                                exception_message=error.original)
        raise
    final = safe_text(final)
    content_type = safe_text(content_type).lower()
    path = safe_text(urlparse(final).path).lower()
    if content_type.startswith("image/") or re.search(
            r"\.(?:png|jpe?g|gif|webp|svg|ico|avif)$", path, re.I):
        raise ValueError("official_source_is_image_not_document")
    is_pdf = "pdf" in content_type or path.endswith(".pdf")
    pdf_diagnostic = (lambda **values: official_source_log(
        company, normalized, http_status=status, final_url=final, content_type=content_type,
        body_characters=len(body), document_type="PDF", **values)) if registered else None
    try:
        text = pdf_text(body, pdf_diagnostic) if is_pdf else page_text(body, content_type)
    except Exception as error:
        if registered:
            official_source_log(company, normalized, http_status=status, final_url=final,
                                content_type=content_type, body_characters=len(body),
                                document_type="PDF" if is_pdf else "HTML", reason=getattr(error, "reason", "extraction_failure"),
                                exception_class=type(error).__name__, exception_message=error)
        raise
    if registered:
        javascript_empty = bool(not text and not is_pdf and re.search(rb"(?i)<script\b", body))
        official_source_log(company, normalized, http_status=status, final_url=final,
                            content_type=content_type or "unavailable", body_characters=len(body),
                            document_type="PDF" if is_pdf else "HTML",
                            extracted_text_preview=text[:200] or "(empty)",
                            javascript_dependent_empty=javascript_empty,
                            openai_body_characters=len(text),
                            fetch_reason="javascript_dependent_empty" if javascript_empty else ("extracted_text_empty" if not text else "success"))
    # Exactly one recursive level: prefer a maintained site's linked PDF/detail
    # page when it supplies more benefit facts than a JavaScript shell/overview.
    if registered and follow_links and not is_pdf:
        best = (final, text)
        for linked in registered_link_candidates(body, final, company):
            try:
                linked_final, linked_text = fetch_official_page(
                    linked, company, {linked: {}}, registered=True, follow_links=False)
            except Exception as error:
                official_source_log(company, linked, reason="linked_source_rejected",
                                    exception_class=type(error).__name__, exception_message=error)
                continue
            linked_text = safe_text(linked_text)
            best_text = safe_text(best[1])
            facts = sum(bool(re.search(pattern, linked_text)) for pattern in
                        (r"\d[\d,]*\s*株", r"\d[\d,]*\s*(?:円|ポイント)", r"(?:基準日|権利確定|\d{1,2}月)", r"(?:継続保有|長期保有|保有期間)"))
            best_facts = sum(bool(re.search(pattern, best_text)) for pattern in
                             (r"\d[\d,]*\s*株", r"\d[\d,]*\s*(?:円|ポイント)", r"(?:基準日|権利確定|\d{1,2}月)", r"(?:継続保有|長期保有|保有期間)"))
            if facts > best_facts or (facts == best_facts and len(linked_text) > len(best_text)):
                best = linked_final, linked_text
        final, text = best
    text = safe_text(text)
    official_source_log(company, normalized, **{
        f"evidence_{name}": "found" if found else "missing"
        for name, found in evidence_facts(text).items()
    })
    identity_text = text + " " + source_metadata_text(source_urls, normalized)
    host, path = hostname(final), urlparse(final).path.lower()
    if is_subdomain(host, "jpx.co.jp") and any(path.startswith(value) for value in JPX_BLOCKED_PATHS):
        raise ValueError("jpx_corporate_page_not_company_disclosure")
    normalized_text = normalize_company_name(identity_text)
    name_found = normalize_company_name(company["name"]) in normalized_text
    code_found = security_code_found(identity_text, company["code"])
    if locals().get("redirected_alias"):
        # Corporate reorganisations and brand migrations commonly redirect to a
        # new host.  Adopt it only after the landing document proves issuer
        # identity by legal/company name or security code.
        if not (name_found or code_found):
            raise ValueError("redirect_host_identity_mismatch")
        company["official_domains"] = list(dict.fromkeys([
            *(company.get("official_domains") or []), hostname(final)]))
    if any(is_subdomain(host, exchange) for exchange in OFFICIAL_HOSTS) and not registered:
        # The security code is the stable issuer identifier.  Do not reject a
        # valid disclosure merely because its company name uses an old name,
        # brand, HD abbreviation, spacing, punctuation, or width variant.
        if not code_found:
            raise ValueError("exchange_disclosure_identity_mismatch")
    elif not registered and not name_found:
        raise ValueError("company_identity_not_found")
    if not any(word in identity_text for word in BENEFIT_WORDS):
        raise ValueError("shareholder_benefit_text_not_found")
    return final, text


def load_official_sources(path):
    """Load code-keyed maintained URLs, accepting extensible object entries."""
    raw = load(path, {})
    result = {}
    for code, entry in raw.items() if isinstance(raw, dict) else []:
        url = entry if isinstance(entry, str) else entry.get("url") if isinstance(entry, dict) else None
        if canonical_url(url):
            result[str(code).upper()] = {"url": canonical_url(url), **(entry if isinstance(entry, dict) else {})}
    return result


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


def linked_official_sources(sources, company):
    """Follow official links on a JPX overview instead of citing the overview.

    A linked URL is eligible only when it points at the mapped corporate domain
    or at a JPX/TDnet PDF.  This keeps the evidence chain official and bounded.
    Failures (including stale/404 overview URLs) are ignored so the remaining
    search candidates can still be tried.
    """
    expanded = dict(sources)
    domain = company.get("official_domain")
    for overview in list(sources)[:5]:
        host, path = hostname(overview), safe_text(urlparse(safe_text(overview)).path).lower()
        if not (is_subdomain(host, "jpx.co.jp") and
                any(path.startswith(value) for value in JPX_BLOCKED_PATHS)):
            continue
        try:
            request = Request(overview, headers={"User-Agent": BROWSER_USER_AGENT,
                                                 "Accept": "text/html,application/xhtml+xml"})
            with urlopen(request, timeout=25) as response:
                if response.status != 200:
                    continue
                final = canonical_url(response.geturl()) or overview
                body = response.read(2_000_000)
            html = body.decode("utf-8", "ignore")
        except (HTTPError, URLError, socket.timeout, ValueError):
            continue
        for match in re.finditer(r'''(?is)\bhref\s*=\s*["']([^"'#]+)["']''', html):
            linked = canonical_url(urljoin(final, match.group(1)))
            linked_host = hostname(linked)
            linked_path = safe_text(urlparse(safe_text(linked)).path).lower()
            is_exchange_pdf = (linked and linked_path.endswith(".pdf") and
                               any(is_subdomain(linked_host, exchange) for exchange in OFFICIAL_HOSTS))
            is_corporate = bool(domain and is_subdomain(linked_host, domain))
            if linked and (is_exchange_pdf or is_corporate):
                expanded.setdefault(linked, {"title": "JPX linked official disclosure"})
    return expanded


def select_verified_source(item, company, sources, page_fetcher=None):
    page_fetcher = page_fetcher or fetch_and_validate
    sources = linked_official_sources(sources, company)
    for url in candidate_urls(sources, company, item.get("official_source_url")):
        try:
            result = page_fetcher(url, company, sources)
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


def parsed_time(value):
    """Parse an ISO timestamp from durable state, returning a UTC value or None."""
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=result.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def excluded_security(company):
    """Reject non-operating securities and delisted master rows for auto mode."""
    text = " ".join(str(company.get(key) or "") for key in
                    ("market", "sector", "name", "security_type", "status"))
    return bool(re.search(r"ETF|ETN|REIT|リート|インフラファンド|優先株|上場廃止", text, re.I)) or \
        company.get("listed") is False or company.get("delisted") is True


def free_priority(company, review_items=None, official_sources=None, universe=None):
    """Score normalized free official metadata; never use OpenAI for scheduling."""
    code = str(company["code"])
    source = (official_sources or {}).get(code, {})
    fields = ("official_source_url", "page_title", "title", "h1", "meta_description",
              "ir_title", "stock_title", "sitemap_text", "official_pdf_name",
              "pdf_name", "url_path", "link_text", "official_page_text")
    metadata = " ".join(safe_text(company.get(key)) for key in fields)
    metadata += " " + safe_text(source.get("url"))
    disclosures = []
    for item in review_items or []:
        if security_code_found(" ".join(safe_text(item.get(k)) for k in
                                        ("code", "title", "description")), code):
            value = " ".join(safe_text(item.get(k)) for k in
                             ("title", "description", "url", "pdf_url", "link_text"))
            metadata += " " + value
            disclosures.append(safe_text(item.get("title")))
    metadata = normalize_japanese_text(metadata)
    match_metadata = re.sub(r"\s+", "", metadata)
    detected = [term for term in PRIORITY_MEDIUM_TERMS if term in match_metadata]
    title_h1 = normalize_japanese_text(" ".join(safe_text(company.get(key))
                                                for key in ("page_title", "title", "h1")))
    title_h1_match = re.sub(r"\s+", "", title_h1)
    pdf_disclosure = normalize_japanese_text(" ".join([
        safe_text(company.get("official_pdf_name")), safe_text(company.get("pdf_name")),
        *disclosures]))
    page_obtained = bool(company.get("official_page_obtained") or
                         company.get("official_source_url") or source.get("url") or
                         any(company.get(key) for key in fields[1:]))
    if "株主優待" in title_h1_match or "株主優待制度" in re.sub(r"\s+", "", pdf_disclosure):
        score, priority = 100, "high"
    elif detected:
        score, priority = 50, "medium"
    elif page_obtained:
        score, priority = 10, "medium"
    else:
        score, priority = 0, "low"
    if score and code in (universe or set()):
        score += 15
    if score and re.search(r"(?:^|[/_-])(?:ir|investor|stock|shareholder)(?:[/_.-]|$)", metadata, re.I):
        score += 5
    return score, priority


def priority_diagnostic(company, url=None, official_text="", review_items=None):
    """Classify and log free evidence after the official fetch."""
    text = normalize_japanese_text(official_text)
    title = re.search(r"PAGE_TITLE\[(.*?)\]", text)
    heading = re.search(r"H1\[(.*?)\]", text)
    enriched = dict(company, official_source_url=url, official_page_obtained=bool(url),
                    official_page_text=text,
                    page_title=title.group(1) if title else company.get("page_title"),
                    h1=heading.group(1) if heading else company.get("h1"),
                    pdf_name=Path(urlparse(url).path).name if url else "")
    score, priority = free_priority(enriched, review_items)
    match_text = re.sub(r"\s+", "", normalize_japanese_text(
        " ".join((safe_text(url), text, safe_text(enriched.get("pdf_name"))))))
    detected = [term for term in PRIORITY_MEDIUM_TERMS if term in match_text]
    title_h1 = normalize_japanese_text(" ".join((safe_text(enriched.get("page_title")),
                                                  safe_text(enriched.get("h1")))))
    reason = ("title_or_h1" if "株主優待" in re.sub(r"\s+", "", title_h1) else
              "official_pdf_or_tdnet_title" if priority == "high" else
              "benefit_term" if detected else
              "official_page_without_term" if url else "official_page_not_obtained")
    print(f"Free priority {company['code']}: score={score} priority={priority} "
          f"reason={reason} detected_terms={','.join(detected) or 'none'} url={url or 'none'}")
    return score, priority


def quota_order(candidates, batch_size, review_items, official_sources, universe):
    """Mix 15 high, 7 medium and at least 3 low targets, then backfill."""
    buckets = {name: [] for name in ("high", "medium", "low")}
    for company in candidates:
        score, priority = free_priority(company, review_items, official_sources, universe)
        company["candidate_score"], company["candidate_priority"] = score, priority
        buckets[priority].append(company)
    for values in buckets.values():
        values.sort(key=lambda c: (-c["candidate_score"], str(c["code"])))
    limits = {"high": min(15, batch_size), "medium": min(7, max(0, batch_size - 3)),
              "low": min(3, batch_size)}
    selected = (buckets["high"][:limits["high"]] + buckets["medium"][:limits["medium"]] +
                buckets["low"][:limits["low"]])
    selected_codes = {str(c["code"]) for c in selected}
    remainder = [c for level in ("high", "medium", "low") for c in buckets[level]
                 if str(c["code"]) not in selected_codes]
    return (selected + remainder)[:batch_size]


def openai_eligible(priority, official_candidates, extracted_fact_count=0, low_sent=0):
    """Apply the deliberately permissive pre-analysis gate.

    URL/PDF provenance, not completeness of regex extraction, is the gate.  The
    final argument documents and regression-tests the five-low-candidate floor;
    it is not a ceiling, so useful low-priority candidates may all proceed.
    """
    if not official_candidates:
        return False
    if priority in ("high", "medium") and extracted_fact_count >= 1:
        return True
    if priority == "low" and low_sent < 5:
        return True
    return True


def choose(companies, args, progress, benefits, queue=None, now=None):
    """Choose manual targets first, otherwise scan genuinely unresearched issuers."""
    immutable = {x["code"] for x in benefits if x.get("benefit_status") in ("official_confirmed", "abolished")}
    manual = parse_security_codes(getattr(args, "security_codes", ""))
    by_code = {str(company["code"]): company for company in companies}
    auto = getattr(args, "auto_select", None)
    if manual:
        ordered = manual
    elif auto is True:
        ordered = [str(company["code"]) for company in companies]
    elif auto is None:  # compatibility for callers predating the workflow input
        eligible = load_benefit_universe(DATA / "benefit-universe.csv") | tdnet_codes(
            load(DATA / "review-queue.json", []))
        ordered = sorted(eligible)
    else:
        ordered = []
    candidates = [by_code[code] for code in ordered if code in by_code and code not in immutable]
    if not manual:
        candidates = [company for company in candidates if not excluded_security(company)]
    now = now or dt.datetime.now(dt.timezone.utc)
    recent_research, recent_failed = set(), set()
    for item in load(DATA / "research-log.json", []):
        checked = parsed_time(item.get("checked_at"))
        age = now - checked if checked else None
        failed = item.get("result") in ("failed", "api_failed", "url_fetch_failed", "analysis_failed")
        if failed and age is not None and age < dt.timedelta(days=7):
            recent_failed.add(str(item.get("code")))
        elif not failed and age is not None and age < dt.timedelta(days=30):
            recent_research.add(str(item.get("code")))
    if not getattr(args, "retry_research_log", False):
        candidates = [c for c in candidates if str(c["code"]) not in recent_research]
    if not getattr(args, "retry_failed", False):
        candidates = [c for c in candidates if str(c["code"]) not in recent_failed]
    companies_per_run = max(0, getattr(args, "companies_per_run", getattr(args, "batch_size", 25)))
    if manual or auto is not True:
        return candidates[:companies_per_run]
    review = load(DATA / "review-queue.json", [])
    sources = load_official_sources(DATA / "official-benefit-sources.json")
    universe = load_benefit_universe(DATA / "benefit-universe.csv")
    fresh = [c for c in candidates if str(c["code"]) not in recent_research]
    retry = [c for c in candidates if str(c["code"]) in recent_research][:5]
    ordered = quota_order(fresh, companies_per_run, review, sources, universe)
    # Explicit retries never displace more than five newly researched companies.
    if getattr(args, "retry_research_log", False) and retry:
        keep = max(0, companies_per_run - min(5, len(retry)))
        ordered = ordered[:keep] + quota_order(retry, min(5, len(retry)), review, sources, universe)
    return ordered[:companies_per_run]


def calls_today(records, now=None):
    """Count Responses calls already made on the current UTC day."""
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    return sum(int(record.get("responses_api_calls") or 0) for record in records
               if parsed_time(record.get("executed_at")) and
               parsed_time(record.get("executed_at")).date() == today)


def load_pricing(model, path=None):
    """Load the checked-in price for the model actually sent to OpenAI."""
    config = load(path or PRICING_CONFIG, {})
    try:
        price = config["models"][model]
        return {
            "input_usd_per_million": float(price["input_usd_per_million"]),
            "cached_input_usd_per_million": float(price["cached_input_usd_per_million"]),
            "output_usd_per_million": float(price["output_usd_per_million"]),
            "usd_to_jpy": float(config["usd_to_jpy"]),
            "source": price.get("source"),
            "verified_at": price.get("verified_at"),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"OpenAI pricing is not configured for model: {model}") from error


def estimated_cost_jpy(token_usage, pricing):
    """Price billed input (including its cached subset) and output tokens in JPY."""
    input_tokens = max(0, int(token_usage.get("input_tokens") or 0))
    cached_tokens = min(input_tokens, max(0, int(token_usage.get("cached_input_tokens") or 0)))
    output_tokens = max(0, int(token_usage.get("output_tokens") or 0))
    usd = ((input_tokens - cached_tokens) * pricing["input_usd_per_million"] +
           cached_tokens * pricing["cached_input_usd_per_million"] +
           output_tokens * pricing["output_usd_per_million"]) / 1_000_000
    return round(usd * pricing["usd_to_jpy"], 6)


def cost_today(records, now=None, pricing=None):
    """Sum durable estimated JPY charges for the current UTC day."""
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    total = 0.0
    for record in records:
        executed = parsed_time(record.get("executed_at"))
        if not executed or executed.date() != today:
            continue
        if record.get("estimated_cost_jpy") is not None:
            total += float(record["estimated_cost_jpy"])
        elif pricing:
            # Price legacy records so rollout-day usage cannot bypass the budget.
            total += estimated_cost_jpy(record, pricing)
    return round(total, 6)


def projected_request_cost_jpy(payload, pricing):
    """Conservatively reserve a request before its server token usage is known."""
    input_tokens = max(1, (len(json.dumps(payload, ensure_ascii=False)) + 3) // 4)
    output_tokens = int(payload.get("max_output_tokens") or 4_000)
    return estimated_cost_jpy({"input_tokens": input_tokens, "output_tokens": output_tokens}, pricing)


def parse_security_codes(value):
    codes = [code.strip().upper() for code in str(value or "").split(",") if code.strip()]
    invalid = [code for code in codes if not re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code)]
    if invalid:
        raise ValueError("invalid security code(s): " + ", ".join(invalid))
    return list(dict.fromkeys(codes))


def load_benefit_universe(path):
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as source:
        return {str(row.get("code", "")).strip().upper() for row in csv.DictReader(source)
                if str(row.get("code", "")).strip()}


def tdnet_codes(items):
    result = set()
    for item in items:
        code = str(item.get("code") or "").strip().upper()
        if re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code):
            result.add(code)
        for match in re.findall(r"(?<![0-9A-Z])(\d{4}|\d{3}[A-Z])(?![0-9A-Z])", str(item.get("title") or "").upper()):
            result.add(match)
    return result


def upsert_benefit_csv(path, item):
    """Insert or replace one officially verified result in the derived CSV."""
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
        rows = list(reader)
    row = {name: "" for name in fieldnames}
    row.update({name: item.get(name) for name in fieldnames if name in item})
    row["record_months"] = "|".join(map(str, item.get("record_months") or []))
    row["benefit_tiers_json"] = json.dumps(item.get("benefit_tiers") or [], ensure_ascii=False)
    rows = [existing for existing in rows if existing["code"] != item["code"]] + [row]
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def save_progress(progress, code, failed=False):
    """Record a production target exactly once as processed or failed."""
    if failed:
        progress["failed_codes"] = list(dict.fromkeys(progress.get("failed_codes", []) + [code]))
    else:
        progress["processed_codes"] = list(dict.fromkeys(progress.get("processed_codes", []) + [code]))
        progress["failed_codes"] = [value for value in progress.get("failed_codes", []) if value != code]
    progress["next_index"] = progress.get("next_index", 0) + 1
    progress["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def persist_production_state(benefits, queue, progress):
    atomic(DATA / "benefits.json", benefits)
    atomic(DATA / "verification-queue.json", queue)
    atomic(DATA / "discovery-progress.json", progress)


def append_research_log(company, result, reasons, source_url=None):
    """Keep non-official outcomes out of dashboard queues and counters."""
    path = DATA / "research-log.json"
    entries = load(path, [])
    entry = {"code": company["code"], "name": company["name"],
             "result": result, "reasons": list(reasons),
             "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if source_url:
        entry["source_url"] = canonical_url(source_url)
    # One source failure is one durable outcome.  Retrying requires the explicit
    # --retry-failed switch and replaces, rather than duplicates, that outcome.
    entries = [value for value in entries if not (
        value.get("code") == entry["code"] and value.get("result") == result and
        value.get("source_url") == entry.get("source_url"))]
    entries.append(entry)
    atomic(path, entries)


def append_unresolved(company, reasons, explored_urls=()):
    """Persist issuer-site discovery failures separately from sparse benefits."""
    path = DATA / "unresolved.json"
    entries = load(path, [])
    entry = {"code": str(company["code"]), "name": company["name"],
             "result": "official_site_discovery_failed", "reasons": list(reasons),
             "explored_urls": list(dict.fromkeys(canonical_url(url) for url in explored_urls
                                                  if canonical_url(url))),
             "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    entries = [value for value in entries if str(value.get("code")) != entry["code"]]
    entries.append(entry)
    atomic(path, entries)


def candidate_metadata(company, url, text, review_items, explored_urls):
    """Build bounded provenance context for OpenAI, including disclosure titles."""
    title = re.search(r"PAGE_TITLE\[(.*?)\]", safe_text(text))
    disclosures = []
    for item in review_items or []:
        haystack = " ".join(safe_text(item.get(key)) for key in ("code", "title", "description"))
        if security_code_found(haystack, company["code"]):
            disclosures.append({"title": safe_text(item.get("title"))[:500],
                                "url": canonical_url(item.get("pdf_url") or item.get("url"))})
    return {"url": canonical_url(url), "page_title": title.group(1)[:500] if title else None,
            "link_text": safe_text(company.get("link_text"))[:500] or None,
            "pdf_name": Path(urlparse(safe_text(url)).path).name if url else None,
            "explored_urls": list(dict.fromkeys(explored_urls))[:24],
            "tdnet_jpx_disclosures": disclosures[:10]}


def classified_reasons(reasons, facts=None):
    """Collapse implementation details into stable, actionable queue reasons."""
    facts = facts or {}
    mapped = []
    text = " ".join(map(safe_text, reasons))
    if "official_source_not_found" in text or "official_site_discovery_failed" in text:
        mapped.append("official_site_discovery_failed")
    if "redirect" in text or "domain_not_allowed" in text:
        mapped.append("redirect_domain_rejected")
    if "pdf" in text and re.search(r"parse|conversion|extract", text, re.I):
        mapped.append("pdf_parse_failed")
    for fact, reason in (("required_shares", "required_shares_missing"),
                         ("benefit_content", "benefit_content_missing"),
                         ("record_month", "record_month_missing")):
        if facts.get(fact) is False or ({"required_shares": "minimum_shares_unknown",
                                        "record_month": "record_date_unknown"}.get(fact) in reasons):
            mapped.append(reason)
    if "low_confidence" in reasons or (reasons and not mapped):
        mapped.append("confidence_low")
    return list(dict.fromkeys(mapped)) or ["confidence_low"]


def is_test_fixture():
    """Distinguish isolated test data from the repository's production data."""
    try:
        return DATA.resolve() != (ROOT / "data").resolve()
    except OSError:
        return True


def publish_workflow_counts(totals):
    """Expose durable outcome counts without making the workflow parse logs."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(f"confirmed={totals['successes']}\n")
        stream.write(f"research_log={totals['research_log_saved']}\n")
        stream.write(f"failed={totals['failures']}\n")
        stream.write(f"openai_calls={totals['responses_api_calls']}\n")
        for name in ("high", "medium", "low", "free_extraction_success",
                     "official_domains_found", "official_url_candidates_found",
                     "official_company_url_candidates", "non_official_urls_excluded",
                     "official_material_fetch_success", "post_official_fetch_openai_calls",
                     "pre_openai_excluded", "unresolved"):
            stream.write(f"{name}={totals.get(name, 0)}\n")
        stream.write("research_reasons=" + json.dumps(totals.get("research_reasons", {}),
                                                       ensure_ascii=False, separators=(",", ":")) + "\n")
        cause = totals.get("zero_confirmed_cause") or "none"
        stream.write(f"zero_confirmed_cause={cause}\n")


def diagnostic_outcome(totals, failed_stage):
    """Classify a diagnostic run without treating manual review as an error."""
    if failed_stage or totals["failures"]:
        return "failure", 1
    if totals["verification_required"]:
        return "success_with_verification_required", 0
    return "success", 0


def run(args):
    fixture = is_test_fixture()
    if fixture:
        print("TEST FIXTURE")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        if args.diagnostic_mode:
            diagnostic_stage("API key check", "start")
            diagnostic_stage("API key check", "failure", ValueError("OPENAI_API_KEY is not set"))
            print("Diagnostic result: failure\nFailed stage: plain\nResponses API calls: 0\nWeb-search Responses requests: 0\nWeb-search output items: 0\nUnique web-search call IDs: 0\nWeb-search action types: none\nInput tokens: 0\nOutput tokens: 0")
            return 1
        print("OPENAI_API_KEY is required", file=sys.stderr); return 2
    if not fixture and not pdf_extractor_available():
        print("PDF PREFLIGHT FAILED: pdftotext is unavailable; OpenAI API calls: 0", file=sys.stderr)
        publish_workflow_counts({"successes": 0, "research_log_saved": 0, "failures": 1,
                                 "responses_api_calls": 0,
                                 "zero_confirmed_cause": "pdftotext_unavailable"})
        return 1
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    try:
        pricing = load_pricing(model)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    companies = load(DATA / "listed-companies.json", [])
    domains = load(DATA / "company-domains.json", {})
    registered_sources = load_official_sources(DATA / "official-benefit-sources.json")
    for company in companies:
        master_candidate = (company.get("official_url") or company.get("official_url_candidate") or
                            next(iter(company.get("official_url_candidates") or []), None))
        mapped = domains.get(company["code"])
        if isinstance(mapped, dict):
            master_candidate = master_candidate or mapped.get("url") or mapped.get("official_url")
            mapped = mapped.get("domain")
        if not company.get("official_domain"):
            company["official_domain"] = mapped or normalized_host(master_candidate)
    if args.diagnostic_mode:
        companies = [x for x in companies if x.get("code") == "1301"]
        if not companies: companies = [{"code": "1301", "name": "極洋", "official_domain": domains.get("1301")}]
    benefits = load(DATA / "benefits.json", []); queue = load(DATA / "verification-queue.json", [])
    progress = load(DATA / "discovery-progress.json", {"next_index": 0, "processed_codes": [], "failed_codes": []})
    selected = companies if args.diagnostic_mode else choose(companies, args, progress, benefits, queue)
    previous_usage = load(DATA / "openai-api-usage.json", [])
    prior_daily_calls = calls_today(previous_usage)
    prior_daily_cost_jpy = cost_today(previous_usage, pricing=pricing)
    max_calls_per_run = max(0, getattr(args, "max_openai_calls_per_run",
                                      getattr(args, "max_openai_calls", 25)))
    max_calls_per_day = max(0, getattr(args, "max_openai_calls_per_day", 100))
    max_budget_jpy_per_day = max(0.0, getattr(args, "max_openai_budget_jpy_per_day", 100.0))
    if not args.diagnostic_mode and not fixture:
        targets = ", ".join(f'{company["code"]} {company["name"]}' for company in selected) or "none"
        print(f"PRODUCTION TARGETS ({len(selected)}): {targets}")
    totals = {"processed_companies": 0, "responses_api_calls": 0, "responses_with_web_search": 0,
              "web_search_output_items": 0, "web_search_calls": 0,
              "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "successes": 0, "verification_required": 0, "research_log_saved": 0,
              "failures": 0, "free_candidates_checked": 0, "benefit_candidates": 0,
              "free_extraction_success": 0, "research_reasons": {reason: 0 for reason in RESEARCH_REASONS},
              "high": 0, "medium": 0, "low": 0, "official_domains_found": 0,
              "official_url_candidates_found": 0, "official_company_url_candidates": 0,
              "non_official_urls_excluded": 0, "official_material_fetch_success": 0,
              "post_official_fetch_openai_calls": 0, "pre_openai_excluded": 0, "unresolved": 0}
    company_usage = []
    budget_deferred = 0
    low_sent = 0
    run_estimated_cost_jpy = 0.0
    confirmed_codes = []
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
    for company_index, company in enumerate(selected):
        facts = {}
        active_label = "Web search + Structured Outputs"
        active_failed_stage = "web_search"
        failure_logged = False
        try:
            totals["free_candidates_checked"] += 1
            # Scheduling classification (before network access) is deliberately
            # logged separately from the evidence-based post-fetch result.
            before_score, before_priority = free_priority(
                company, load(DATA / "review-queue.json", []), registered_sources)
            print(f"Free priority before fetch {company['code']}: score={before_score} "
                  f"priority={before_priority}")
            registered = registered_sources.get(str(company["code"]).upper())
            active_label = "Official source discovery and extraction"
            active_failed_stage = "official_discovery"
            if registered:
                approved = registered.get("allowed_domains") or []
                company["official_domains"] = list(dict.fromkeys([
                    normalized_host(registered["url"]), *approved,
                    *([company["official_domain"]] if company.get("official_domain") else [])]))
                if not company.get("official_domain"):
                    company["official_domain"] = company["official_domains"][0]
            review_items = load(DATA / "review-queue.json", [])
            explored_urls = []
            final_url, official_text = discover_verified_official_source(
                company, registered, review_items, explored_out=explored_urls)
            if not final_url and not registered and not fixture:
                search_decisions = []
                rejection_path = DATA / "rejected-official-candidates.json"
                rejection_cache = load(rejection_path, {})
                prior_rejections = rejection_cache.get(str(company["code"]), [])
                final_url, official_text = free_search_official_source(
                    company, decisions_out=search_decisions, previously_rejected=prior_rejections)
                totals["non_official_urls_excluded"] += sum(
                    not accepted for _url, accepted, _reason in search_decisions)
                rejected_now = [url for url, accepted, _reason in search_decisions if not accepted and url]
                if rejected_now and not args.diagnostic_mode:
                    rejection_cache[str(company["code"])] = list(dict.fromkeys(
                        [*prior_rejections, *rejected_now]))[-100:]
                    atomic(rejection_path, rejection_cache)
                if final_url and not company.get("official_domain"):
                    company["official_domain"] = normalized_host(final_url)
                if final_url:
                    explored_urls.append(final_url)
            # A known issuer host is useful provenance even when a benefit page
            # cannot be text-extracted.  Give discovery at least the mandated
            # top/IR/stock/shareholder/sitemap/robots candidate set.
            if company.get("official_domain"):
                totals["official_domains_found"] += 1
                host = company["official_domain"]
                explored_urls.extend(canonical_url(f"https://{host}{path}") for path in
                                     ("/", "/ir/", "/ir/stock/", "/ir/shareholder/",
                                      "/sitemap.xml", "/robots.txt"))
            explored_urls.extend(exchange_candidates(company, review_items))
            explored_urls = list(dict.fromkeys(url for url in explored_urls if url))
            totals["official_url_candidates_found"] += len(explored_urls)
            accepted_urls = []
            for explored in explored_urls:
                accepted, reason = official_url_decision(explored, company, "discovery_pipeline")
                log_url_decision(company, explored, accepted, reason, "discovery_pipeline")
                if accepted:
                    accepted_urls.append(explored)
                else:
                    totals["non_official_urls_excluded"] += 1
            explored_urls = accepted_urls
            totals["official_company_url_candidates"] += len(explored_urls)
            _score, fetched_priority = priority_diagnostic(
                company, final_url, official_text, load(DATA / "review-queue.json", []))
            totals[fetched_priority] += 1
            candidate_url = final_url or (explored_urls[0] if explored_urls else None)
            if not candidate_url:
                totals["processed_companies"] += 1
                totals["verification_required"] += 1
                totals["pre_openai_excluded"] += 1
                totals["unresolved"] += 1
                totals["research_reasons"]["official_site_discovery_failed"] += 1
                if not args.diagnostic_mode:
                    append_unresolved(company, ["official_site_discovery_failed"], explored_urls)
                    persist_production_state(benefits, queue, progress)
                print(f'{"FIXTURE RESULT" if fixture else "PRODUCTION RESULT"} '
                      f'{company["code"]} {company["name"]}: unresolved')
                continue
            else:
                final_url = candidate_url
                totals["official_material_fetch_success"] += 1
                totals["benefit_candidates"] += 1
                facts = evidence_facts(official_text)
                print("Evidence readiness " + str(company["code"]) + ": " + " ".join(
                    f"{name}={'found' if value else 'missing'}" for name, value in facts.items()))
                core_count = sum(facts[name] for name in
                                 ("required_shares", "benefit_content", "record_month"))
                if not openai_eligible(fetched_priority, explored_urls, core_count, low_sent):
                    raise AssertionError("official candidate gate invariant violated")
                if fetched_priority == "low":
                    low_sent += 1
                if core_count >= 1:
                    totals["free_extraction_success"] += 1
                company["official_candidate_metadata"] = candidate_metadata(
                    company, candidate_url, official_text, review_items, explored_urls)
                # OpenAI receives already-fetched text and performs structure
                # extraction only.  It never participates in URL discovery or IO.
                payload = official_page_payload(company, candidate_url, official_text,
                                                empty_extraction(company, candidate_url), model)
                projected_cost = projected_request_cost_jpy(payload, pricing)
                daily_calls = prior_daily_calls + totals["responses_api_calls"]
                daily_cost = prior_daily_cost_jpy + run_estimated_cost_jpy
                print(f"OpenAI budget check {company['code']}: daily_calls={daily_calls}/{max_calls_per_day} "
                      f"daily_estimated_cost_jpy={daily_cost:.6f}/{max_budget_jpy_per_day:.2f} "
                      f"projected_request_cost_jpy={projected_cost:.6f}")
                if (totals["responses_api_calls"] >= max_calls_per_run or
                        daily_calls >= max_calls_per_day or
                        daily_cost >= max_budget_jpy_per_day or
                        daily_cost + projected_cost > max_budget_jpy_per_day):
                    item = empty_extraction(company, candidate_url)
                    reasons = ["openai_call_budget_exhausted"]
                    raise OpenAICallBudgetExhausted(item, reasons, candidate_url)
                totals["responses_api_calls"] += 1
                totals["post_official_fetch_openai_calls"] += 1
                correction = request_response(payload, key)
                response_usage = usage(correction)
                for key_name, value in response_usage.items(): totals[key_name] += value
                company_cost = estimated_cost_jpy(response_usage, pricing)
                run_estimated_cost_jpy = round(run_estimated_cost_jpy + company_cost, 6)
                company_usage.append({"code": str(company["code"]), **response_usage,
                                      "estimated_cost_jpy": company_cost})
                item = json.loads(output_text(correction))
                item["code"], item["name"], item["official_source_url"] = (
                    str(company["code"]), company["name"], candidate_url)
                item = apply_regex_official_facts(item, official_text)
                item, _facts, stale_pdf = apply_official_evidence_policy(item, official_text, candidate_url)
                item, reasons = validate(item, company, {candidate_url: {}},
                                         fetcher=lambda url, _company, _sources: url)
                if stale_pdf:
                    reasons = list(dict.fromkeys([*reasons, "historical_pdf_not_current_evidence"]))
                    item["benefit_status"] = "candidate"
                    item["error_reason"] = ",".join(reasons)
                if not args.diagnostic_mode:
                    save_official_source(DATA / "official-benefit-sources.json", company, candidate_url,
                                         company.get("official_domains") or ())
            # Both fixed and searched sources converge on the same persistence path.
            item = normalize_for_storage(item, company)
            totals["processed_companies"] += 1
            if reasons: totals["verification_required"] += 1
            else: totals["successes"] += 1
            if args.diagnostic_mode:
                continue
            if reasons:
                reasons = classified_reasons(reasons, facts)
                for reason in reasons: totals["research_reasons"][reason] += 1
                append_research_log(company, "not_officially_verified", reasons, final_url)
                totals["research_log_saved"] += 1
                queue = [x for x in queue if x.get("code") != company["code"]]
                outcome = "research_log"
            else:
                benefits = [value for value in benefits if value.get("code") != company["code"]] + [item]
                upsert_benefit_csv(DATA / "benefits.csv", item)
                queue = [x for x in queue if x.get("code") != company["code"]]
                save_progress(progress, company["code"])
                confirmed_codes.append(company["code"])
                outcome = "confirmed"
            if registered:
                official_source_log(company, registered["url"], final_outcome=outcome,
                                    final_reason=",".join(reasons) if reasons else "officially_confirmed")
            persist_production_state(benefits, queue, progress)
            label = "FIXTURE RESULT" if fixture else "PRODUCTION RESULT"
            print(f'{label} {company["code"]} {company["name"]}: {outcome}')
            continue
        except OpenAICallBudgetExhausted as budget:
            # Do not mark deferred issuers as researched: the next run must be
            # able to select them again rather than silently omitting them.
            budget_deferred = len(selected) - company_index
            print(f"OPENAI BUDGET EXHAUSTED: deferred_companies={budget_deferred}")
            break
        except OfficialSourceNotFound as error:
            totals["processed_companies"] += 1
            totals["verification_required"] += 1
            if not args.diagnostic_mode:
                if registered:
                    official_source_log(company, registered["url"], final_outcome="research_log",
                                        final_reason="http_404",
                                        exception_class=type(error).__name__, exception_message=error)
                append_research_log(company, "official_source_not_found", [str(error)])
                totals["research_log_saved"] += 1
                queue = [x for x in queue if x.get("code") != company["code"]]
                persist_production_state(benefits, queue, progress)
                label = "FIXTURE RESULT" if fixture else "PRODUCTION RESULT"
                print(f'{label} {company["code"]} {company["name"]}: research_log')
        except APIError as error:
            if args.diagnostic_mode and not failure_logged:
                failed_stage = failed_stage or active_failed_stage
                diagnostic_stage(active_label, "failure", error, key)
            totals["failures"] += 1; errors.append({"code": company["code"], "status": error.status, "error": error.message})
            if not args.diagnostic_mode and not registered:
                append_research_log(company, "api_failed", [error.message])
                totals["research_log_saved"] += 1
            if not args.diagnostic_mode:
                if registered:
                    official_source_log(company, registered["url"], final_outcome="failed",
                                        final_reason="openai_api_failure",
                                        exception_class=type(error).__name__, exception_message=error)
                persist_production_state(benefits, queue, progress)
                label = "FIXTURE RESULT" if fixture else "PRODUCTION RESULT"
                print(f'{label} {company["code"]} {company["name"]}: failed')
        except SparseOfficialEvidence as sparse:
            totals["processed_companies"] += 1
            totals["verification_required"] += 1
            totals["research_log_saved"] += 1
            for reason in sparse.reasons: totals["research_reasons"][reason] += 1
            append_research_log(company, "not_officially_verified", sparse.reasons, sparse.url)
            persist_production_state(benefits, queue, progress)
            label = "FIXTURE RESULT" if fixture else "PRODUCTION RESULT"
            print(f'{label} {company["code"]} {company["name"]}: research_log (OpenAI skipped)')
        except Exception as error:
            if args.diagnostic_mode and not failure_logged:
                failed_stage = failed_stage or active_failed_stage
                diagnostic_stage(active_label, "failure", error, key)
            totals["failures"] += 1; errors.append({"code": company["code"], "error": str(error)[:300]})
            if not args.diagnostic_mode:
                reasons = classified_reasons([safe_message(error)], facts)
                for reason in reasons: totals["research_reasons"][reason] += 1
                append_research_log(company, "not_officially_verified", reasons,
                                    registered.get("url") if registered else None)
                totals["research_log_saved"] += 1
            if not args.diagnostic_mode:
                if registered:
                    official_source_log(company, registered["url"], final_outcome="failed",
                                        final_reason=getattr(error, "reason", "unhandled_exception"),
                                        exception_class=type(error).__name__, exception_message=error)
                persist_production_state(benefits, queue, progress)
                label = "FIXTURE RESULT" if fixture else "PRODUCTION RESULT"
                print(f'{label} {company["code"]} {company["name"]}: failed')
    if not args.diagnostic_mode:
        totals["unique_web_search_call_ids"] = len(unique_web_search_call_ids)
        record = {"executed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "model": model,
                  "diagnostic_mode": False, **totals,
                  "estimated_cost_jpy": round(run_estimated_cost_jpy, 6),
                  "daily_estimated_cost_jpy": round(prior_daily_cost_jpy + run_estimated_cost_jpy, 6),
                  "pricing": pricing, "company_usage": company_usage,
                  "duration_seconds": round(time.monotonic()-started, 3), "errors": errors}
        usage_log=load(DATA / "openai-api-usage.json", []); usage_log.append(record); atomic(DATA / "openai-api-usage.json", usage_log)
        accounted = totals["successes"] + totals["verification_required"] + totals["failures"]
        summary_label = "FIXTURE SUMMARY" if fixture else "PRODUCTION SUMMARY"
        print(f"{summary_label}: "
              f"confirmed={totals['successes']} verification_queue={totals['verification_required']} "
              f"research_log={totals['research_log_saved']} failed={totals['failures']} "
              f"skipped={budget_deferred} selected={len(selected)} free_checked={totals['free_candidates_checked']} "
              f"benefit_candidates={totals['benefit_candidates']} openai_calls={totals['responses_api_calls']}")
        rate = (100 * totals["successes"] / totals["processed_companies"]
                if totals["processed_companies"] else 0.0)
        print(f"PRIORITY SUMMARY: high={totals['high']} medium={totals['medium']} low={totals['low']} "
              f"free_extraction_success={totals['free_extraction_success']} confirmed_rate={rate:.1f}%")
        print(f"DISCOVERY SUMMARY: official_domains_found={totals['official_domains_found']} "
              f"official_url_candidates_found={totals['official_url_candidates_found']} "
              f"official_company_url_candidates={totals['official_company_url_candidates']} "
              f"non_official_urls_excluded={totals['non_official_urls_excluded']} "
              f"official_material_fetch_success={totals['official_material_fetch_success']} "
              f"post_official_fetch_openai_calls={totals['post_official_fetch_openai_calls']} "
              f"pre_openai_excluded={totals['pre_openai_excluded']} unresolved={totals['unresolved']}")
        print("RESEARCH-LOG REASONS: " + " ".join(
            f"{reason}={totals['research_reasons'][reason]}" for reason in RESEARCH_REASONS))
        print(f"API USAGE: input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']} "
              f"estimated_cost_jpy={run_estimated_cost_jpy:.6f} "
              f"daily_estimated_cost_jpy={prior_daily_cost_jpy + run_estimated_cost_jpy:.6f}")
        if not fixture:
            changed = subprocess.run(
                ["git", "diff", "--name-only", "--", "data"], cwd=ROOT,
                check=False, capture_output=True, text=True).stdout.splitlines()
            print("PRODUCTION SAVED CODES: " + (", ".join(confirmed_codes) or "none"))
            print("PRODUCTION CHANGED FILES: " + (", ".join(changed) or "none"))
        if totals["successes"] == 0:
            causes = sorted({safe_text(error.get("error")) for error in errors if error.get("error")})
            totals["zero_confirmed_cause"] = "; ".join(causes)[:500] or (
                "all_results_require_research" if totals["verification_required"] else "no_eligible_targets")
            print(f"ZERO CONFIRMED: OpenAI API calls={totals['responses_api_calls']} "
                  f"input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']} "
                  f"cause={totals['zero_confirmed_cause']}")
        publish_workflow_counts(totals)
        if accounted + budget_deferred != len(selected):
            print(f"ERROR: selected {len(selected)} companies but persisted outcomes for {accounted}", file=sys.stderr)
            return 1
    if args.diagnostic_mode:
        result, exit_code = diagnostic_outcome(totals, failed_stage)
        verification_required = result == "success_with_verification_required"
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
        return exit_code
    # Per-company failures are durable discovery outcomes, not a reason to skip
    # the commit step. Structural/accounting and diagnostic failures return early.
    return 0


def parser():
    result=argparse.ArgumentParser()
    result.add_argument("--companies-per-run", "--batch-size", dest="companies_per_run", type=int, default=25)
    result.add_argument("--max-openai-calls-per-run", "--max-openai-calls",
                        dest="max_openai_calls_per_run", type=int, default=25)
    result.add_argument("--max-openai-calls-per-day", type=int, default=100)
    result.add_argument("--max-openai-budget-jpy-per-day", type=float, default=100)
    result.add_argument("--security-codes", default="", help="comma-separated security codes")
    result.add_argument("--auto-select", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--retry-research-log", action="store_true")
    result.add_argument("--retry-failed", action="store_true")
    result.add_argument("--official-only", action="store_true"); result.add_argument("--diagnostic-mode", action="store_true"); return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
