"""FB1/FB2/FB3/FB4/FB5/FB6: pure settings, requests, provider parameters, envelopes, normalization and hooks.

No socket is opened and no provider is contacted.
"""

import json
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.web_search import logic, transport
from blocs.web_search.block import WebSearchBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from search_fixture import REF, TOKEN
from unittest.mock import patch

BRAVE = {"enabled": True, "provider": "brave", "secret_ref": REF}
SEARX = {"enabled": True, "provider": "searxng", "searxng_url": "https://search.example.org"}
WIKI = {"enabled": True, "provider": "wikipedia"}
# Built at run time: private and link-local literals never appear in versioned sources.
LAN = ".".join(("10", "0", "0", "5"))
METADATA = ".".join(("169", "254", "169", "254"))


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.SearchError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def params(target):
    return dict(parse_qsl(target["query"], keep_blank_values=True))


def settings_contract():
    """# - FB1 - remote access stays off by default, one provider, wallet keys, fixed origins and explicit SearXNG opt-ins."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["provider"] == "wikipedia" and defaults["api_base_url"] == ""
    assert defaults["allow_private_network"] is False and defaults["allow_insecure_http"] is False
    assert defaults["count"] == 10 and defaults["safe_search"] == "moderate" and defaults["time_range"] == ""
    assert logic.configuration(WIKI)["enabled"] is True, "Wikipedia needs no key"
    assert logic.configuration(MappingProxyType(BRAVE))["secret_ref"] == REF
    assert logic.configuration({**BRAVE, "execution": {"inhibited": False}})["provider"] == "brave"
    normalized = logic.configuration({**BRAVE, "language": " PT_BR ", "region": "fr"})
    assert normalized["language"] == "pt-br" and normalized["region"] == "FR"
    for raw in ({"enabled": True, "provider": "brave"}, {"enabled": True, "provider": "serpapi"},
                {**BRAVE, "secret_ref": TOKEN}, {**BRAVE, "secret_ref": "env://BRAVE"}, {**BRAVE, "api_key": TOKEN},
                {**BRAVE, "provider": "google"}, {**BRAVE, "enabled": "yes"}, {**BRAVE, "language": "english language"},
                {**BRAVE, "language": "e"}, {**BRAVE, "region": "USA"}, {**BRAVE, "region": 1},
                {**BRAVE, "safe_search": "high"}, {**BRAVE, "time_range": "hour"},
                {**BRAVE, "api_base_url": "https://api.search.brave.com"}, {**BRAVE, "api_base_url": "https://example.com"},
                {**BRAVE, "api_base_url": "http://127.0.0.1:8080/res/v1"}, {**BRAVE, "api_base_url": "ftp://127.0.0.1"},
                {**BRAVE, "api_base_url": "http://user:pw@127.0.0.1:8080"}, {**BRAVE, "api_base_url": "http://127.0.0.1:8080?x=1"},
                {**BRAVE, "ca_file": "/srv/ca.pem"}, {**BRAVE, "ca_file": "ca.pem"},
                {**BRAVE, "api_base_url": "http://127.0.0.1:8080", "ca_file": "/srv/ca.pem"},
                {"enabled": True, "provider": "searxng"}, {**SEARX, "searxng_url": "http://search.example.org"},
                {**SEARX, "searxng_url": "https://search.example.org/search?q=x"}, {**SEARX, "searxng_url": "https://u:p@search.example.org"},
                {**SEARX, "searxng_url": "https://search.example.org/../x"}, {**SEARX, "searxng_url": "https://127.0.0.1:8888"},
                {**SEARX, "searxng_url": "https://localhost:8888"}, {**SEARX, "searxng_url": "https://" + LAN},
                {**SEARX, "searxng_url": "https://" + METADATA, "allow_private_network": True},
                {**SEARX, "searxng_url": "https://[fe80::1]", "allow_private_network": True},
                {**SEARX, "searxng_url": "http://search.example.org", "ca_file": "/srv/ca.pem", "allow_insecure_http": True},
                {**SEARX, "allow_private_network": "true"}, "not an object"):
        error = rejected(lambda raw=raw: logic.configuration(raw), "config")
        assert TOKEN not in json.dumps(error.envelope())
    for key, (low, high) in logic.BOUNDS.items():
        for bad in (low - 1, high + 1, True, float(low), str(low)):
            rejected(lambda key=key, bad=bad: logic.configuration({**BRAVE, key: bad}), "config")
        assert logic.configuration({**BRAVE, key: low})[key] == low
        assert logic.configuration({**BRAVE, key: high})[key] == high
    bases = {"http://127.0.0.1:8081": "http://127.0.0.1:8081", "http://localhost:9/": "http://localhost:9",
             "https://[::1]:7": "https://[::1]:7", " ": ""}
    for raw, expected in bases.items():
        assert logic.configuration({**BRAVE, "api_base_url": raw})["api_base_url"] == expected
    assert logic.configuration({**BRAVE, "api_base_url": "https://127.0.0.1:8443", "ca_file": "/srv/ca.pem"})["ca_file"] == "/srv/ca.pem"
    lan = logic.configuration({**SEARX, "searxng_url": "http://" + LAN + ":8888/searx/", "allow_private_network": True,
                               "allow_insecure_http": True})
    assert lan["searxng_url"] == "http://" + LAN + ":8888/searx" and logic.address_policy(lan) == "private"
    assert logic.configuration({**SEARX, "searxng_url": "https://localhost", "allow_private_network": True})["searxng_url"] == "https://localhost"
    assert logic.configuration({**SEARX, "ca_file": "/srv/ca.pem"})["ca_file"] == "/srv/ca.pem"
    assert logic.address_policy(logic.configuration(SEARX)) == "public"
    assert logic.address_policy(logic.configuration(BRAVE)) == "public"
    assert logic.address_policy(logic.configuration({**BRAVE, "api_base_url": "http://127.0.0.1:9"})) == "loopback"
    # Settings of another provider are ignored rather than blocking a switch of provider.
    assert logic.configuration({**WIKI, "secret_ref": REF, "searxng_url": "http://search.example.org"})["provider"] == "wikipedia"
    assert logic.configuration({**SEARX, "api_base_url": "http://127.0.0.1:9"})["searxng_url"] == "https://search.example.org"
    assert logic.api_token(" " + TOKEN + "\n") == TOKEN
    for bad in ("", "short", "Bearer " + TOKEN, TOKEN + "\r\nX: y", TOKEN + " y", "clé-" + "a" * 20, None, 42):
        rejected(lambda bad=bad: logic.api_token(bad), "secret")
    redacted = logic.redact("key " + TOKEN + " https://serpapi.com/search?q=x&api_key=abcdef123 X-Subscription-Token: zzz9", TOKEN)
    assert TOKEN not in redacted and "abcdef123" not in redacted and "zzz9" not in redacted and redacted.count("***") == 3


def request_contract():
    """# - FB2 - # - FB3 - queries are validated before any wallet or network access, with explicit defaults."""
    brave = logic.configuration({**BRAVE, "count": 7, "language": "fr", "region": "be", "time_range": "month"})
    plain = logic.parse_request("  ada\n lovelace\t notes ", brave)
    assert plain == {"operation": "search", "request_id": None, "provider": "brave", "query": "ada lovelace notes",
                     "count": 7, "page": 1, "language": "fr", "region": "BE", "safe_search": "moderate", "time_range": "month"}
    assert logic.parse_request(b"bytes query", brave)["query"] == "bytes query"
    assert logic.parse_request("[1, 2] as words", brave)["query"] == "[1, 2] as words"
    explicit = logic.parse_request(json.dumps({"query": "q", "count": 20, "page": 10, "language": "DE", "region": "at",
                                               "safe_search": "strict", "time_range": "", "request_id": "r-1"}), brave)
    assert (explicit["count"], explicit["page"], explicit["language"], explicit["region"]) == (20, 10, "de", "AT")
    assert explicit["safe_search"] == "strict" and explicit["time_range"] == "" and explicit["request_id"] == "r-1"
    assert logic.parse_request(MappingProxyType({"operation": "search", "query": "q"}), brave)["operation"] == "search"
    for bad in ('{"query":', '{"query": "a", "query": "b"}', '{"query": NaN}', "", "   ", {"query": ""}, {"query": 5},
                {"query": "q", "count": 0}, {"query": "q", "count": 21}, {"query": "q", "count": "5"},
                {"query": "q", "page": 11}, {"query": "q", "page": 0}, {"query": "q", "safe_search": "none"},
                {"query": "q", "time_range": "hour"}, {"query": "q", "region": "FRA"}, {"query": "q", "language": "?"},
                {"query": "q", "url": "https://example.com"}, {"query": "q", "api_key": "x"}, {"operation": "fetch"},
                {"query": "q", "request_id": "bad id"}, {"query": "x" * 401}, {"query": " ".join(["w"] * 51)},
                {"operation": "wikipedia_summary", "title": "Ada"}, 42, ["q"], "x" * 70000):
        error = rejected(lambda bad=bad: logic.parse_request(bad, brave), "invalid_input")
        assert len(str(error)) < 200
    assert logic.parse_request({"query": " ".join(["w"] * 51)}, logic.configuration(SEARX))["query"].count("w") == 51
    wiki = logic.configuration({**WIKI, "region": "us", "time_range": "week", "safe_search": "strict"})
    search = logic.parse_request({"query": "Ada", "region": "fr", "safe_search": "off"}, wiki)
    assert search["language"] == "en" and search["region"] == "" and search["safe_search"] is None and search["time_range"] == ""
    rejected(lambda: logic.parse_request({"query": "Ada", "time_range": "week"}, wiki), "invalid_input")
    rejected(lambda: logic.parse_request({"query": "x" * 301}, wiki), "invalid_input")
    summary = logic.parse_request({"operation": "wikipedia_summary", "title": "  Ada   Lovelace ", "request_id": "s-1"}, wiki)
    assert summary == {"operation": "wikipedia_summary", "request_id": "s-1", "provider": "wikipedia", "title": "Ada Lovelace",
                       "query": None, "language": "en"}
    by_query = logic.parse_request({"operation": "wikipedia_summary", "query": "first programmer", "language": "fr"}, wiki)
    assert by_query["query"] == "first programmer" and by_query["title"] is None and by_query["language"] == "fr"
    link = logic.parse_request({"operation": "wikipedia_summary", "title": "https://fr.m.wikipedia.org/wiki/Ada_Lovelace#Vie"}, wiki)
    assert link["title"] == "Ada Lovelace" and link["language"] == "fr"
    accented = logic.parse_request({"operation": "wikipedia_summary", "title": "https://de.wikipedia.org/wiki/K%C3%B6ln"}, wiki)
    assert accented["title"] == "Köln" and accented["language"] == "de"
    for bad in ({"operation": "wikipedia_summary"}, {"operation": "wikipedia_summary", "title": "A", "query": "B"},
                {"operation": "wikipedia_summary", "title": "A[1]"}, {"operation": "wikipedia_summary", "title": "a#b"},
                {"operation": "wikipedia_summary", "title": "x" * 256}, {"operation": "wikipedia_summary", "title": ""},
                {"operation": "wikipedia_summary", "title": "http://en.wikipedia.org/wiki/Ada"},
                {"operation": "wikipedia_summary", "title": "https://en.wikipedia.org.evil.example/wiki/Ada"},
                {"operation": "wikipedia_summary", "title": "https://en.wikipedia.org/w/index.php?title=Ada"},
                {"operation": "wikipedia_summary", "title": "https://fr.wikipedia.org/wiki/Ada", "language": "en"},
                {"operation": "wikipedia_summary", "title": "Ada", "count": 3}):
        rejected(lambda bad=bad: logic.parse_request(bad, wiki), "invalid_input")


def target_contract():
    """# - FB2 - # - FB3 - each provider receives its documented parameters; the filters actually sent are reported."""
    brave = logic.configuration({**BRAVE, "language": "pt-br", "region": "br", "time_range": "day", "safe_search": "strict"})
    target, sent = logic.search_target(brave, logic.parse_request({"query": "café crème", "count": 5, "page": 3}, brave))
    assert (target["origin"], target["path"]) == ("https://api.search.brave.com", "/res/v1/web/search")
    assert params(target) == {"q": "café crème", "count": "5", "offset": "2", "safesearch": "strict", "text_decorations": "false",
                              "search_lang": "pt-br", "country": "BR", "freshness": "pd"}
    assert "%20" in target["query"] and "+" not in target["query"]
    assert sent == {"language": "pt-br", "region": "BR", "safe_search": "strict", "time_range": "day"}
    serp = logic.configuration({**BRAVE, "provider": "serpapi"})
    target, sent = logic.search_target(serp, logic.parse_request({"query": "q", "count": 4, "page": 2, "language": "fr",
                                                                   "region": "ca", "time_range": "year"}, serp))
    assert (target["origin"], target["path"]) == ("https://serpapi.com", "/search")
    assert params(target) == {"engine": "google", "q": "q", "num": "4", "start": "4", "hl": "fr", "gl": "ca", "tbs": "qdr:y"}
    assert sent == {"language": "fr", "region": "CA", "safe_search": None, "time_range": "year"}, "moderate sends no safe filter"
    first, _ = logic.search_target(serp, logic.parse_request({"query": "q", "safe_search": "strict"}, serp))
    assert "start" not in params(first) and params(first)["safe"] == "active"
    assert params(logic.search_target(serp, logic.parse_request({"query": "q", "safe_search": "off"}, serp))[0])["safe"] == "off"
    assert "api_key" not in first["query"], "the key is added only inside the network worker"
    searx = logic.configuration({**SEARX, "searxng_url": "https://search.example.org/searx/", "language": "zh-hans"})
    target, sent = logic.search_target(searx, logic.parse_request({"query": "q", "region": "tw", "safe_search": "off"}, searx), 4)
    assert (target["origin"], target["path"]) == ("https://search.example.org", "/searx/search")
    assert params(target) == {"q": "q", "format": "json", "pageno": "4", "safesearch": "0", "language": "zh-Hans"}
    assert sent == {"language": "zh-Hans", "region": None, "safe_search": "off", "time_range": None}, "a subtag already set wins"
    bare = logic.search_target(searx, logic.parse_request({"query": "q", "language": "pt", "region": "br"}, searx))
    assert params(bare[0])["language"] == "pt-BR" and bare[1]["region"] == "BR"
    regional = logic.search_target(searx, logic.parse_request({"query": "q", "language": "fr-ca", "region": "fr",
                                                               "safe_search": "strict", "time_range": "week"}, searx))
    assert params(regional[0])["language"] == "fr-CA" and regional[1]["region"] is None and params(regional[0])["safesearch"] == "2"
    assert params(regional[0])["time_range"] == "week"
    no_language = logic.search_target(logic.configuration(SEARX), logic.parse_request({"query": "q", "region": "fr"},
                                                                                        logic.configuration(SEARX)))
    assert "language" not in params(no_language[0]) and no_language[1]["region"] is None
    wiki = logic.configuration(WIKI)
    target, sent = logic.search_target(wiki, logic.parse_request({"query": "Ada", "count": 5, "page": 3, "language": "fr"}, wiki))
    assert (target["origin"], target["path"]) == ("https://fr.wikipedia.org", "/w/api.php")
    assert params(target) == {"action": "query", "format": "json", "formatversion": "2", "list": "search", "srsearch": "Ada",
                              "srlimit": "5", "sroffset": "10", "srnamespace": "0", "srprop": "snippet", "srinfo": "totalhits"}
    assert sent == {"language": "fr", "region": None, "safe_search": None, "time_range": None}
    summary = logic.summary_target(wiki, logic.parse_request({"operation": "wikipedia_summary", "title": "Ada Lovelace"}, wiki))
    assert summary["origin"] == "https://en.wikipedia.org" and params(summary)["titles"] == "Ada Lovelace"
    assert params(summary)["prop"] == "extracts|info|pageprops" and params(summary)["redirects"] == "1"
    assert params(summary)["exintro"] == "1" and params(summary)["explaintext"] == "1" and "generator" not in params(summary)
    found = params(logic.summary_target(wiki, logic.parse_request({"operation": "wikipedia_summary", "query": "x y"}, wiki)))
    assert found["generator"] == "search" and found["gsrsearch"] == "x y" and found["gsrlimit"] == "1" and "titles" not in found
    double = logic.configuration({**WIKI, "api_base_url": "http://127.0.0.1:9"})
    assert logic.origin_and_path(double, "de") == ("http://127.0.0.1:9", "/w/api.php")
    assert logic.USER_AGENT == "BloxSmith-WebSearch/0.1 (+https://github.com/HackInvent/bloxmith-web-search)"


def response(status, body=None, *, error=None, retry=None, reset=None):
    return {"status": "response", "http_status": status, "body": body, "body_error": error,
            "retry_after_header": retry, "rate_reset_header": reset}


def provider_contract():
    """# - FB4 - provider error envelopes map to stable codes; retries are bounded and never hide quota or key errors."""
    ok = {"type": "search"}
    assert logic.provider_result(response(200, ok), "brave") is ok
    brave_error = lambda status, code: response(status, {"type": "ErrorResponse", "error": {
        "id": "x", "status": status, "code": code, "detail": "Detail with " + TOKEN, "meta": {}}})
    for status, code, expected in ((422, "SUBSCRIPTION_TOKEN_INVALID", "unauthorized"), (429, "RATE_LIMITED", "rate_limited"),
                                   (429, "QUOTA_LIMITED", "quota_exceeded"), (403, "OPTION_NOT_IN_PLAN", "forbidden"),
                                   (422, "VALIDATION", "bad_request"), (500, "UNKNOWN", "provider_unavailable")):
        error = rejected(lambda status=status, code=code: logic.provider_result(brave_error(status, code), "brave", TOKEN), expected)
        assert error.provider_code == code and error.http_status == status and "***" in error.provider_message
    limited = rejected(lambda: logic.provider_result({**brave_error(429, "RATE_LIMITED"), "rate_reset_header": 1}, "brave"),
                       "rate_limited")
    assert limited.retry_after == 1
    serp = lambda status, text, **extra: response(status, {"error": text}, **extra)
    assert rejected(lambda: logic.provider_result(serp(401, "Invalid API key."), "serpapi"), "unauthorized").provider_message == "Invalid API key."
    assert rejected(lambda: logic.provider_result(serp(429, "Your account has run out of searches."), "serpapi"), "quota_exceeded")
    throughput = rejected(lambda: logic.provider_result(serp(429, "Hourly throughput limit.", retry=3), "serpapi"), "rate_limited")
    assert throughput.retry_after == 3
    assert rejected(lambda: logic.provider_result(serp(400, "Missing query `q` parameter."), "serpapi"), "bad_request")
    forbidden = rejected(lambda: logic.provider_result(response(403, None, error="not_json"), "searxng"), "forbidden")
    assert "search.formats" in str(forbidden) and forbidden.http_status == 403
    html = rejected(lambda: logic.provider_result(response(200, None, error="not_json"), "searxng"), "protocol")
    assert "json format" in str(html)
    assert "Wikimedia" in str(rejected(lambda: logic.provider_result(response(403, None, error="not_json"), "wikipedia"), "forbidden"))
    for status, code in ((302, "redirect"), (404, "not_found"), (402, "quota_exceeded"), (418, "provider_error"),
                         (503, "provider_unavailable"), (429, "rate_limited")):
        error = rejected(lambda status=status: logic.provider_result(response(status, None, error="not_json", retry=7), "brave"), code)
        assert error.retry_after == (7 if code == "rate_limited" else None)
    rejected(lambda: logic.provider_result(response(200, None, error="too_large"), "brave"), "protocol")
    rejected(lambda: logic.provider_result(response(200, None, error="encoded"), "serpapi"), "protocol")
    rejected(lambda: logic.provider_result(response(200, ["list"]), "brave"), "protocol")
    rejected(lambda: logic.provider_result(response(99, {}), "brave"), "protocol")
    wiki = lambda code: response(200, {"error": {"code": code, "info": "Info " + code}})
    assert rejected(lambda: logic.provider_result(wiki("badvalue"), "wikipedia"), "bad_request").provider_code == "badvalue"
    assert rejected(lambda: logic.provider_result(wiki("ratelimited"), "wikipedia"), "rate_limited")
    assert rejected(lambda: logic.provider_result(wiki("maxlag"), "wikipedia"), "provider_unavailable")
    config = logic.configuration({**BRAVE, "max_retries": 2, "max_retry_wait_sec": 10})
    error = lambda code, wait=None: logic.SearchError(code, retry_after=wait)
    assert logic.retry_delay(error("rate_limited", 3), 1, config, 30) == 3.0
    assert logic.retry_delay(error("rate_limited"), 1, config, 30) == 1.0
    assert logic.retry_delay(error("rate_limited", 11), 1, config, 30) is None
    assert logic.retry_delay(error("rate_limited", 5), 1, config, 5) is None, "never wait past the deadline"
    assert logic.retry_delay(error("provider_unavailable"), 2, config, 30) == 2.0
    assert logic.retry_delay(error("network"), 3, config, 30) is None, "max_retries reached"
    for code in ("quota_exceeded", "unauthorized", "forbidden", "bad_request", "not_found", "timeout", "tls", "destination",
                 "protocol", "cancelled"):
        assert logic.retry_delay(error(code), 1, config, 30) is None, code
    envelope = logic.SearchError("rate_limited", http_status=429, retry_after=2).envelope(operation="search", request_id=None)
    assert envelope == {"code": "rate_limited", "message": logic.MESSAGES["rate_limited"], "http_status": 429,
                        "retry_after": 2, "operation": "search"}


def normalize_contract():
    """# - FB2 - # - FB3 - provider results become {rank, title, url, snippet, source, site, published?} and summaries stay bounded."""
    config = logic.configuration({**BRAVE, "max_text_chars": 100})
    items, more, estimate = logic.brave_page({"query": {"more_results_available": True}, "web": {"results": [
        {"title": "<strong>Ada</strong> &amp; Babbage", "url": "https://example.com/a", "description": "x " * 80,
         "page_age": "2026-09-01T08:00:00"},
        {"title": "Unsafe", "url": "javascript:alert(1)", "description": "dropped"},
        {"title": "", "url": "http://EXAMPLE.org/b", "description": None, "page_age": "two days ago"}, "noise"]}}, config)
    assert more is True and estimate is None and len(items) == 2
    assert items[0] == {"title": "Ada & Babbage", "url": "https://example.com/a", "snippet": ("x " * 50).strip() + "…",
                        "source": "brave", "site": "example.com", "published": "2026-09-01T08:00:00"}
    assert items[1]["title"] == "example.org" and items[1]["snippet"] == "" and "published" not in items[1]
    assert logic.brave_page({"query": {}}, config) == ([], False, None)
    rejected(lambda: logic.brave_page({"web": {"results": "x"}}, config), "protocol")
    serp = logic.configuration({**BRAVE, "provider": "serpapi"})
    items, more, estimate = logic.serpapi_page({"organic_results": [{"title": "T", "link": "https://e.com", "snippet": "S",
                                                                     "date": "Mar 5, 2026"}],
                                                "serpapi_pagination": {"next": "https://serpapi.com/search.json?start=10"},
                                                "search_information": {"total_results": 42}}, serp)
    assert items[0]["published"] == "2026-03-05" and items[0]["source"] == "serpapi" and more and estimate == 42
    assert logic.serpapi_page({"search_metadata": {"status": "Success"}, "error": "Google hasn't returned any results for this query."},
                              serp) == ([], False, 0)
    failed = rejected(lambda: logic.serpapi_page({"search_metadata": {"status": "Error"}, "error": "Upstream failure"}, serp),
                      "provider_error")
    assert failed.provider_message == "Upstream failure" and failed.http_status == 200
    searx = logic.configuration(SEARX)
    items, _, estimate = logic.searxng_page({"number_of_results": 0, "results": [
        {"url": "https://e.com/1", "title": "One", "content": "C", "engines": ["bing", "duckduckgo"], "publishedDate": None},
        {"url": "https://e.com/2", "title": "Two", "content": "D", "engine": "qwant", "publishedDate": "2026-08-01T00:00:00"}]}, searx)
    assert estimate is None and items[0]["engines"] == ["bing", "duckduckgo"] and items[1]["engines"] == ["qwant"]
    assert items[1]["published"] == "2026-08-01T00:00:00" and "published" not in items[0]
    rejected(lambda: logic.searxng_page({"query": "x"}, searx), "protocol")
    wiki = logic.configuration(WIKI)
    items, more, estimate = logic.wikipedia_page({"continue": {"sroffset": 10}, "query": {"searchinfo": {"totalhits": 99}, "search": [
        {"title": "Ada Lovelace", "snippet": "<span class=\"searchmatch\">Ada</span> was"},
        {"title": "C (programming language)", "snippet": ""}]}}, wiki, "fr")
    assert more and estimate == 99 and items[0]["url"] == "https://fr.wikipedia.org/wiki/Ada_Lovelace"
    assert items[0]["snippet"] == "Ada was" and items[1]["url"] == "https://fr.wikipedia.org/wiki/C_(programming_language)"
    assert items[0]["site"] == "fr.wikipedia.org"
    rejected(lambda: logic.wikipedia_page({"batchcomplete": True}, wiki, "en"), "protocol")
    request = logic.parse_request({"operation": "wikipedia_summary", "title": "lovelace", "request_id": "s"}, wiki)
    body = {"query": {"normalized": [{"from": "lovelace", "to": "Lovelace"}], "redirects": [{"from": "Lovelace", "to": "Ada Lovelace"}],
                      "pages": [{"pageid": 974, "ns": 0, "title": "Ada Lovelace", "extract": "First.\n\nSecond paragraph.\u0007",
                                 "fullurl": "https://en.wikipedia.org/wiki/Ada_Lovelace"}]}}
    result = logic.summary(body, wiki, request)
    assert result["title"] == "Ada Lovelace" and result["page_id"] == 974 and result["extract"] == "First.\nSecond paragraph."
    assert result["redirected_from"] == "Lovelace" and result["disambiguation"] is False and result["truncated"] is False
    assert result["results"] == [{"rank": 1, "title": "Ada Lovelace", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
                                  "snippet": "First.\nSecond paragraph.", "source": "wikipedia", "site": "en.wikipedia.org"}]
    short = logic.summary({"query": {"pages": [{"pageid": 1, "title": "Long", "extract": "word " * 60,
                                                "pageprops": {"disambiguation": ""}, "fullurl": "javascript:x"}]}},
                          logic.configuration({**WIKI, "max_text_chars": 100}), request)
    assert short["truncated"] and len(short["extract"]) <= 101 and short["extract"].endswith("…") and short["disambiguation"]
    assert short["url"] == "https://en.wikipedia.org/wiki/Long"
    rejected(lambda: logic.summary({"batchcomplete": True}, wiki, request), "not_found")
    rejected(lambda: logic.summary({"query": {"pages": [{"ns": 0, "title": "Nope", "missing": True}]}}, wiki, request), "not_found")
    invalid = rejected(lambda: logic.summary({"query": {"pages": [{"title": "x", "invalid": True, "invalidreason": "Bad title"}]}},
                                             wiki, request), "bad_request")
    assert invalid.provider_message == "Bad title"
    ranked = logic.summary({"query": {"pages": [{"pageid": 2, "title": "Second", "index": 2},
                                                {"pageid": 1, "title": "First", "index": 1}]}}, wiki, request)
    assert ranked["title"] == "First"
    assert logic.published("2026-09-01") == "2026-09-01" and logic.published("1 Sep 2026") == "2026-09-01"
    assert logic.published("yesterday") is None and logic.published(5) is None
    assert logic.clean("<b>a</b>\n\t b &lt;c&gt;", 50) == "a b <c>" and logic.clean("", 5) is None
    assert logic.result_url("https://e.com/x y") is None and logic.result_url("ftp://e.com") is None
    assert logic.result_url("https://user@e.com") is None and logic.result_url("https://e.com:99999") is None


def block_contract():
    """# - FB1 - # - FB4 - # - FB5 - # - FB6 - block hooks fail before side effects and keep surfaces bounded."""
    block = WebSearchBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    assert [port.name for port in ports_in] == ["query"] and [port.name for port in ports_out] == ["results", "error"]
    assert block.model["config"] == logic.DEFAULTS and block.model["version"] == "0.1.0"
    assert block.model["runtime"]["active_execution_policy"] == "on_each_event"
    calls, secret = [], {"value": "not a key " + TOKEN}

    def resolver(ref):
        calls.append(ref)
        return secret["value"]

    def run(config, inputs):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs, input_ports=ports_in,
                                      output_ports=ports_out, services={"resolve_secret": resolver})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, {output.port_name: json.loads(output.value) for output in result.outputs}

    assert run(BRAVE, {})[0].status == "skipped"
    for config, inputs, code in (({**BRAVE, "enabled": False}, {"query": "ada"}, "disabled"),
                                 (BRAVE, {"query": '{"query":'}, "invalid_input"),
                                 (BRAVE, {"query": {"query": "a", "count": 99}}, "invalid_input"),
                                 (BRAVE, {"query": {"operation": "wikipedia_summary", "title": "Ada"}}, "invalid_input"),
                                 ({**BRAVE, "secret_ref": ""}, {"query": "ada"}, "config"),
                                 ({**WIKI, "api_base_url": "http://127.0.0.1:9", "timeout_sec": 5},
                                  {"query": {"query": "a", "time_range": "day"}}, "invalid_input")):
        result, outputs = run(config, inputs)
        assert set(outputs) == {"error"} and outputs["error"]["code"] == code, outputs
        assert result.metadata["web_search"]["code"] == code and result.metadata["web_search"]["status"] == "error"
    assert not calls, "The wallet must not be read before input and policy validation"
    result, outputs = run(BRAVE, {"query": {"query": "a", "count": 99, "request_id": "bad-1"}})
    assert outputs["error"]["request_id"] == "bad-1" and outputs["error"]["operation"] == "search", outputs
    assert result.logs == ["[web-search-error] brave search: invalid_input"] and not calls
    _, outputs = run(BRAVE, {"query": {"operation": "nope", "request_id": "bad id"}})
    assert "request_id" not in outputs["error"] and "operation" not in outputs["error"], outputs
    result, outputs = run(BRAVE, {"query": {"query": "private question", "request_id": "req-1"}})
    assert outputs["error"]["code"] == "secret" and outputs["error"]["request_id"] == "req-1" and calls == [REF]
    assert outputs["error"]["operation"] == "search" and outputs["error"]["provider"] == "brave"
    assert TOKEN not in json.dumps(outputs) and TOKEN not in " ".join(result.logs)
    assert result.logs == ["[web-search-error] brave search: secret"] and "private question" not in " ".join(result.logs)
    calls.clear()
    seen = []

    def offline(config, token, request, cancel):
        seen.append((config["provider"], token, request["query"]))
        return {"request_id": request["request_id"], "operation": "search", "count": 1, "attempts": 1,
                "results": [{"rank": 1, "title": "Echo " + TOKEN, "url": "https://example.com", "snippet": "",
                             "source": config["provider"], "site": "example.com"}]}

    with patch.object(transport, "run", offline):
        keyless, outputs = run(WIKI, {"query": "ada"})
        assert not calls and seen == [("wikipedia", "", "ada")], "Wikipedia never reads the wallet"
        assert outputs["results"]["count"] == 1 and keyless.logs == ["[web-search] wikipedia search returned 1 result(s) after 1 attempt(s)."]
        secret["value"] = TOKEN
        keyed, outputs = run(BRAVE, {"query": "ada"})
    assert calls == [REF] and seen[-1] == ("brave", TOKEN, "ada")
    assert outputs["results"]["results"][0]["title"] == "Echo ***", "the key is redacted from every output"
    assert keyed.metadata["web_search"] == {"status": "success", "operation": "search", "provider": "brave", "count": 1, "attempts": 1}

    def prepared(config, mode):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context)

    assert prepared(BRAVE, "zeromq_active").listen_on_run is False
    rejected(lambda: prepared({**BRAVE, "secret_ref": TOKEN}, "zeromq_active"), "config")
    node = block.build_node_payload(node_id="search", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings",
                                      values={"node_patch": {"config": {**BRAVE, "region": " fr", "language": "PT_br"}}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True and operation["config"]["provider"] == "brave"
    assert operation["config"]["region"] == "FR" and operation["config"]["language"] == "pt-br", "canonical values are saved"
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BRAVE, "secret_ref": TOKEN}}})
    assert "secret_ref" in refused["error"] and TOKEN not in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    for key in logic.DEFAULTS:
        assert f'data-block-config-field="{key}"' in html, key
    assert 'data-block-value-type="integer"' in html and 'data-block-value-type="boolean"' in html and "{{" not in html
    assert 'data-i18n="block.web_search.label_count"' in html and '<details class="owned-advanced">' in html
    assert '<option value="wikipedia" selected data-i18n="block.web_search.option_provider_wikipedia">Wikipedia</option>' in html
    assert 'data-i18n="block.web_search.option_time_range_any">Any time</option>' in html
    assert 'data-show-field="provider" data-show-value="[&quot;brave&quot;, &quot;serpapi&quot;]" hidden>' in html
    assert 'data-show-value="&quot;searxng&quot;" hidden>' in html
    shown = block.render_inspector_panel(node={**node, "config": {**node["config"], **SEARX}})["html"]
    assert 'data-show-value="&quot;searxng&quot;">' in shown and 'data-block-config-field="ca_file"' in shown
    cards = {name: block.render_node_card(node={**node, "config": {**node["config"], **config}})["html"]
             for name, config in (("off", {}), ("brave", BRAVE), ("searxng", SEARX), ("wikipedia", WIKI),
                                  ("serpapi", {**BRAVE, "provider": "serpapi"}))}
    assert 'data-i18n="block.web_search.card_disabled">Remote access off<' in cards["off"]
    assert 'data-i18n="block.web_search.card_brave">Enabled · Brave Search<' in cards["brave"]
    assert 'data-i18n="block.web_search.card_searxng">Enabled · SearXNG<' in cards["searxng"]
    assert 'data-i18n="block.web_search.card_wikipedia">Enabled · Wikipedia<' in cards["wikipedia"]
    assert 'data-i18n="block.web_search.card_serpapi">Enabled · SerpAPI (Google)<' in cards["serpapi"]
    assert REF not in cards["brave"] and "search.example.org" not in cards["searxng"]


def main():
    settings_contract()
    request_contract()
    target_contract()
    provider_contract()
    normalize_contract()
    block_contract()
    print("[ok] Web Search settings, requests, provider parameters, envelopes, normalization and hooks")


if __name__ == "__main__":
    main()
