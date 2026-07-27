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
BENEFIT_WORDS = ("株主優待", "優待制度", "優待券", "優待ポイント")
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
        "official_page_shareholder_benefit_excerpt": text[:20_000],
        "initial_structured_result": initial,
    }
    instructions = ("検証済み企業公式ページの本文だけを使い、株主優待情報を再抽出する。"
                    "本文にない値は推測しない。株数ごとの全区分と贈呈時期はconditionsに残す。")
    return {"model": model, "input": instructions + "\n" + json.dumps(prompt, ensure_ascii=False),
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
    decoded = meta + " " + decoded
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", decoded)).strip()


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
    values = [company.get("official_domain"), *(company.get("official_domains") or [])]
    return tuple(dict.fromkeys(normalized_host(value) for value in values if normalized_host(value)))


def registered_link_candidates(body, base_url, company):
    """Extract official detail/PDF/API URLs from one HTML response only."""
    html = body.decode("utf-8", "ignore")
    raw = re.findall(r'''(?is)\b(?:href|src)\s*=\s*["']([^"'#]+)["']''', html)
    # JSON strings cover JSON-LD, Next/Nuxt state and explicit API endpoints.
    raw += [value.replace(r"\/", "/") for value in re.findall(
        r'''(?i)["']((?:https?:)?\\?/\\?/[^"']+|/[^"']+)["']''', html)]
    domains = registered_domains(company)
    result = []
    for value in raw:
        linked = canonical_url(urljoin(base_url, value))
        host, path = hostname(linked), urlparse(linked).path.lower()
        official = any(is_subdomain(host, domain) for domain in domains)
        relevant = (path.endswith(".pdf") or bool(re.search(
            r"benefit|yutai|complimentary|shareholder|stockholder|kabunushi|株主|優待|/api/", value, re.I)))
        if linked and official and relevant and linked != canonical_url(base_url):
            result.append(linked)
    return list(dict.fromkeys(result))[:10]


def document_links(body, base_url):
    """Return URLs advertised by HTML, JSON-LD and embedded application JSON.

    This is intentionally structure-agnostic: discovery never guesses an issuer's
    IR directory.  Static links, escaped JSON URLs, and API endpoints emitted by a
    hydrated application all go through the same canonicalisation step.
    """
    html = body.decode("utf-8", "ignore")
    values = re.findall(r'''(?is)\b(?:href|src|action)\s*=\s*["']([^"'#]+)["']''', html)
    values += re.findall(r"(?is)<loc>\s*([^<\s]+)\s*</loc>", html)
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
    seeds = [canonical_url(f"https://{domains[0]}/"), canonical_url(f"https://{domains[0]}/sitemap.xml")]
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
            path = urlparse(link).path.lower()
            label_relevant = DISCOVERY_TERMS.search(link) or re.search(r"(?:^|/)(?:ir|investor|stock|shareholder)(?:/|$)", path)
            if path.endswith(".pdf"):
                relevant.append(link)
            elif link not in seen and not re.search(
                    r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|mp4|webm)$", path):
                queue.append(link)
    unique = list(dict.fromkeys(relevant))
    # Required global order: corporate HTML before corporate PDF.
    return sorted(unique, key=lambda url: (urlparse(url).path.lower().endswith(".pdf"), len(url)))


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
                                        page_fetcher=None, crawler=None):
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
            final = canonical_url(response.geturl())
            expected_domains = registered_domains(company)
            final_is_exchange = final and any(is_subdomain(hostname(final), exchange)
                                              for exchange in OFFICIAL_HOSTS)
            if not final or (expected_domains and not any(is_subdomain(hostname(final), value) for value in expected_domains) and
                             not final_is_exchange):
                raise ValueError(f"redirect_host_not_official_domain:{hostname(final) or 'invalid'}")
            if not expected_domains and hostname(final) != hostname(normalized):
                raise ValueError("source_redirected_outside_candidate_domain")
            content_type = str(response.headers.get("Content-Type", "")).lower() if hasattr(response, "headers") else ""
            body = response.read(2_000_000)
            content_encoding = (str(response.headers.get("Content-Encoding", ""))
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
    is_pdf = "pdf" in content_type or urlparse(final).path.lower().endswith(".pdf")
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
    identity_text = text + " " + source_metadata_text(source_urls, normalized)
    host, path = hostname(final), urlparse(final).path.lower()
    if is_subdomain(host, "jpx.co.jp") and any(path.startswith(value) for value in JPX_BLOCKED_PATHS):
        raise ValueError("jpx_corporate_page_not_company_disclosure")
    normalized_text = normalize_company_name(identity_text)
    name_found = normalize_company_name(company["name"]) in normalized_text
    code_found = security_code_found(identity_text, company["code"])
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
        host, path = hostname(overview), urlparse(overview).path.lower()
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
            is_exchange_pdf = (linked and urlparse(linked).path.lower().endswith(".pdf") and
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


def choose(companies, args, progress, benefits, queue=None):
    """Select only explicitly eligible companies; never scan the JPX master.

    Eligibility comes from a manual code list, the maintained benefit universe,
    or a TDnet review item.  Code ranges and progress cursors intentionally play
    no part in selection.
    """
    immutable = {x["code"] for x in benefits if x.get("benefit_status") in ("official_confirmed", "abolished")}
    manual = parse_security_codes(getattr(args, "security_codes", ""))
    universe = load_benefit_universe(DATA / "benefit-universe.csv")
    tdnet = tdnet_codes(load(DATA / "review-queue.json", []))
    eligible = set(manual) | universe | tdnet
    by_code = {str(company["code"]): company for company in companies}
    ordered = manual + sorted(eligible - set(manual))
    candidates = [by_code[code] for code in ordered if code in by_code and code not in immutable]
    return candidates[:min(args.batch_size, args.daily_limit)]


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


def append_research_log(company, result, reasons):
    """Keep non-official outcomes out of dashboard queues and counters."""
    path = DATA / "research-log.json"
    entries = load(path, [])
    entries.append({"code": company["code"], "name": company["name"],
                    "result": result, "reasons": list(reasons),
                    "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    atomic(path, entries)


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
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    companies = load(DATA / "listed-companies.json", [])
    domains = load(DATA / "company-domains.json", {})
    registered_sources = load_official_sources(DATA / "official-benefit-sources.json")
    for company in companies:
        if not company.get("official_domain"): company["official_domain"] = domains.get(company["code"])
    if args.diagnostic_mode:
        companies = [x for x in companies if x.get("code") == "1301"]
        if not companies: companies = [{"code": "1301", "name": "極洋", "official_domain": domains.get("1301")}]
    benefits = load(DATA / "benefits.json", []); queue = load(DATA / "verification-queue.json", [])
    progress = load(DATA / "discovery-progress.json", {"next_index": 0, "processed_codes": [], "failed_codes": []})
    selected = companies if args.diagnostic_mode else choose(companies, args, progress, benefits, queue)
    if not args.diagnostic_mode and not fixture:
        targets = ", ".join(f'{company["code"]} {company["name"]}' for company in selected) or "none"
        print(f"PRODUCTION TARGETS ({len(selected)}): {targets}")
    totals = {"processed_companies": 0, "responses_api_calls": 0, "responses_with_web_search": 0,
              "web_search_output_items": 0, "web_search_calls": 0,
              "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "successes": 0, "verification_required": 0, "research_log_saved": 0,
              "failures": 0}
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
    for company in selected:
        active_label = "Web search + Structured Outputs"
        active_failed_stage = "web_search"
        failure_logged = False
        try:
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
            final_url, official_text = discover_verified_official_source(
                company, registered, load(DATA / "review-queue.json", []))
            if not final_url:
                item = empty_extraction(company, None)
                reasons = ["official_source_not_found"]
            else:
                # OpenAI receives already-fetched text and performs structure
                # extraction only.  It never participates in URL discovery or IO.
                totals["responses_api_calls"] += 1
                correction = request_response(official_page_payload(
                    company, final_url, official_text,
                    empty_extraction(company, final_url), model), key)
                for key_name, value in usage(correction).items(): totals[key_name] += value
                item = json.loads(output_text(correction))
                item["code"], item["name"], item["official_source_url"] = (
                    str(company["code"]), company["name"], final_url)
                item, reasons = validate(item, company, {final_url: {}},
                                         fetcher=lambda url, _company, _sources: url)
                if item.get("long_term_required") is None:
                    reasons = list(dict.fromkeys([*reasons, "long_term_condition_unknown"]))
                    item["benefit_status"] = "candidate"
                    item["error_reason"] = ",".join(reasons)
                if not args.diagnostic_mode:
                    save_official_source(DATA / "official-benefit-sources.json", company, final_url,
                                         company.get("official_domains") or ())
            # Both fixed and searched sources converge on the same persistence path.
            item = normalize_for_storage(item, company)
            totals["processed_companies"] += 1
            if reasons: totals["verification_required"] += 1
            else: totals["successes"] += 1
            if args.diagnostic_mode:
                continue
            if reasons:
                append_research_log(company, "not_officially_verified", reasons)
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
        except Exception as error:
            if args.diagnostic_mode and not failure_logged:
                failed_stage = failed_stage or active_failed_stage
                diagnostic_stage(active_label, "failure", error, key)
            totals["failures"] += 1; errors.append({"code": company["code"], "error": str(error)[:300]})
            if not args.diagnostic_mode and not registered:
                append_research_log(company, "failed", [safe_message(error)])
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
                  "diagnostic_mode": False, **totals, "duration_seconds": round(time.monotonic()-started, 3), "errors": errors}
        usage_log=load(DATA / "openai-api-usage.json", []); usage_log.append(record); atomic(DATA / "openai-api-usage.json", usage_log)
        accounted = totals["successes"] + totals["verification_required"] + totals["failures"]
        summary_label = "FIXTURE SUMMARY" if fixture else "PRODUCTION SUMMARY"
        print(f"{summary_label}: "
              f"confirmed={totals['successes']} verification_queue={totals['verification_required']} "
              f"research_log={totals['research_log_saved']} failed={totals['failures']} "
              f"skipped=0 selected={len(selected)}")
        if not fixture:
            changed = subprocess.run(
                ["git", "diff", "--name-only", "--", "data"], cwd=ROOT,
                check=False, capture_output=True, text=True).stdout.splitlines()
            print("PRODUCTION SAVED CODES: " + (", ".join(confirmed_codes) or "none"))
            print("PRODUCTION CHANGED FILES: " + (", ".join(changed) or "none"))
        publish_workflow_counts(totals)
        if accounted != len(selected):
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
    result=argparse.ArgumentParser(); result.add_argument("--batch-size", type=int, default=5); result.add_argument("--daily-limit", type=int, default=20)
    result.add_argument("--security-codes", default="", help="comma-separated security codes")
    result.add_argument("--retry-failed", action="store_true")
    result.add_argument("--official-only", action="store_true"); result.add_argument("--diagnostic-mode", action="store_true"); return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
