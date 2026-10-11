# Web Search

Search the web with SearXNG, Brave or SerpAPI, and Wikipedia. Each node sends a
query to one provider: your own SearXNG instance, the Brave Search API, SerpAPI
(Google results), or Wikipedia. Results come back as one ranked list with the same
fields for every provider (rank, title, URL, snippet, source). The block can also
return the introduction of a Wikipedia article. It never opens result pages; chain
[Web Extract](https://github.com/HackInvent/bloxmith-web-extract) for that. Remote
access stays off until you enable it, and API keys stay in the BloxSmith wallet.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Web Search — search the web and Wikipedia](media/thumbnail.webp)](media/cover.png)

## When to use it

- Give an LLM or an agent flow fresh web results to ground an answer.
- Watch the news on a topic: search with a `week` time range on a schedule.
- Collect candidate links, then read the pages with Web Extract.
- Look up a person, place or concept and get the Wikipedia introduction and link.
- Search privately through a SearXNG instance on your own network.

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `query` | Plain-text search terms, or one JSON request object (see below) |
| Output | `results` | Ranked, normalized results, or a Wikipedia summary |
| Output | `error` | Machine code and English message for any failed search |

Each fresh query triggers exactly one search. A retained value is never searched
again, so a replayed or forced activation does not repeat a paid request.
Failures are published on `error` and the run continues; they never stop an Active
Runtime worker.

## Requests

Plain text is a `search` with the node's defaults: `ada lovelace computer`. A text
that starts with `{` must be one JSON object; unknown fields are refused, so typos
are reported instead of ignored. `request_id` (1–128 of `A-Za-z0-9._:-`) is echoed in
every output.

| `operation` | Fields |
| --- | --- |
| `search` (default) | `query` (required), `count` 1–20, `page` 1–10, `language`, `region`, `safe_search` (`off`, `moderate`, `strict`), `time_range` (`""`, `day`, `week`, `month`, `year`) |
| `wikipedia_summary` | exactly one of `title` (a page title or a `https://<language>.wikipedia.org/wiki/<Title>` link) and `query` (search terms; the best match is summarized), optional `language` |

Request fields override the node settings for that query only. `page` counts in
pages of `count` results: `{"count": 5, "page": 2}` returns results 6 to 10.
Whitespace and line breaks in a query are collapsed. A query is limited to 400
characters (300 for Wikipedia) and, for Brave, 50 words.

`wikipedia_summary` needs `provider = wikipedia`: each node contacts only its one
configured provider. A summary link fixes the wiki language (`https://fr.wikipedia.org/wiki/...`
uses the French Wikipedia); a conflicting `language` is refused.

### Examples

```text
open source vector databases
```

```json
{"query": "BloxSmith release notes", "count": 5, "time_range": "month", "request_id": "news-1"}
{"query": "boulangerie", "language": "fr", "region": "FR", "page": 2}
{"operation": "wikipedia_summary", "title": "Ada Lovelace"}
{"operation": "wikipedia_summary", "query": "first computer programmer", "language": "en"}
{"operation": "wikipedia_summary", "title": "https://de.wikipedia.org/wiki/K%C3%B6ln"}
```

## Outputs

### results (search)

```json
{
  "request_id": "news-1", "operation": "search", "provider": "brave", "query": "BloxSmith release notes",
  "page": 1, "requested": 5, "count": 2, "has_more": false, "next_page": null, "limited": false,
  "total_estimate": null, "requests": 1, "attempts": 1,
  "filters": {"language": null, "region": null, "safe_search": "moderate", "time_range": "month"},
  "results": [
    {"rank": 1, "title": "Release notes", "url": "https://example.com/releases", "snippet": "What changed in…",
     "source": "brave", "site": "example.com", "published": "2026-09-30T08:00:00"},
    {"rank": 2, "title": "Changelog", "url": "https://example.org/changelog", "snippet": "…",
     "source": "brave", "site": "example.org"}
  ]
}
```

- `rank` is absolute: page 2 of 5 results starts at rank 6.
- `source` is the provider that returned the result; `site` is the host of `url`.
- `published` is present only when the provider gives a date: Brave `page_age`,
  SearXNG `publishedDate` (ISO 8601 as given) or a SerpAPI `date` such as
  `Mar 5, 2026` (converted to `2026-03-05`). Relative dates ("3 days ago") are dropped.
- SearXNG results also carry `engines`, the upstream engines that found them.
- Titles and snippets are plain text: markup is removed and entities decoded.
  Snippets are cut at a word boundary after `max_text_chars`. Results without an
  absolute `http(s)` link are dropped, and duplicate URLs are removed.
- `filters` shows what was actually sent to the provider (`null`: not sent, the
  provider's default applies).
- `has_more`/`next_page` say whether another page is likely; `total_estimate` is
  the provider's own estimate when it gives one (SerpAPI, Wikipedia, SearXNG).
- `requests` counts provider requests, `attempts` includes retries.

### results (wikipedia_summary)

```json
{
  "request_id": null, "operation": "wikipedia_summary", "provider": "wikipedia", "language": "en",
  "query": null, "requested_title": "Lovelace", "title": "Ada Lovelace", "page_id": 974,
  "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
  "extract": "Augusta Ada King, Countess of Lovelace was an English mathematician…",
  "truncated": false, "disambiguation": false, "redirected_from": "Lovelace", "count": 1,
  "results": [{"rank": 1, "title": "Ada Lovelace", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
               "snippet": "Augusta Ada King…", "source": "wikipedia", "site": "en.wikipedia.org"}],
  "requests": 1, "attempts": 1
}
```

`extract` is the plain-text introduction (paragraphs separated by line breaks), cut
at a word boundary after `max_text_chars` (`truncated: true`). Redirects are
followed by the MediaWiki API; `redirected_from` names the requested title. A
`disambiguation: true` page lists meanings rather than describing one subject.
The same summary is repeated as a one-item `results` list, so downstream blocks can
handle both operations alike.

### error

`{"code", "message", "operation", "provider", "request_id"}` and, when known,
`http_status`, `retry_after`, `provider_code` (Brave or MediaWiki error code),
`provider_message` (the provider's explanation, at most 500 characters) and
`attempts`.

| Code | Meaning |
| --- | --- |
| `config`, `disabled`, `secret`, `invalid_input` | Refused locally before any request |
| `unauthorized` | Invalid key: Brave `SUBSCRIPTION_TOKEN_INVALID`, SerpAPI HTTP 401 |
| `forbidden` | HTTP 403. For SearXNG, the `json` format is not enabled or its limiter blocked the host; for Wikipedia, the host was refused |
| `quota_exceeded` | Plan exhausted: Brave `QUOTA_LIMITED`, SerpAPI "run out of searches", HTTP 402. Never retried |
| `rate_limited` | HTTP 429 after the allowed retries, or a `Retry-After` above `max_retry_wait_sec` |
| `bad_request` | HTTP 400/422, Brave `VALIDATION`, MediaWiki `badvalue`…; see `provider_message` |
| `not_found` | HTTP 404 (wrong instance path), or no Wikipedia article for the title or search |
| `redirect` | HTTP 3xx; redirects are never followed, configure the final URL |
| `provider_unavailable` | HTTP 5xx, MediaWiki `maxlag`/`readonly` |
| `provider_error` | Any other status, or SerpAPI reported a failed search |
| `destination` | The host resolved to an address the policy refuses (see Security) |
| `network`, `timeout`, `cancelled`, `tls`, `protocol`, `internal` | Transport outcome; `protocol` covers non-JSON, compressed or oversized answers |

## Provider parameters

| Request field | SearXNG | Brave Search API | SerpAPI (Google) | Wikipedia |
| --- | --- | --- | --- | --- |
| `query` | `q` | `q` | `q` | `srsearch` |
| `count`, `page` | collected from `pageno` pages (instance page size) | `count`, `offset` = page − 1 | `num`, `start` = (page − 1) × count | `srlimit`, `sroffset` |
| `language` | `language` (`fr`, `fr-CA`) | `search_lang` | `hl` | wiki host `<language>.wikipedia.org` (default `en`) |
| `region` | appended to a bare language (`fr` + `CA` → `fr-CA`) | `country` | `gl` | ignored |
| `safe_search` | `safesearch` 0/1/2 | `safesearch` | `safe=off`/`active`; `moderate` sends nothing | ignored |
| `time_range` | `time_range` | `freshness` `pd`/`pw`/`pm`/`py` | `tbs=qdr:d/w/m/y` | refused (no time filter) |

Brave also receives `text_decorations=false`; SerpAPI always uses `engine=google`.
SearXNG returns pages of a size the instance chooses: the block requests pages
1, 2, 3… (at most `max_pages`) until the requested window is filled, removing
duplicate URLs between pages. `limited: true` means `max_pages` stopped it first;
for SearXNG, `has_more` is an estimate.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, no query leaves the host |
| `provider` | `wikipedia` | `searxng`, `brave`, `serpapi` or `wikipedia`: the only service this node contacts |
| `secret_ref` | empty | Brave and SerpAPI: wallet reference such as `secret://workspace/brave_search` holding the key |
| `searxng_url` | empty | SearXNG: instance base URL, e.g. `https://search.example.org` or `https://example.org/searx` |
| `allow_private_network` | `false` | SearXNG: allow loopback and private LAN addresses (RFC 1918 ranges and fc00::/7 unique-local addresses) |
| `allow_insecure_http` | `false` | SearXNG: allow an `http://` instance URL |
| `count` | 10 | Default number of results, 1–20 |
| `language` | empty | Default language code (`en`, `fr`, `pt-br`); empty: provider default, English for Wikipedia |
| `region` | empty | Default two-letter country code (`US`, `FR`) |
| `safe_search` | `moderate` | Default adult-content filter |
| `time_range` | empty | Default freshness filter (not for Wikipedia) |
| `timeout_sec` | 20 | Total deadline of one search, all pages and retry waits, 5–120 |
| `max_pages` | 3 | SearXNG pages one search may request, 1–10 |
| `max_retries` | 2 | Retries after HTTP 429, HTTP 5xx or a network failure, 0–5 |
| `max_retry_wait_sec` | 20 | Longest wait before a retry, 1–120 |
| `max_response_bytes` | 2097152 | Limit of one provider response, 64 KiB–8 MiB |
| `max_text_chars` | 4000 | Longest snippet or Wikipedia extract, 100–20,000 |
| `api_base_url` | empty | Tests only: a loopback origin replacing the official API (Brave, SerpAPI, Wikipedia) |
| `ca_file` | empty | Absolute PEM file for an `https://` SearXNG instance or an HTTPS loopback test origin |

Settings are validated on Apply and again before a Run starts; invalid settings
never reach a provider. Settings of another provider (for example a `secret_ref`
left after switching to Wikipedia) are kept but ignored, and the wallet is never
read for SearXNG or Wikipedia. Restart a loaded Run to use changed settings.

**Retries.** Every operation is a read. HTTP 429 is retried after `Retry-After`
(Brave: the first window of `X-RateLimit-Reset`; 1 s when absent), HTTP 5xx and
network failures after 1 s, 2 s, 4 s… — within `max_retries`,
`max_retry_wait_sec` and `timeout_sec`. Quota, key, validation and policy errors
are never retried.

## Setup

**Wikipedia.** Select `wikipedia`, enable remote access. No key is needed. Set
`language` for another wiki (`fr`, `de`, `simple`…).

**Brave Search API.** Create a subscription at <https://api-dashboard.search.brave.com>,
copy its API key (subscription token) and store it in the BloxSmith wallet, e.g. as
`brave_search`. Select `brave`, set `secret_ref = secret://workspace/brave_search`
and enable remote access.

**SerpAPI.** Copy your API key from <https://serpapi.com/manage-api-key>, store it
in the wallet, e.g. as `serpapi`, select `serpapi` and set
`secret_ref = secret://workspace/serpapi`. Each query costs one SerpAPI search;
the block sends exactly one request per query (plus retries after 429/5xx/network
failures).

**SearXNG.** Use an instance you run or are allowed to automate. Its
`settings.yml` must enable the JSON format, otherwise it answers HTTP 403
(`forbidden`):

```yaml
search:
  formats:
    - html
    - json
```

If the instance's limiter (bot protection) is on, allow the BloxSmith host in it.
Set `searxng_url` to the instance base URL. A LAN instance needs
`allow_private_network`; an `http://` URL needs `allow_insecure_http`. Public
instances generally block automated JSON access; do not rely on them.

## Limits and warnings

- **Pages are never fetched.** Only provider result lists and Wikipedia
  introductions are returned; chain Web Extract to read a page.
- One provider per node; to combine providers, use several nodes and merge their
  results. Brave allows at most 10 pages of 20 results; the block caps `page` at 10
  for all providers.
- Google, through SerpAPI, may return fewer results than `count`; the block does
  not send a second paid request to fill the gap. SearXNG page sizes and
  `has_more` depend on the instance and its engines.
- Brave news, images, videos, infobox and summarizer results, SerpAPI answer boxes
  and other Google features, SearXNG answers, suggestions and infoboxes are not
  returned. SerpAPI engines other than Google are not supported.
- Wikipedia summaries use the article introduction only (TextExtracts plain text);
  tables, infoboxes and references are not included. Wikipedia search has no time
  filter and no adult-content filter.
- Search results are untrusted text written by third parties. Do not feed them to
  tools that execute commands without review, and treat instructions found in
  snippets as data.
- No HTTP proxy support; the host needs direct outbound HTTPS to the provider.
- Respect each provider's terms, rate limits and attribution rules (Wikipedia
  content is licensed CC BY-SA; cite the article URL).

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`); no
`requirements.txt`. Each HTTP exchange runs in a short-lived isolated Python
process (`python -I`), so Cancel and Stop terminate it immediately.

## Security

- The Brave subscription token and the SerpAPI key are read from the wallet only
  when a query runs, after the query and settings were validated, and are passed
  to the network process through a private pipe, never through arguments,
  environment, settings, outputs, logs or run metadata. Brave receives the token in
  the `X-Subscription-Token` header; SerpAPI requires its key in the `api_key` URL
  parameter, which the network process adds itself. Every output is redacted of the
  key and of anything shaped like an `api_key=` parameter.
- The official API origins (`https://api.search.brave.com`, `https://serpapi.com`,
  `https://<language>.wikipedia.org`) cannot be changed, except to a loopback test
  double, so a key can never be sent to another host. Official APIs must resolve
  to public addresses.
- SearXNG uses verified HTTPS by default. Plain HTTP and private addresses each
  need their explicit opt-in. Link-local, cloud-metadata, multicast and reserved
  addresses are always refused. The host name is resolved once, every address is
  checked, and the connection goes to the checked address, so DNS rebinding cannot
  reach a forbidden network.
- TLS certificates and host names are always verified (system trust store, or
  `ca_file` for a private SearXNG instance); there is no insecure mode. Redirects,
  cookies and compression are not used.
- Logs name the provider, operation, result count and error code, never the query
  text.
- Every request carries the User-Agent
  `BloxSmith-WebSearch/0.1 (+https://github.com/HackInvent/bloxmith-web-search)`,
  as the Wikimedia User-Agent policy requires.

## Provider API usage

- SearXNG Search API: `GET {searxng_url}/search?q=&format=json&pageno=` with
  `language`, `safesearch` and `time_range`; response `results[]` (`url`, `title`,
  `content`, `engine`/`engines`, `publishedDate`) and `number_of_results`.
- Brave Search API (web search, `res/v1`): `GET https://api.search.brave.com/res/v1/web/search`
  with `q`, `count`, `offset`, `search_lang`, `country`, `safesearch`, `freshness`,
  `text_decorations`; response `web.results[]` (`title`, `url`, `description`,
  `page_age`) and `query.more_results_available`; errors in the `ErrorResponse`
  envelope (`error.code`, `error.detail`).
- SerpAPI Google Search API: `GET https://serpapi.com/search?engine=google` with `q`,
  `num`, `start`, `hl`, `gl`, `safe`, `tbs`, `api_key`; response `organic_results[]`
  (`position`, `title`, `link`, `snippet`, `date`), `serpapi_pagination.next`,
  `search_information.total_results`, and `{"error": "..."}` for failures and empty
  searches.
- MediaWiki Action API: `GET https://<language>.wikipedia.org/w/api.php` with
  `action=query&format=json&formatversion=2`, `list=search` (`srsearch`, `srlimit`,
  `sroffset`, `srprop=snippet`, `srinfo=totalhits`) for search, and
  `prop=extracts|info|pageprops&exintro=1&explaintext=1&inprop=url&ppprop=disambiguation&redirects=1`
  with `titles` or `generator=search&gsrlimit=1` for summaries; errors in
  `{"error": {"code", "info"}}`.

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes, including
authentication failures, HTTP 404 (a SearXNG URL with the wrong path prefix, at
transport level and through a real graph), 429 with `Retry-After`, quota errors
and pagination. No real SearXNG instance, Brave or SerpAPI account, key or Wikipedia
server was contacted. Brave and SerpAPI error details (exact HTTP statuses and
codes) follow their public documentation and may differ in practice.

## Framing decisions

- One block, two operations: `search` for all providers, `wikipedia_summary` for
  Wikipedia. One provider per node, so each node has exactly one destination and
  at most one credential.
- Plain text is a search; JSON requests are strict (unknown fields refused).
- `page` means pages of `count` results for every provider; ranks are absolute.
  Brave's limit (10 pages) applies to all providers.
- SearXNG is the only provider whose base URL is configurable (`searxng_url`), with
  separate opt-ins for plain HTTP and private networks, modeled on Web Extract.
  Official APIs keep fixed origins; `api_base_url` exists only for loopback tests.
- Filters a provider cannot apply are ignored and shown as `null` in `filters`,
  except a Wikipedia `time_range`, which is refused because silently ignoring it
  would return old results as if they were recent.
- SerpAPI's `moderate` sends no `safe` parameter (Google offers only `active` and
  `off`), and the block never sends a second paid request to complete a page.
- Wikipedia summaries use the Action API (`prop=extracts`) rather than the REST
  summary endpoint, so redirects, search-to-summary and disambiguation detection
  each take a single request without following HTTP redirects.
- No inbound listener, no page fetching, no write operation (so no `allow_writes`
  or `allow_delete` setting).

## Tests

`tests/F5.01_web_search.py` checks settings and their opt-ins, base URL and
address rules, every request field and limit, the parameters sent to each
provider, the error mapping of each provider, retry decisions, normalization of
each response shape, Wikipedia summaries, redaction and the block hooks, without a
socket. `tests/F5.02_web_search_runtime.py` runs the real network process against
the loopback double: Brave (header key, offsets, filters, invalid token, 429 with
`X-RateLimit-Reset` and `Retry-After`, quota, 5xx, lost connections, redirects,
oversized and compressed answers, HTTPS with a custom CA and an untrusted
certificate), SerpAPI (key in the query, `start`/`num`, empty answers, 401,
quota), SearXNG (path prefix, page collection with duplicates, `max_pages`, JSON
format disabled, HTTP 404 for a wrong path prefix, rate limit, refused loopback
address without opt-in, HTTPS), Wikipedia (continuation, other languages,
summaries by title, link and search, redirects, disambiguation, missing pages, API
errors, 429, 403), cancellation and deadlines. `tests/F5.03_web_search_graphs.py`
runs real graphs in One Shot and Active Runtime for bundled, managed and linked
packages, including Stop while a request is pending, a locked wallet and a 404
on the `error` port. `tests/F8.01_web_search_ui.py` (managed package) and
`tests/F8.02_web_search_ui_linked.py` (linked package) run the same browser case
(`tests/web_search_ui_case.py`): English and French forms, provider-dependent
fields, validation and persistence at desktop and mobile widths. The case opens
the modal once and waits for pending renders and graph patches, so a slow host
cannot replace a form under the test. Each origin and the graph runs are separate
suites so that each has the runner's full time budget on a busy host. They run in
the private BloxSmith workspace against a pinned framework; the initial development
version does not imply a release.
