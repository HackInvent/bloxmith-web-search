"""Loopback-only provider doubles. No real provider, instance, account or API key is contacted.

One server on a dynamic loopback port reproduces the documented request and
response shapes the block uses:

- Brave Search API ``GET /res/v1/web/search``: ``X-Subscription-Token``, ``count``
  (max 20), ``offset`` (max 9), ``search_lang``, ``country``, ``safesearch``,
  ``freshness``, ``text_decorations``; ``web.results[]`` and
  ``query.more_results_available``; the ``ErrorResponse`` envelope with
  ``SUBSCRIPTION_TOKEN_INVALID``, ``VALIDATION``, ``RATE_LIMITED`` (HTTP 429 with
  ``X-RateLimit-Reset``) and ``QUOTA_LIMITED``.
- SerpAPI ``GET /search?engine=google&api_key=``: ``organic_results[]``,
  ``serpapi_pagination.next``, ``search_information.total_results``; ``{"error"}``
  with HTTP 401 (invalid key) and 429 (account out of searches), and the HTTP 200
  "hasn't returned any results" answer.
- SearXNG ``GET /searx/search?format=json&pageno=``: ``results[]`` with ``engines``
  and ``publishedDate``, ``number_of_results``; HTTP 403 HTML when the JSON format
  is not enabled.
- MediaWiki Action API ``GET /w/api.php``: ``list=search`` with ``sroffset``
  continuation and ``totalhits``; ``prop=extracts|info|pageprops`` by ``titles``
  (``normalized``, ``redirects``, ``missing``, disambiguation page props) or by
  ``generator=search``; the ``{"error": {"code", "info"}}`` envelope.
- Any other path answers HTTP 404 with an HTML page, as a server does for a
  SearXNG URL with the wrong path prefix.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlsplit

# Built at run time so no literal in the sources resembles a provider credential.
TOKEN = "-".join(("fixture", "search", "key", "not", "real", "0123456789"))
REF = "secret://workspace/search_fixture"
SEARX_PREFIX = "/searx"
SEARX_PAGE = 8
SEARX_TOTAL = 30
WEB_TOTAL = 45
ARTICLES = {
    "Ada Lovelace": {"pageid": 974, "extract": ("Augusta Ada King, Countess of Lovelace was an English mathematician "
                                                "and writer.\n\nShe is often regarded as the first computer programmer.")},
    "Mercury": {"pageid": 19694, "extract": "Mercury usually refers to:\nMercury (planet)\nMercury (element)",
                "pageprops": {"disambiguation": ""}},
    "Analytical Engine": {"pageid": 1271, "extract": "The Analytical Engine was a proposed mechanical general-purpose computer."},
}
REDIRECTS = {"Lovelace": "Ada Lovelace", "Ada lovelace": "Ada Lovelace"}
SEARCH_TITLES = ["Ada Lovelace", "Analytical Engine", "Mercury"]


def settings(api, provider, **extra):
    """Block settings aimed at this fixture only."""
    config = {"enabled": True, "provider": provider, "timeout_sec": 15, "ca_file": api.ca_file}
    if provider == "searxng":
        config.update(searxng_url=api.origin + SEARX_PREFIX, allow_private_network=True,
                      allow_insecure_http=api.origin.startswith("http://"))
    else:
        config["api_base_url"] = api.origin
    if provider in ("brave", "serpapi"):
        config["secret_ref"] = REF
    return {**config, **extra}


def web_corpus(query, total=WEB_TOTAL):
    """Deterministic web results; the query 'nothing' has none."""
    if query == "nothing":
        return []
    slug = quote(query.replace(" ", "-").lower(), safe="-")
    return [{"title": f"{query} result {index}", "url": f"https://example.com/{slug}/{index}",
             "text": f"About {query}, item {index} & more", "date": index}
            for index in range(1, total + 1)]


@contextmanager
def search_api(tls=False):
    """Run the doubles on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        api = SimpleNamespace(calls=[], script={}, release=threading.Event(), searx_json=True, wiki_language="en")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def send(self, status, payload, headers=None, content_type="application/json; charset=utf-8"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def scripted(self, route):
                queue = api.script.get(route) or []
                if not queue:
                    return False
                entry = queue.pop(0)
                if entry == "hold":
                    api.release.wait(30)
                    return False
                if entry == "lost":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return True
                if entry == "html502":
                    self.send(502, b"<html>Bad gateway</html>", content_type="text/html")
                elif entry == "html200":
                    self.send(200, b"<html><body>results</body></html>", content_type="text/html")
                elif entry == "oversized":
                    self.send(200, {"padding": "x" * 200000, "results": []})
                elif entry == "gzip":
                    self.send(200, b"\x1f\x8b", headers={"Content-Encoding": "gzip"})
                else:
                    self.send(entry.get("status", 200), entry.get("body", {}), entry.get("headers"),
                              entry.get("content_type", "application/json"))
                return True

            def do_GET(self):
                parsed = urlsplit(self.path)
                pairs = parse_qsl(parsed.query, keep_blank_values=True)
                params = dict(pairs)
                routes = {"/res/v1/web/search": "brave", "/search": "serpapi", SEARX_PREFIX + "/search": "searxng",
                          "/w/api.php": "wikipedia"}
                route = routes.get(parsed.path, "unknown")
                key = params.pop("api_key", None)
                call = {"route": route, "path": parsed.path, "params": params, "duplicate_params": len(pairs) != len(dict(pairs)),
                        "user_agent": self.headers.get("User-Agent"), "accept_encoding": self.headers.get("Accept-Encoding"),
                        "accept": self.headers.get("Accept"), "brave_token": self.headers.get("X-Subscription-Token"),
                        "query_key": key}
                api.calls.append(call)
                if self.scripted(route):
                    return
                getattr(self, route, self.unknown)(params, call)

            def unknown(self, _params, _call):
                self.send(404, b"<html>Not Found</html>", content_type="text/html")

            def brave_error(self, status, code, detail, headers=None):
                self.send(status, {"type": "ErrorResponse", "error": {"id": "fixture-error", "status": status, "code": code,
                                                                       "detail": detail, "meta": {}}, "time": 1760000000},
                          headers)

            def brave(self, params, call):
                if call["brave_token"] != TOKEN:
                    self.brave_error(422, "SUBSCRIPTION_TOKEN_INVALID", "The provided subscription token is invalid.")
                    return
                count, offset = int(params.get("count", "20")), int(params.get("offset", "0"))
                if not 1 <= count <= 20 or not 0 <= offset <= 9 or not params.get("q"):
                    self.brave_error(422, "VALIDATION", "Unable to validate request parameter(s).")
                    return
                items = web_corpus(params["q"])
                window = items[offset * count:(offset + 1) * count]
                decorate = params.get("text_decorations") != "false"
                results = [{"type": "search_result", "title": entry["title"], "url": entry["url"],
                            "description": ("<strong>" + entry["text"] + "</strong>") if decorate else entry["text"],
                            "page_age": "2026-09-%02dT08:00:00" % (entry["date"] % 28 + 1), "age": "September 2026",
                            "profile": {"name": "Example"}, "meta_url": {"hostname": "example.com"}, "language": "en",
                            "family_friendly": True} for entry in window]
                body = {"type": "search", "query": {"original": params["q"], "altered": None,
                                                    "more_results_available": (offset + 1) * count < len(items)},
                        "mixed": {"type": "mixed", "main": []}}
                if results:
                    body["web"] = {"type": "search", "results": results, "family_friendly": True}
                self.send(200, body, {"X-RateLimit-Limit": "1, 2000", "X-RateLimit-Remaining": "0, 1999",
                                      "X-RateLimit-Reset": "1, 1419704"})

            def serpapi(self, params, call):
                if call["query_key"] != TOKEN:
                    self.send(401, {"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"})
                    return
                if params.get("engine") != "google" or not params.get("q"):
                    self.send(400, {"error": "Missing query `q` parameter."})
                    return
                items = web_corpus(params["q"])
                metadata = {"id": "fixture", "status": "Success", "json_endpoint": "https://serpapi.com/searches/fixture.json"}
                if not items:
                    self.send(200, {"search_metadata": metadata, "search_parameters": {"engine": "google", "q": params["q"]},
                                    "error": "Google hasn't returned any results for this query."})
                    return
                start, num = int(params.get("start", "0")), int(params.get("num", "10"))
                window = items[start:start + num]
                organic = [{"position": start + index + 1, "title": entry["title"], "link": entry["url"],
                            "displayed_link": "example.com", "snippet": entry["text"], "source": "Example",
                            **({"date": "Mar %d, 2026" % (entry["date"] % 28 + 1)} if entry["date"] % 2 else {})}
                           for index, entry in enumerate(window)]
                body = {"search_metadata": metadata, "search_parameters": {"engine": "google", "q": params["q"]},
                        "search_information": {"total_results": 12300, "time_taken_displayed": 0.31},
                        "organic_results": organic, "serpapi_pagination": {"current": start // 10 + 1}}
                if start + num < len(items):
                    body["serpapi_pagination"]["next"] = f"https://serpapi.com/search.json?engine=google&start={start + num}"
                self.send(200, body)

            def searxng(self, params, _call):
                if params.get("format") != "json" or not api.searx_json:
                    self.send(403, b"<html><body>403 Forbidden</body></html>", content_type="text/html; charset=utf-8")
                    return
                items = web_corpus(params.get("q", ""), SEARX_TOTAL)
                page = int(params.get("pageno", "1"))
                begin = (page - 1) * SEARX_PAGE
                window = items[max(0, begin - 1):begin + SEARX_PAGE] if page > 1 else items[:SEARX_PAGE]
                results = [{"url": entry["url"], "title": entry["title"], "content": entry["text"], "engine": "duckduckgo",
                            "engines": ["duckduckgo", "wikipedia"] if entry["date"] % 3 == 0 else ["duckduckgo"],
                            "score": 1.0, "category": "general", "parsed_url": ["https", "example.com", "/", "", "", ""],
                            "publishedDate": "2026-08-%02dT00:00:00" % (entry["date"] % 28 + 1) if entry["date"] % 2 else None,
                            "positions": [1]} for entry in window]
                self.send(200, {"query": params.get("q"), "number_of_results": 0, "results": results, "answers": [],
                                "corrections": [], "infoboxes": [], "suggestions": ["fixture suggestion"],
                                "unresponsive_engines": []})

            def wiki_error(self, code, info):
                self.send(200, {"error": {"code": code, "info": info, "docref": "See https://en.wikipedia.org/w/api.php."},
                                "servedby": "fixture"})

            def wikipedia(self, params, _call):
                if params.get("action") != "query" or params.get("format") != "json" or params.get("formatversion") != "2":
                    self.wiki_error("badvalue", "Unrecognized value for parameter \"action\".")
                    return
                if params.get("list") == "search":
                    self.wiki_search(params)
                elif params.get("prop") == "extracts|info|pageprops":
                    self.wiki_pages(params)
                else:
                    self.wiki_error("badvalue", "Unsupported request.")

            def wiki_search(self, params):
                query = params.get("srsearch", "")
                if not query:
                    self.wiki_error("missingparam", "The \"srsearch\" parameter must be set.")
                    return
                limit, offset = int(params.get("srlimit", "10")), int(params.get("sroffset", "0"))
                if not 1 <= limit <= 500:
                    self.wiki_error("badvalue", "Invalid value for srlimit.")
                    return
                titles = [] if query == "nothing" else SEARCH_TITLES + [f"{query} topic {index}" for index in range(1, 23)]
                window = titles[offset:offset + limit]
                body = {"batchcomplete": True,
                        "query": {"searchinfo": {"totalhits": len(titles)},
                                  "search": [{"ns": 0, "title": title, "pageid": 1000 + offset + index, "size": 1200,
                                              "wordcount": 180, "snippet": f"<span class=\"searchmatch\">{query}</span> in {title} &amp; co",
                                              "timestamp": "2026-09-01T10:00:00Z"} for index, title in enumerate(window)]}}
                if offset + limit < len(titles):
                    body["continue"] = {"sroffset": offset + limit, "continue": "-||"}
                self.send(200, body)

            def page(self, title, index=None):
                url = f"https://{api.wiki_language}.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
                if title not in ARTICLES:
                    return {"ns": 0, "title": title, "missing": True}
                article = ARTICLES[title]
                page = {"pageid": article["pageid"], "ns": 0, "title": title, "extract": article["extract"],
                        "contentmodel": "wikitext", "pagelanguage": api.wiki_language, "touched": "2026-09-01T10:00:00Z",
                        "lastrevid": 1, "length": 9000, "fullurl": url, "editurl": url + "?action=edit", "canonicalurl": url}
                if "pageprops" in article:
                    page["pageprops"] = article["pageprops"]
                if index is not None:
                    page["index"] = index
                return page

            def wiki_pages(self, params):
                if params.get("exintro") != "1" or params.get("explaintext") != "1" or params.get("inprop") != "url":
                    self.wiki_error("badvalue", "Unexpected extract parameters.")
                    return
                if params.get("generator") == "search":
                    query = params.get("gsrsearch", "")
                    if query == "nothing":
                        self.send(200, {"batchcomplete": True})
                        return
                    best = "Ada Lovelace" if "programmer" in query or "Ada" in query else "Analytical Engine"
                    self.send(200, {"batchcomplete": True, "query": {"pages": [self.page(best, index=1)]}})
                    return
                title = params.get("titles", "")
                query = {}
                normalized = title[:1].upper() + title[1:]
                if normalized != title:
                    query["normalized"] = [{"fromencoded": False, "from": title, "to": normalized}]
                if normalized in REDIRECTS:
                    query["redirects"] = [{"from": normalized, "to": REDIRECTS[normalized]}]
                    normalized = REDIRECTS[normalized]
                query["pages"] = [self.page(normalized)]
                self.send(200, {"batchcomplete": True, "query": query})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        # A worker killed during a held request leaves a broken pipe; that is expected, not a fixture failure.
        server.handle_error = lambda _request, _address: None
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.origin = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        try:
            yield api
        finally:
            api.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
