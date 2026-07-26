#!/usr/bin/env python3
"""Discover shareholder benefits with Responses API search and strict JSON output.

Only URLs returned by the search tool are eligible evidence.  This program deliberately
uses the standard library and never persists an API key or a complete API response.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENDPOINT = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
OFFICIAL_HOSTS = ("jpx.co.jp", "tdnet.info")
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
        host = parsed.hostname.lower().removeprefix("www.")
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        return urlunparse(("https", host + port, parsed.path or "/", "", parsed.query, ""))
    except (TypeError, ValueError):
        return None


def hostname(value):
    normalized = canonical_url(value)
    return urlparse(normalized).hostname if normalized else None


def is_subdomain(host, parent):
    return bool(host and (host == parent or host.endswith("." + parent)))


def allowed_url(url, official_domain):
    host = hostname(url)
    if not host or any(is_subdomain(host, bad) for bad in BLOCKED_HOSTS):
        return False
    domains = OFFICIAL_HOSTS + ((official_domain.lower().removeprefix("www."),) if official_domain else ())
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
割引率や利用額で価値が変動する優待はannual_value_yen=null、valuation_type=not_calculatedとする。
evidence_textは根拠を200字以内で要約する。"""


def response_format():
    return {"type": "json_schema", "name": "shareholder_benefit", "strict": True, "schema": SCHEMA}


def build_payload(company, model=DEFAULT_MODEL):
    tool = {"type": "web_search", "search_context_size": "low"}
    domain = company.get("official_domain")
    if domain:
        tool["filters"] = {"allowed_domains": [domain, *OFFICIAL_HOSTS]}
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


def web_search_calls(response):
    return sum(item.get("type") == "web_search_call" for item in response.get("output", []))


def usage(response):
    value = response.get("usage", {})
    details = value.get("input_tokens_details", {}) or {}
    return {"input_tokens": value.get("input_tokens", 0), "cached_input_tokens": details.get("cached_tokens", 0),
            "output_tokens": value.get("output_tokens", 0)}


def structured_without_search(company, evidence, model):
    return {"model": model, "input": "以下のWeb検索結果だけをスキーマへ変換する。値やURLを作らない。\n" +
            json.dumps({"company": company, "search_result": evidence}, ensure_ascii=False),
            "store": False, "reasoning": {"effort": "none"}, "text": {"format": response_format()}}


def fetch_and_validate(url, company, source_urls):
    normalized = canonical_url(url)
    if normalized not in source_urls or not allowed_url(normalized, company.get("official_domain")):
        raise ValueError("source_url_not_allowed")
    request = Request(normalized, headers={"User-Agent": "kabunushi-yutai-dashboard/1.0 (source verification)"})
    with urlopen(request, timeout=25) as response:
        if response.status != 200: raise ValueError("source_http_not_200")
        final = canonical_url(response.geturl())
        if hostname(final) != hostname(normalized) or not allowed_url(final, company.get("official_domain")):
            raise ValueError("source_redirected_outside_official_domain")
        body = response.read(2_000_000)
    texts = [body.decode(codec, "ignore") for codec in ("utf-8", "shift_jis", "utf-16")]
    names = (company["name"], company["name"].replace("株式会社", ""), str(company["code"]))
    if not any(needle and needle in text for needle in names for text in texts):
        raise ValueError("company_identity_not_found")
    return final


def validate(item, company, sources, fetcher=None):
    fetcher = fetcher or fetch_and_validate
    reasons = []
    if item.get("code") != str(company["code"]) or item.get("name") != company["name"]: reasons.append("company_identity_mismatch")
    url = canonical_url(item.get("official_source_url"))
    if not url or url not in sources: reasons.append("source_not_in_search_results")
    elif not allowed_url(url, company.get("official_domain")):
        # For an unmapped company, accept a new corporate host only when the search
        # source itself names the company. The fetched page must still pass identity,
        # HTTPS, same-host redirect and HTTP checks below.
        source = sources.get(url, {}) if isinstance(sources, dict) else {}
        source_title = str(source.get("title") or "")
        bare_name = company["name"].replace("株式会社", "").replace("（株）", "")
        if company.get("official_domain") or not bare_name or bare_name not in source_title:
            reasons.append("source_domain_not_allowed")
        else:
            company = dict(company, official_domain=hostname(url))
            try: item["official_source_url"] = fetcher(url, company, set(sources))
            except Exception: reasons.append("official_source_validation_failed")
    else:
        try: item["official_source_url"] = fetcher(url, company, set(sources))
        except Exception: reasons.append("official_source_validation_failed")
    if item.get("minimum_shares") is None: reasons.append("minimum_shares_unknown")
    if not item.get("record_months") and not item.get("record_date"): reasons.append("record_date_unknown")
    if item.get("confidence_score", 0) < 90: reasons.append("low_confidence")
    if item.get("benefit_status") != "official_confirmed": reasons.append("current_program_not_confirmed")
    description = " ".join(str(item.get(k) or "") for k in ("benefit_title", "benefit_description", "conditions"))
    if re.search(r"割引|％|%|利用額", description):
        item["annual_value_yen"], item["valuation_type"] = None, "not_calculated"
    if reasons:
        item["benefit_status"] = "candidate"
        item["error_reason"] = ",".join(dict.fromkeys(reasons))
    return item, list(dict.fromkeys(reasons))


def choose(companies, args, progress, benefits):
    immutable = {x["code"] for x in benefits if x.get("benefit_status") in ("official_confirmed", "abolished")}
    failed = set(progress.get("failed_codes", []))
    candidates = [x for x in companies if x["code"] not in immutable]
    if args.start_code: candidates = [x for x in candidates if x["code"] >= args.start_code]
    if args.end_code: candidates = [x for x in candidates if x["code"] <= args.end_code]
    if args.retry_failed: candidates.sort(key=lambda x: x["code"] not in failed)
    else:
        start = progress.get("next_index", 0) % max(1, len(candidates)); candidates = candidates[start:] + candidates[:start]
    return candidates[:min(args.batch_size, args.daily_limit)]


def run(args):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        if args.diagnostic_mode:
            diagnostic_stage("API key check", "start")
            diagnostic_stage("API key check", "failure", ValueError("OPENAI_API_KEY is not set"))
            print("Diagnostic result: failure\nFailed stage: plain\nResponses API calls: 0\nWeb search calls: 0\nInput tokens: 0\nOutput tokens: 0")
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
    selected = companies if args.diagnostic_mode else choose(companies, args, progress, benefits)
    totals = {"processed_companies": 0, "responses_api_calls": 0, "web_search_calls": 0,
              "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "successes": 0, "verification_required": 0, "failures": 0}
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
            totals["responses_api_calls"] += 1; response = request_response(build_payload(company, model), key)
            totals["web_search_calls"] += web_search_calls(response)
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
            if totals["web_search_calls"] > totals["processed_companies"] + 1: raise ValueError("more_than_one_search_call")
            active_label = "Search source extraction"
            if args.diagnostic_mode: diagnostic_stage(active_label, "start")
            sources = search_sources(response)
            if args.diagnostic_mode: diagnostic_stage(active_label, "success", detail=f"{len(sources)} sources")
            active_label = "Official URL validation"
            active_failed_stage = "official_validation"
            if args.diagnostic_mode: diagnostic_stage(active_label, "start")
            item, reasons = validate(item, company, sources)
            if args.diagnostic_mode:
                if reasons:
                    failed_stage = failed_stage or "official_validation"
                    diagnostic_stage("Official URL validation", "failure", ValueError(",".join(reasons)), key)
                else: diagnostic_stage("Official URL validation", "success")
            totals["processed_companies"] += 1
            if reasons: totals["verification_required"] += 1
            else: totals["successes"] += 1
            if args.diagnostic_mode:
                continue
            if reasons: queue = [x for x in queue if x.get("code") != company["code"]] + [item]
            else: benefits.append(item)
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
        record = {"executed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "model": model,
                  "diagnostic_mode": False, **totals, "duration_seconds": round(time.monotonic()-started, 3), "errors": errors}
        usage_log=load(DATA / "openai-api-usage.json", []); usage_log.append(record); atomic(DATA / "openai-api-usage.json", usage_log)
    if args.diagnostic_mode:
        result = "failure" if failed_stage else "success"
        print(f"Diagnostic result: {result}")
        print(f"Failed stage: {failed_stage or 'none'}")
        print(f"Responses API calls: {totals['responses_api_calls']}")
        print(f"Web search calls: {totals['web_search_calls']}")
        print(f"Input tokens: {totals['input_tokens']}")
        print(f"Output tokens: {totals['output_tokens']}")
        return 1 if failed_stage else 0
    return 1 if totals["failures"] else 0


def parser():
    result=argparse.ArgumentParser(); result.add_argument("--batch-size", type=int, default=10); result.add_argument("--daily-limit", type=int, default=20)
    result.add_argument("--start-code"); result.add_argument("--end-code"); result.add_argument("--retry-failed", action="store_true")
    result.add_argument("--official-only", action="store_true"); result.add_argument("--diagnostic-mode", action="store_true"); return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
