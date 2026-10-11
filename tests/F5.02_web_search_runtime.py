"""FB1/FB2/FB3/FB4/FB5: the real network process against loopback provider doubles: protocols, pagination, errors, retries, cancellation.

Every provider exchange targets the loopback doubles in search_fixture.py; no
real provider, instance or key is contacted. Real graphs in both runtimes and
every package origin are in F5.03_web_search_graphs.py.
"""

import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.web_search import logic, transport
from search_fixture import SEARX_PREFIX, TOKEN, search_api, settings


def failure(call, code):
    try:
        call()
    except logic.SearchError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in json.dumps(exc.envelope())
        return exc
    raise AssertionError("Expected " + code)


class Tracking(transport.WorkerCall):
    """Record every owned network child so tests can prove it was reaped."""
    created = []

    def __init__(self, job):
        super().__init__(job)
        Tracking.created.append(self)


def calls(api, route):
    return [call for call in api.calls if call["route"] == route]


def search(config, document, token=TOKEN, cancel=lambda: False):
    config = logic.configuration(config)
    return transport.run(config, token if config["provider"] in logic.KEYED else "",
                         logic.parse_request(document, config), cancel)


def brave_protocol():
    """# - FB2 - # - FB4 - Brave: header key, offset pages, filters, ErrorResponse codes, 429 waits, retries, limits and TLS."""
    with search_api() as api:
        config = settings(api, "brave")
        result = search(config, {"query": "ada lovelace", "count": 5, "page": 2, "language": "fr", "region": "be",
                                 "safe_search": "strict", "time_range": "week", "request_id": "b-1"})
        call = calls(api, "brave")[-1]
        assert call["brave_token"] == TOKEN and call["query_key"] is None and call["accept_encoding"] == "identity"
        assert call["user_agent"] == logic.USER_AGENT and call["accept"] == "application/json"
        assert call["params"] == {"q": "ada lovelace", "count": "5", "offset": "1", "safesearch": "strict",
                                  "text_decorations": "false", "search_lang": "fr", "country": "BE", "freshness": "pw"}
        assert [item["rank"] for item in result["results"]] == [6, 7, 8, 9, 10] and result["request_id"] == "b-1"
        first = result["results"][0]
        assert first == {"rank": 6, "title": "ada lovelace result 6", "url": "https://example.com/ada-lovelace/6",
                         "snippet": "About ada lovelace, item 6 & more", "source": "brave", "site": "example.com",
                         "published": "2026-09-07T08:00:00"}
        assert result["has_more"] and result["next_page"] == 3 and result["attempts"] == 1 and result["requests"] == 1
        assert result["filters"] == {"language": "fr", "region": "BE", "safe_search": "strict", "time_range": "week"}
        last = search(config, {"query": "ada lovelace", "count": 5, "page": 9})
        assert [item["rank"] for item in last["results"]] == [41, 42, 43, 44, 45] and not last["has_more"] and last["next_page"] is None
        empty = search(config, "nothing")
        assert empty["count"] == 0 and empty["results"] == [] and not empty["has_more"]
        denied = failure(lambda: search(config, "ada", token="-".join(("wrong", "fixture", "key"))), "unauthorized")
        assert denied.provider_code == "SUBSCRIPTION_TOKEN_INVALID" and denied.http_status == 422 and denied.attempts == 1
        # HTTP 429 honours X-RateLimit-Reset or Retry-After within bounds; quota exhaustion is never retried.
        api.script["brave"] = [{"status": 429, "body": {"type": "ErrorResponse", "error": {"status": 429, "code": "RATE_LIMITED",
                                                                                           "detail": "Request rate limit exceeded."}},
                                "headers": {"X-RateLimit-Reset": "1, 1419704"}}]
        started, before = time.monotonic(), len(api.calls)
        assert search(config, "ada")["attempts"] == 2 and time.monotonic() - started >= 1 and len(api.calls) == before + 2
        api.script["brave"] = [{"status": 429, "body": {"type": "ErrorResponse", "error": {"code": "RATE_LIMITED"}},
                                "headers": {"Retry-After": "90"}}]
        too_long = failure(lambda: search(config, "ada"), "rate_limited")
        assert too_long.retry_after == 90 and too_long.attempts == 1
        api.script["brave"] = [{"status": 429, "body": {"type": "ErrorResponse", "error": {"code": "QUOTA_LIMITED",
                                                                                           "detail": "Monthly quota exhausted."}}}]
        before = len(api.calls)
        quota = failure(lambda: search(config, "ada"), "quota_exceeded")
        assert quota.attempts == 1 and len(api.calls) == before + 1 and quota.provider_message == "Monthly quota exhausted."
        api.script["brave"] = ["html502", "lost"]
        assert search({**config, "max_retries": 2}, "ada")["attempts"] == 3
        api.script["brave"] = ["html502"]
        unavailable = failure(lambda: search({**config, "max_retries": 0}, "ada"), "provider_unavailable")
        assert unavailable.http_status == 502 and unavailable.attempts == 1
        api.script["brave"] = ["oversized"]
        failure(lambda: search({**config, "max_response_bytes": 65536}, "ada"), "protocol")
        api.script["brave"] = ["gzip"]
        failure(lambda: search(config, "ada"), "protocol")
        api.script["brave"] = [{"status": 301, "body": b"", "content_type": "text/html", "headers": {"Location": "https://x.example"}}]
        assert "never followed" in str(failure(lambda: search(config, "ada"), "redirect"))
    with search_api(tls=True) as api:
        config = settings(api, "brave")
        assert search(config, {"query": "secure", "count": 2})["count"] == 2 and calls(api, "brave")[-1]["brave_token"] == TOKEN
        before = len(api.calls)
        failure(lambda: search({**config, "ca_file": ""}, "secure"), "tls")
        assert len(api.calls) == before, "an unverified server never receives the key"
    print("[ok] Brave Search protocol, errors, retries and TLS", flush=True)


def serpapi_protocol():
    """# - FB2 - # - FB4 - SerpAPI: key in the query only, start/num, Google filters, empty answers, 401 and quota."""
    with search_api() as api:
        config = settings(api, "serpapi")
        result = search(config, {"query": "ada", "count": 4, "page": 3, "language": "fr", "region": "ca",
                                 "safe_search": "off", "time_range": "day"})
        call = calls(api, "serpapi")[-1]
        assert call["query_key"] == TOKEN and call["brave_token"] is None and not call["duplicate_params"]
        assert call["params"] == {"engine": "google", "q": "ada", "num": "4", "start": "8", "hl": "fr", "gl": "ca",
                                  "safe": "off", "tbs": "qdr:d"}
        assert [item["rank"] for item in result["results"]] == [9, 10, 11, 12] and result["total_estimate"] == 12300
        assert result["results"][0]["published"] == "2026-03-10" and "published" not in result["results"][1]
        assert result["has_more"] and result["filters"]["safe_search"] == "off"
        first = search(config, "ada")
        assert "start" not in calls(api, "serpapi")[-1]["params"] and first["count"] == 10 and first["results"][0]["rank"] == 1
        assert "safe" not in calls(api, "serpapi")[-1]["params"] and first["filters"]["safe_search"] is None
        tail = search(config, {"query": "ada", "count": 20, "page": 3})
        assert tail["count"] == 5 and not tail["has_more"]
        empty = search(config, "nothing")
        assert empty["count"] == 0 and empty["total_estimate"] == 0 and not empty["has_more"]
        denied = failure(lambda: search(config, "ada", token="-".join(("wrong", "fixture", "key"))), "unauthorized")
        assert denied.http_status == 401 and "Invalid API key" in denied.provider_message
        api.script["serpapi"] = [{"status": 429, "body": {"error": "Your account has run out of searches."}}]
        before = len(api.calls)
        assert failure(lambda: search(config, "ada"), "quota_exceeded").attempts == 1 and len(api.calls) == before + 1
        api.script["serpapi"] = [{"status": 200, "body": {"search_metadata": {"status": "Error"}, "error": "Google blocked."}}]
        failure(lambda: search(config, "ada"), "provider_error")
    print("[ok] SerpAPI protocol, empty answers and errors", flush=True)


def searxng_protocol():
    """# - FB1 - # - FB2 - # - FB4 - SearXNG: path prefix, page collection with de-duplication, bounds, 403 format, 404 path and policy."""
    with search_api() as api:
        config = settings(api, "searxng")
        result = search(config, {"query": "ada", "count": 20, "language": "fr", "region": "ca", "safe_search": "strict",
                                 "time_range": "month"})
        pages = calls(api, "searxng")
        assert [call["params"]["pageno"] for call in pages] == ["1", "2", "3"] and pages[0]["path"] == SEARX_PREFIX + "/search"
        assert pages[0]["params"] == {"q": "ada", "format": "json", "pageno": "1", "safesearch": "2", "language": "fr-CA",
                                      "time_range": "month"}
        assert [item["url"].rsplit("/", 1)[1] for item in result["results"]] == [str(index) for index in range(1, 21)]
        assert [item["rank"] for item in result["results"]] == list(range(1, 21)) and result["requests"] == 3
        assert result["has_more"] and not result["limited"] and result["results"][2]["engines"] == ["duckduckgo", "wikipedia"]
        assert result["results"][0]["published"] == "2026-08-02T00:00:00" and result["total_estimate"] is None
        default_bound = search(config, {"query": "ada", "count": 10, "page": 3})
        assert default_bound["count"] == 4 and default_bound["limited"] and default_bound["requests"] == 3, "max_pages defaults to 3"
        config["max_pages"] = 10
        second = search(config, {"query": "ada", "count": 10, "page": 3})
        assert [item["rank"] for item in second["results"]] == list(range(21, 31)) and second["requests"] == 4
        assert second["has_more"], "a full last page is reported as possibly continued (estimate)"
        beyond = search(config, {"query": "ada", "count": 10, "page": 4})
        assert beyond["count"] == 0 and not beyond["has_more"] and beyond["requests"] == 5, "an empty page ends the collection"
        limited = search({**config, "max_pages": 2}, {"query": "ada", "count": 20})
        assert limited["count"] == 16 and limited["limited"] and not limited["has_more"] and limited["requests"] == 2
        assert search(config, "nothing")["count"] == 0
        api.searx_json = False
        disabled = failure(lambda: search(config, "ada"), "forbidden")
        assert disabled.http_status == 403 and "search.formats" in str(disabled) and disabled.attempts == 1
        api.searx_json = True
        # A wrong instance path prefix reaches no SearXNG route: the HTTP 404 page is not_found and never retried.
        before = len(api.calls)
        missing = failure(lambda: search({**config, "searxng_url": api.origin + "/wrong-prefix/"}, "ada"), "not_found")
        assert missing.http_status == 404 and missing.attempts == 1 and len(api.calls) == before + 1
        assert api.calls[-1]["route"] == "unknown" and api.calls[-1]["path"] == "/wrong-prefix/search"
        assert "not found" in str(missing) and missing.envelope()["http_status"] == 404
        api.script["searxng"] = ["html200"]
        assert "json format" in str(failure(lambda: search(config, "ada"), "protocol"))
        api.script["searxng"] = [{"status": 429, "body": b"Too Many Requests", "content_type": "text/plain"}]
        limited_rate = failure(lambda: search({**config, "max_retries": 0}, "ada"), "rate_limited")
        assert limited_rate.retry_after is None
        # Without the private-network opt-in, the worker refuses the loopback address before sending anything.
        before = len(api.calls)
        public = logic.configuration(config)
        public["allow_private_network"] = False
        assert logic.address_policy(public) == "public"
        blocked = failure(lambda: transport.run(public, "", logic.parse_request("ada", public)), "destination")
        assert blocked.attempts == 1 and len(api.calls) == before
    with search_api(tls=True) as api:
        config = settings(api, "searxng")
        assert config["searxng_url"].startswith("https://") and search(config, {"query": "tls", "count": 3})["count"] == 3
    print("[ok] SearXNG pages, de-duplication, limits, disabled JSON format and address policy", flush=True)


def wikipedia_protocol():
    """# - FB2 - # - FB3 - # - FB4 - MediaWiki: search continuation, summaries by title, link or search, and error envelopes."""
    with search_api() as api:
        config = settings(api, "wikipedia")
        result = search(config, {"query": "ada", "count": 2, "page": 2, "request_id": "w-1"})
        call = calls(api, "wikipedia")[-1]
        assert call["params"]["srlimit"] == "2" and call["params"]["sroffset"] == "2" and call["brave_token"] is None
        assert call["query_key"] is None and call["user_agent"] == logic.USER_AGENT
        assert [item["title"] for item in result["results"]] == ["Mercury", "ada topic 1"]
        assert result["results"][0] == {"rank": 3, "title": "Mercury", "url": "https://en.wikipedia.org/wiki/Mercury",
                                        "snippet": "ada in Mercury & co", "source": "wikipedia", "site": "en.wikipedia.org"}
        assert result["has_more"] and result["total_estimate"] == 25 and result["filters"]["language"] == "en"
        french = search(config, {"query": "ada", "count": 1, "language": "fr"})
        assert french["results"][0]["url"] == "https://fr.wikipedia.org/wiki/Ada_Lovelace"
        end = search(config, {"query": "ada", "count": 5, "page": 5})
        assert end["count"] == 5 and not end["has_more"] and end["results"][-1]["rank"] == 25
        assert search(config, "nothing")["count"] == 0
        summary = search(config, {"operation": "wikipedia_summary", "title": "lovelace", "request_id": "s-1"})
        assert summary["title"] == "Ada Lovelace" and summary["redirected_from"] == "Lovelace" and summary["page_id"] == 974
        assert summary["url"] == "https://en.wikipedia.org/wiki/Ada_Lovelace" and summary["request_id"] == "s-1"
        assert summary["extract"].startswith("Augusta Ada King") and summary["results"][0]["snippet"] == summary["extract"]
        assert calls(api, "wikipedia")[-1]["params"]["titles"] == "lovelace"
        found = search(config, {"operation": "wikipedia_summary", "query": "first programmer"})
        assert found["title"] == "Ada Lovelace" and calls(api, "wikipedia")[-1]["params"]["gsrsearch"] == "first programmer"
        linked = search(config, {"operation": "wikipedia_summary", "title": "https://en.wikipedia.org/wiki/Mercury"})
        assert linked["disambiguation"] and linked["redirected_from"] is None
        short = search({**config, "max_text_chars": 100}, {"operation": "wikipedia_summary", "title": "Ada Lovelace"})
        assert short["truncated"] and len(short["extract"]) <= 101
        failure(lambda: search(config, {"operation": "wikipedia_summary", "title": "Missing page"}), "not_found")
        failure(lambda: search(config, {"operation": "wikipedia_summary", "query": "nothing"}), "not_found")
        api.script["wikipedia"] = [{"status": 200, "body": {"error": {"code": "maxlag", "info": "Waiting for a database server."}}}]
        assert search(config, "ada")["attempts"] == 2
        api.script["wikipedia"] = [{"status": 200, "body": {"error": {"code": "badvalue", "info": "Unrecognized value."}}}]
        bad = failure(lambda: search(config, "ada"), "bad_request")
        assert bad.provider_code == "badvalue" and bad.attempts == 1
        api.script["wikipedia"] = [{"status": 429, "body": b"<html>Too many requests</html>", "content_type": "text/html",
                                    "headers": {"Retry-After": "1"}}]
        assert search(config, "ada")["attempts"] == 2
        api.script["wikipedia"] = [{"status": 403, "body": b"<html>Forbidden</html>", "content_type": "text/html"}]
        assert "Wikimedia" in str(failure(lambda: search(config, "ada"), "forbidden"))
    print("[ok] Wikipedia search and summaries", flush=True)


def cancellation_protocol():
    """# - FB4 - # - FB5 - cancellation and deadlines terminate the owned network child; closed ports are retried in bounds."""
    with search_api() as api:
        config = settings(api, "brave")
        Tracking.created.clear()
        with patch.object(transport, "WorkerCall", Tracking):
            api.script["brave"] = ["hold"]
            api.release.clear()
            started = time.monotonic()
            cancelled = failure(lambda: search(config, "held", cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
            assert time.monotonic() - started < 2.5 and cancelled.attempts == 1
            api.release.set()
            api.script["wikipedia"] = ["hold"]
            api.release.clear()
            started = time.monotonic()
            failure(lambda: search(settings(api, "wikipedia", timeout_sec=5), "slow"), "timeout")
            assert 4 <= time.monotonic() - started < 8
            api.release.set()
        assert Tracking.created and all(item.child.poll() is not None for item in Tracking.created)
        before = len(api.calls)
        closed = {**settings(api, "brave"), "api_base_url": "http://127.0.0.1:9", "max_retries": 1}
        refused = failure(lambda: search(closed, "x"), "network")
        assert refused.attempts == 2 and len(api.calls) == before
    print("[ok] Cancellation, deadlines and bounded network retries", flush=True)


def main():
    brave_protocol()
    serpapi_protocol()
    searxng_protocol()
    wikipedia_protocol()
    cancellation_protocol()


if __name__ == "__main__":
    main()
