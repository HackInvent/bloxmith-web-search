"""Pure Web Search contracts: settings, requests, provider parameters, envelopes and normalization.

No network, filesystem, framework or third-party import happens here. Every
function is deterministic, so the policy can be checked directly and the
isolated network worker never depends on the framework.

Provider request and response shapes implemented:

- SearXNG Search API: ``GET {instance}/search?q=&format=json&pageno=`` with
  ``language``, ``safesearch`` and ``time_range``; the instance must enable the
  ``json`` format.
- Brave Search API: ``GET https://api.search.brave.com/res/v1/web/search`` with
  the ``X-Subscription-Token`` header.
- SerpAPI Google Search API: ``GET https://serpapi.com/search?engine=google``
  with the ``api_key`` query parameter.
- MediaWiki Action API: ``GET https://<language>.wikipedia.org/w/api.php`` with
  ``list=search`` and ``prop=extracts|info|pageprops``, no key.
"""

from collections.abc import Mapping
from datetime import datetime
import html
import ipaddress
import json
import math
import re
from urllib.parse import quote, unquote, urlencode, urlsplit

PROVIDERS = ("searxng", "brave", "serpapi", "wikipedia")
KEYED = ("brave", "serpapi")
OPERATIONS = ("search", "wikipedia_summary")
SAFE_SEARCH = ("off", "moderate", "strict")
TIME_RANGES = ("", "day", "week", "month", "year")
OFFICIAL_ORIGINS = {"brave": "https://api.search.brave.com", "serpapi": "https://serpapi.com"}
PATHS = {"brave": "/res/v1/web/search", "serpapi": "/search", "wikipedia": "/w/api.php", "searxng": "/search"}
USER_AGENT = "BloxSmith-WebSearch/0.1 (+https://github.com/HackInvent/bloxmith-web-search)"
DEFAULT_WIKIPEDIA_LANGUAGE = "en"
MAX_PAGE = 10
QUERY_LIMITS = {"searxng": 400, "brave": 400, "serpapi": 400, "wikipedia": 300}
BRAVE_WORD_LIMIT = 50
DEFAULTS = {
    "enabled": False, "provider": "wikipedia", "secret_ref": "", "searxng_url": "",
    "allow_private_network": False, "allow_insecure_http": False, "count": 10, "language": "", "region": "",
    "safe_search": "moderate", "time_range": "", "timeout_sec": 20, "max_pages": 3, "max_retries": 2,
    "max_retry_wait_sec": 20, "max_response_bytes": 2097152, "max_text_chars": 4000, "api_base_url": "",
    "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "count": (1, 20), "timeout_sec": (5, 120), "max_pages": (1, 10), "max_retries": (0, 5),
    "max_retry_wait_sec": (1, 120), "max_response_bytes": (65536, 8388608), "max_text_chars": (100, 20000),
}
REQUEST_KEYS = {
    "search": {"operation", "query", "count", "page", "language", "region", "safe_search", "time_range", "request_id"},
    "wikipedia_summary": {"operation", "query", "title", "language", "request_id"},
}
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.~+/=-]{8,512}\Z")
# Anything shaped like a provider credential is masked, wherever it could appear.
TOKEN_SHAPES = (re.compile(r"(?i)(api_key=)[^&\s\"']+"), re.compile(r"(?i)(x-subscription-token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"))
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
LANGUAGE = re.compile(r"[a-z]{2,12}(?:-[a-z0-9]{1,12}){0,2}\Z")
REGION = re.compile(r"[a-z]{2}\Z")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
PROVIDER_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,9})?)?"
                      r"(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?\Z")
TAG = re.compile(r"<[^<>]{0,2000}>")
WIKI_HOST = re.compile(r"([a-z0-9-]{2,35})\.(?:m\.)?wikipedia\.org\Z")
TITLE_FORBIDDEN = set("#<>[]|{}")
PRIVATE = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
INPUT_LIMIT = 65536
TITLE_LIMIT = 300
URL_LIMIT = 2048
MESSAGE_LIMIT = 500

MESSAGES = {
    "config": "Invalid Web Search settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before searching.",
    "secret": "Unlock the wallet and store the provider API key under the configured secret_ref.",
    "invalid_input": "The query does not follow the documented Web Search request contract; nothing was sent.",
    "bad_request": "The provider rejected the request parameters.",
    "unauthorized": "The provider rejected the API key; check the wallet secret.",
    "forbidden": "The provider refused this request for this key or host.",
    "searxng_forbidden": ("SearXNG refused the request (HTTP 403): enable the json format in the instance "
                          "settings (search.formats), or allow this host in its limiter."),
    "wikipedia_forbidden": "Wikimedia refused the request (HTTP 403); the host may be blocked or rate limited.",
    "not_found": "The provider endpoint or the requested page was not found.",
    "no_article": "No Wikipedia article matches the request.",
    "rate_limited": "Provider rate limit reached (HTTP 429); retry after the indicated delay.",
    "quota_exceeded": "The provider plan quota is exhausted; retrying will not help until it is renewed.",
    "provider_error": "The provider returned an unexpected status.",
    "redirect": "The provider answered with a redirect, which is never followed; configure the final URL.",
    "provider_unavailable": "The provider is temporarily unavailable (HTTP 5xx).",
    "network": "The provider could not be reached.",
    "destination": ("The host resolved to a loopback, private, link-local or reserved address; only a "
                    "SearXNG instance with allow_private_network may use a private network."),
    "tls": "TLS verification failed for the provider origin; nothing was sent.",
    "timeout": "The search exceeded timeout_sec.",
    "cancelled": "The search was cancelled.",
    "protocol": "The provider returned an invalid, compressed, non-JSON or oversized response.",
    "searxng_protocol": ("SearXNG did not return JSON; enable the json format in the instance settings "
                         "(search.formats) and check searxng_url."),
    "internal": "The search failed without a complete result.",
}


class SearchError(ValueError):
    """A diagnostic with a stable machine code and an English message, never the secret."""

    def __init__(self, code, detail=None, *, http_status=None, retry_after=None, provider_code=None,
                 provider_message=None):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code = code
        self.http_status, self.retry_after = http_status, retry_after
        self.provider_code, self.provider_message = provider_code, provider_message
        self.attempts, self.context = 0, {}

    def envelope(self, **context):
        """Return the public error document; callers add identifiers only."""
        result = {"code": self.code, "message": str(self)}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("provider_code", self.provider_code), ("provider_message", self.provider_message)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure."""
    raise SearchError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit=INPUT_LIMIT, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 16 or count > 10000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail(code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit=INPUT_LIMIT, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail(code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except SearchError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name):
    if type(value) is not bool:
        fail("config", f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def path_text(value, name):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail("config", f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail("config", f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_text(scheme, host, port):
    netloc = "[" + host + "]" if ":" in host else host
    return scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def parse_http_url(value, name):
    """(scheme, host, port, path) of an ASCII http(s) URL without credentials, query or fragment."""
    if not isinstance(value, str) or not 1 <= len(value) <= URL_LIMIT or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail("config", f"{name} must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail("config", f"{name} has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value or "\\" in value or "%" in host):
        fail("config", f"{name} must be an http(s) URL without credentials, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail("config", f"{name} has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail("config", f"{name} has an invalid port.")
    path = parsed.path.rstrip("/")
    if path and (not re.fullmatch(r"(?:/[A-Za-z0-9._~-]{1,64}){1,8}", path) or any(part in (".", "..") for part in path.split("/"))):
        fail("config", f"{name} may only carry a simple path prefix.")
    return parsed.scheme, host, port, path


def api_base(value):
    """Empty (official provider origin) or a loopback test double origin, HTTP or HTTPS."""
    if not isinstance(value, str):
        fail("config", "api_base_url must be text.")
    value = value.strip()
    if not value:
        return ""
    scheme, host, port, path = parse_http_url(value, "api_base_url")
    if path or not is_loopback(host):
        fail("config", "api_base_url must stay empty (official provider API); only a loopback test origin "
                       "such as http://127.0.0.1:8080 may replace it.")
    return origin_text(scheme, host, port)


def literal_address_allowed(host, allow_private):
    """Refuse literal addresses the SearXNG policy would never reach; host names are checked after DNS."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    address = getattr(address, "ipv4_mapped", None) or address
    if address.is_multicast or address.is_link_local or address.is_unspecified:
        return False
    if address.is_global and not address.is_reserved:
        return True
    return allow_private and (address.is_loopback or any(address.version == net.version and address in net
                                                          for net in PRIVATE))


def searxng_base(value, config, active):
    """A SearXNG instance base URL: https by default, plain http and private networks only by opt-in."""
    if not isinstance(value, str):
        fail("config", "searxng_url must be text.")
    value = value.strip()
    if not value:
        return ""
    scheme, host, port, path = parse_http_url(value, "searxng_url")
    if active:
        if scheme == "http" and not config["allow_insecure_http"]:
            fail("config", "A plain http:// searxng_url requires allow_insecure_http.")
        if host == "localhost" and not config["allow_private_network"]:
            fail("config", "A loopback searxng_url requires allow_private_network.")
        if not literal_address_allowed(host, config["allow_private_network"]):
            fail("config", "searxng_url points to a private, link-local or reserved address; private LAN and "
                           "loopback instances require allow_private_network, link-local is always refused.")
    return origin_text(scheme, host, port) + path


def language_tag(value, name, code):
    if not isinstance(value, str) or len(value) > 40:
        fail(code, f"{name} must be a language code such as en, fr or pt-br.")
    value = value.strip().lower().replace("_", "-")
    if value and not LANGUAGE.fullmatch(value):
        fail(code, f"{name} must be a language code such as en, fr or pt-br.")
    return value


def region_code(value, name, code):
    if not isinstance(value, str) or len(value) > 8:
        fail(code, f"{name} must be a two-letter country code such as US or FR.")
    value = value.strip().lower()
    if value and not REGION.fullmatch(value):
        fail(code, f"{name} must be a two-letter country code such as US or FR.")
    return value.upper()


def configuration(raw):
    """Pure settings validation; no wallet, filesystem, DNS or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0] + ". Credentials belong in the wallet.")
    config = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for key in ("enabled", "allow_private_network", "allow_insecure_http"):
        boolean(config[key], key)
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    if config["provider"] not in PROVIDERS:
        fail("config", "provider must be one of " + ", ".join(PROVIDERS) + ".")
    provider = config["provider"]
    ref = config["secret_ref"]
    if not isinstance(ref, str) or len(ref) > 256:
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/brave_search.")
    ref = ref.strip()
    if ref and not SECRET_REF.fullmatch(ref):
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/brave_search, never the key.")
    config["secret_ref"] = ref
    config["language"] = language_tag(config["language"], "language", "config")
    config["region"] = region_code(config["region"], "region", "config")
    if config["safe_search"] not in SAFE_SEARCH:
        fail("config", "safe_search must be off, moderate or strict.")
    if config["time_range"] not in TIME_RANGES:
        fail("config", "time_range must be empty, day, week, month or year.")
    config["api_base_url"] = api_base(config["api_base_url"])
    config["searxng_url"] = searxng_base(config["searxng_url"], config, provider == "searxng")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    if config["ca_file"]:
        if provider == "searxng":
            allowed = config["searxng_url"].startswith("https://")
        else:
            allowed = config["api_base_url"].startswith("https://")
        if not allowed:
            fail("config", "ca_file is accepted only for an https:// SearXNG instance or an HTTPS loopback test "
                           "origin; official APIs use the system trust store.")
    if config["enabled"]:
        if provider in KEYED and not config["secret_ref"]:
            fail("config", f"Remote access with {provider} requires secret_ref, a wallet reference holding the API key.")
        if provider == "searxng" and not config["searxng_url"]:
            fail("config", "Remote access with searxng requires searxng_url, the base URL of the instance.")
    return config


def api_token(value):
    """Validate the wallet value before it reaches a header or query; never echo it."""
    if not isinstance(value, str) or not TOKEN.fullmatch(value.strip()):
        fail("secret", "The wallet secret is not an API key (8-512 letters, digits or _.~+/=- characters).")
    return value.strip()


def redact(text, token=""):
    """Mask the key and anything shaped like a provider credential."""
    text = "" if text is None else str(text)
    if token:
        text = text.replace(token, "***")
    for pattern in TOKEN_SHAPES:
        text = pattern.sub(lambda match: match.group(1) + "***", text)
    return text


def clean(value, limit):
    """Bounded single-line text: markup removed, entities decoded, control characters and runs of spaces collapsed."""
    if not isinstance(value, str):
        return None
    value = html.unescape(TAG.sub(" ", value))
    value = " ".join("".join(" " if ord(char) < 32 or 127 <= ord(char) < 160 else char for char in value).split())
    return value[:limit].rstrip() or None


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def structured(value):
    """One request object from a mapping, JSON object text, or a plain-text query."""
    if isinstance(value, Mapping):
        document = thaw(value)
        encoded(document)
        return document
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "The query must be UTF-8 text.")
    if not isinstance(value, str):
        fail("invalid_input", "The query must be text or one JSON object.")
    if len(value.encode("utf-8", errors="replace")) > INPUT_LIMIT:
        fail("invalid_input", "The query message exceeds 64 KiB.")
    text = value.strip()
    if text.startswith("{"):
        document = strict_json(text)
        if not isinstance(document, dict):
            fail("invalid_input", "A JSON query must be one object.")
        return document
    return {"query": text}


def query_text(value, provider, name="query"):
    if not isinstance(value, str):
        fail("invalid_input", f"{name} must be text.")
    text = " ".join("".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).split())
    if not text:
        fail("invalid_input", f"{name} is required and cannot be empty.")
    limit = QUERY_LIMITS[provider]
    if len(text) > limit:
        fail("invalid_input", f"{name} exceeds {limit} characters for {provider}.")
    if provider == "brave" and len(text.split()) > BRAVE_WORD_LIMIT:
        fail("invalid_input", f"{name} exceeds {BRAVE_WORD_LIMIT} words, the Brave Search limit.")
    return text


def wiki_title(value, language, explicit_language):
    """A page title, or a https://<language>.wikipedia.org/wiki/<Title> link, and the wiki language."""
    if not isinstance(value, str) or len(value) > URL_LIMIT:
        fail("invalid_input", "title must be a Wikipedia page title or article link.")
    text = value.strip()
    if text.lower().startswith(("https://", "http://")):
        try:
            parsed = urlsplit(text)
        except ValueError:
            parsed = None
        match = WIKI_HOST.fullmatch((parsed.hostname or "").lower()) if parsed else None
        if not match or parsed.scheme != "https" or not parsed.path.startswith("/wiki/") or parsed.username is not None:
            fail("invalid_input", "title links must look like https://en.wikipedia.org/wiki/Title.")
        linked = match.group(1)
        if explicit_language and explicit_language != linked:
            fail("invalid_input", f"The title link is on {linked}.wikipedia.org but language is {explicit_language}.")
        language = language_tag(linked, "title link language", "invalid_input")
        try:
            text = unquote(parsed.path[len("/wiki/"):], errors="strict").replace("_", " ")
        except UnicodeError:
            fail("invalid_input", "The title link is not valid UTF-8.")
    text = " ".join(text.split())
    if (not text or len(text.encode("utf-8")) > 255 or any(char in TITLE_FORBIDDEN or ord(char) < 32 or ord(char) == 127
                                                          for char in text)):
        fail("invalid_input", "title must be 1-255 bytes without control characters or any of # < > [ ] | { }.")
    return text, language


def choice(document, key, allowed, default):
    value = document.get(key)
    if value is None:
        return default
    if value == "" and "" in allowed:
        return ""
    if value not in allowed:
        fail("invalid_input", f"{key} must be one of " + ", ".join(item or "\"\"" for item in allowed) + ".")
    return value


def parse_request(value, config):
    """Build one validated operation; nothing here touches the wallet or the network.

    A refused request still echoes a well-formed request_id and operation in its error.
    """
    document = structured(value)
    rid, operation = document.get("request_id"), document.get("operation", "search")
    context = {"request_id": rid if isinstance(rid, str) and IDENTIFIER.fullmatch(rid) else None,
               "operation": operation if operation in OPERATIONS else None}
    try:
        return build_request(document, config)
    except SearchError as exc:
        exc.context = context
        raise


def build_request(document, config):
    operation = document.get("operation", "search")
    if operation not in REQUEST_KEYS:
        fail("invalid_input", "operation must be search or wikipedia_summary.")
    unknown = set(document) - REQUEST_KEYS[operation]
    if unknown:
        fail("invalid_input", "Unknown " + operation + " field: " + sorted(unknown)[0] + ".")
    provider = config["provider"]
    request = {"operation": operation, "request_id": identifier(document.get("request_id")), "provider": provider}
    explicit = language_tag(document["language"], "language", "invalid_input") if document.get("language") is not None else ""
    language = explicit or config["language"]
    if operation == "wikipedia_summary":
        if provider != "wikipedia":
            fail("invalid_input", f"wikipedia_summary needs provider wikipedia; this node searches with {provider}.")
        given = [key for key in ("title", "query") if document.get(key) is not None]
        if len(given) != 1:
            fail("invalid_input", "wikipedia_summary needs exactly one of title or query.")
        language = language or DEFAULT_WIKIPEDIA_LANGUAGE
        if given[0] == "title":
            title, language = wiki_title(document["title"], language, explicit)
            request.update(title=title, query=None)
        else:
            request.update(title=None, query=query_text(document["query"], provider))
        request["language"] = language
        return request
    request["query"] = query_text(document.get("query"), provider)
    request["count"] = integer(document.get("count", config["count"]), "count", 1, BOUNDS["count"][1], "invalid_input")
    request["page"] = integer(document.get("page", 1), "page", 1, MAX_PAGE, "invalid_input")
    region = region_code(document["region"], "region", "invalid_input") if document.get("region") is not None else ""
    safe = choice(document, "safe_search", SAFE_SEARCH, None)
    time_range = choice(document, "time_range", TIME_RANGES, None)
    if provider == "wikipedia":
        if time_range:
            fail("invalid_input", "Wikipedia search has no time range; remove time_range or use another provider.")
        request.update(language=language or DEFAULT_WIKIPEDIA_LANGUAGE, region="", safe_search=None, time_range="")
        return request
    request.update(language=language, region=region or config["region"],
                   safe_search=safe or config["safe_search"],
                   time_range=config["time_range"] if time_range is None else time_range)
    return request


# ---------------------------------------------------------------- provider targets

def address_policy(config):
    """Which resolved addresses the worker may connect to."""
    if config["provider"] == "searxng":
        return "private" if config["allow_private_network"] else "public"
    return "loopback" if config["api_base_url"] else "public"


def origin_and_path(config, language=""):
    """(origin, path) of the provider endpoint for this request."""
    provider = config["provider"]
    if provider == "searxng":
        parsed = urlsplit(config["searxng_url"])
        return origin_text(parsed.scheme, parsed.hostname, parsed.port), parsed.path.rstrip("/") + PATHS["searxng"]
    if config["api_base_url"]:
        return config["api_base_url"], PATHS[provider]
    if provider == "wikipedia":
        return "https://" + (language or DEFAULT_WIKIPEDIA_LANGUAGE) + ".wikipedia.org", PATHS[provider]
    return OFFICIAL_ORIGINS[provider], PATHS[provider]


def bcp47(tag):
    """Language tag with conventional casing (fr-FR, zh-Hans) for SearXNG."""
    parts = tag.split("-")
    result = [parts[0]]
    for part in parts[1:]:
        result.append(part.upper() if len(part) == 2 and part.isalpha()
                      else part.title() if len(part) == 4 and part.isalpha() else part)
    return "-".join(result)


def query_string(params):
    return urlencode(params, quote_via=quote, safe="")


def search_target(config, request, pageno=1):
    """Endpoint, query string and the filters actually sent, for one search request."""
    provider = config["provider"]
    origin, path = origin_and_path(config, request["language"])
    count, page, offset = request["count"], request["page"], (request["page"] - 1) * request["count"]
    language, region, safe, time_range = (request["language"], request["region"], request["safe_search"],
                                          request["time_range"])
    sent = {"language": None, "region": None, "safe_search": None, "time_range": None}
    if provider == "brave":
        params = [("q", request["query"]), ("count", str(count)), ("offset", str(page - 1)), ("safesearch", safe),
                  ("text_decorations", "false")]
        sent["safe_search"] = safe
        if language:
            params.append(("search_lang", language))
            sent["language"] = language
        if region:
            params.append(("country", region))
            sent["region"] = region
        if time_range:
            params.append(("freshness", {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[time_range]))
            sent["time_range"] = time_range
    elif provider == "serpapi":
        params = [("engine", "google"), ("q", request["query"]), ("num", str(count))]
        if offset:
            params.append(("start", str(offset)))
        if language:
            params.append(("hl", language))
            sent["language"] = language
        if region:
            params.append(("gl", region.lower()))
            sent["region"] = region
        if safe in ("off", "strict"):
            params.append(("safe", "off" if safe == "off" else "active"))
            sent["safe_search"] = safe
        if time_range:
            params.append(("tbs", "qdr:" + time_range[0]))
            sent["time_range"] = time_range
    elif provider == "searxng":
        params = [("q", request["query"]), ("format", "json"), ("pageno", str(pageno)),
                  ("safesearch", str(SAFE_SEARCH.index(safe)))]
        sent["safe_search"] = safe
        if language:
            tag = bcp47(language)
            if region and "-" not in tag:
                tag += "-" + region
                sent["region"] = region
            params.append(("language", tag))
            sent["language"] = tag
        if time_range:
            params.append(("time_range", time_range))
            sent["time_range"] = time_range
    else:
        params = [("action", "query"), ("format", "json"), ("formatversion", "2"), ("list", "search"),
                  ("srsearch", request["query"]), ("srlimit", str(count)), ("sroffset", str(offset)),
                  ("srnamespace", "0"), ("srprop", "snippet"), ("srinfo", "totalhits")]
        sent["language"] = language
    return {"origin": origin, "path": path, "query": query_string(params)}, sent


def summary_target(config, request):
    """MediaWiki Action API request for one article intro, by title or by best search match."""
    origin, path = origin_and_path(config, request["language"])
    params = [("action", "query"), ("format", "json"), ("formatversion", "2"), ("prop", "extracts|info|pageprops"),
              ("exintro", "1"), ("explaintext", "1"), ("inprop", "url"), ("ppprop", "disambiguation"),
              ("redirects", "1")]
    if request["title"] is not None:
        params.append(("titles", request["title"]))
    else:
        params += [("generator", "search"), ("gsrsearch", request["query"]), ("gsrlimit", "1"), ("gsrnamespace", "0")]
    return {"origin": origin, "path": path, "query": query_string(params)}


# ---------------------------------------------------------------- provider envelopes

def status_code(status):
    if 300 <= status < 400:
        return "redirect"
    return {400: "bad_request", 401: "unauthorized", 402: "quota_exceeded", 403: "forbidden", 404: "not_found",
            422: "bad_request", 429: "rate_limited"}.get(status, "provider_unavailable" if status >= 500 else "provider_error")


BRAVE_CODES = {"SUBSCRIPTION_TOKEN_INVALID": "unauthorized", "RATE_LIMITED": "rate_limited",
               "QUOTA_LIMITED": "quota_exceeded", "OPTION_NOT_IN_PLAN": "forbidden", "VALIDATION": "bad_request"}
WIKI_CODES = {"ratelimited": "rate_limited", "maxlag": "provider_unavailable", "readonly": "provider_unavailable",
              "internal_api_error": "provider_unavailable"}
QUOTA_TEXT = re.compile(r"run out of searches|exceeded .*(?:plan|quota)|plan .*(?:limit|exhausted)", re.I)


def provider_details(provider, body, token):
    """(provider_code, provider_message) of a documented error envelope, bounded and redacted."""
    code = message = None
    if provider == "brave" and isinstance(body.get("error"), dict):
        code, message = body["error"].get("code"), body["error"].get("detail")
    elif provider == "wikipedia" and isinstance(body.get("error"), dict):
        code, message = body["error"].get("code"), body["error"].get("info")
    elif provider in ("serpapi", "searxng") and isinstance(body.get("error"), str):
        message = body["error"]
    code = code if isinstance(code, str) and PROVIDER_CODE.fullmatch(code) else None
    message = clean(redact(message, token), MESSAGE_LIMIT) if isinstance(message, str) else None
    return code, message


def retry_after(response):
    """Retry-After, else the first window of Brave's X-RateLimit-Reset, in bounded seconds."""
    for key in ("retry_after_header", "rate_reset_header"):
        value = response.get(key)
        if type(value) is int and 0 <= value <= 86400:
            return value
    return None


def provider_result(response, provider, token=""):
    """Interpret one worker HTTP response with the provider's documented error envelope."""
    status = response.get("http_status")
    if type(status) is not int or not 100 <= status <= 599:
        fail("protocol")
    body = response.get("body")
    wait = retry_after(response)
    if response.get("body_error") is not None or not isinstance(body, dict):
        if 200 <= status < 300:
            fail("protocol", MESSAGES["searxng_protocol"] if provider == "searxng" else None, http_status=status)
        code = status_code(status)
        if code == "forbidden" and provider in ("searxng", "wikipedia"):
            fail(code, MESSAGES[provider + "_forbidden"], http_status=status)
        fail(code, MESSAGES["redirect"] if code == "redirect" else None, http_status=status,
             retry_after=wait if code == "rate_limited" else None)
    if 200 <= status < 300 and not (provider == "wikipedia" and isinstance(body.get("error"), dict)):
        return body
    provider_code, message = provider_details(provider, body, token)
    if provider == "brave" and provider_code in BRAVE_CODES:
        code = BRAVE_CODES[provider_code]
    elif provider == "wikipedia" and 200 <= status < 300:
        code = WIKI_CODES.get(provider_code, "bad_request")
    else:
        code = status_code(status)
    if code == "rate_limited" and provider == "serpapi" and message and QUOTA_TEXT.search(message):
        code = "quota_exceeded"
    detail = None
    if code == "redirect":
        detail = MESSAGES["redirect"]
    elif code == "forbidden" and provider in ("searxng", "wikipedia"):
        detail = MESSAGES[provider + "_forbidden"]
    fail(code, detail, http_status=status, retry_after=wait if code == "rate_limited" else None,
         provider_code=provider_code, provider_message=message)


def retry_delay(error, attempts, config, deadline_left):
    """Seconds to wait before another attempt, or None when the bounds forbid one.

    Every operation is a read, so HTTP 429, HTTP 5xx and network failures can be
    retried; quota, authentication, validation and policy failures never are.
    """
    if error.code not in ("rate_limited", "provider_unavailable", "network") or attempts > config["max_retries"]:
        return None
    if error.code == "rate_limited":
        wait = error.retry_after if error.retry_after is not None else 1
    else:
        wait = min(2 ** (attempts - 1), config["max_retry_wait_sec"])
    if wait > config["max_retry_wait_sec"] or wait + 1 >= deadline_left:
        return None
    return float(wait)


# ---------------------------------------------------------------- normalization

def result_url(value):
    """(url, host) of an absolute http(s) result link, or None."""
    if not isinstance(value, str) or not 1 <= len(value) <= URL_LIMIT:
        return None
    value = value.strip()
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        return None
    return value, parsed.hostname.lower()


def published(value):
    """An ISO 8601 date or date-time as given, or a 'Mon D, YYYY' date as YYYY-MM-DD; otherwise None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) <= 40 and ISO_DATE.fullmatch(value):
        return value
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def item(title, url, snippet, config, *, date=None, engines=None):
    """One normalized result before ranking, or None when it has no usable link."""
    link = result_url(url)
    if link is None:
        return None
    result = {"title": clean(title, TITLE_LIMIT) or link[1], "url": link[0],
              "snippet": cut(clean(snippet, INPUT_LIMIT) or "", config["max_text_chars"])[0], "source": config["provider"],
              "site": link[1]}
    when = published(date)
    if when:
        result["published"] = when
    if engines:
        names = [name for name in (clean(engine, 40) for engine in engines if isinstance(engine, str)) if name][:10]
        if names:
            result["engines"] = names
    return result


def total(value):
    return value if type(value) is int and 0 <= value <= 10 ** 15 else None


def listed(body, key):
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        fail("protocol", f"The provider returned a malformed {key} list.")
    return [entry for entry in value if isinstance(entry, dict)]


def brave_page(body, config):
    """Brave web results: web.results[] with title, url, description and page_age."""
    web = body.get("web")
    if web is not None and not isinstance(web, dict):
        fail("protocol", "Brave returned a malformed web section.")
    raw = listed(web or {}, "results")
    items = [item(entry.get("title"), entry.get("url"), entry.get("description"), config, date=entry.get("page_age"))
             for entry in raw]
    query = body.get("query") if isinstance(body.get("query"), dict) else {}
    return [entry for entry in items if entry], query.get("more_results_available") is True, None


def serpapi_page(body, config):
    """SerpAPI Google organic_results[]; an empty search is a 200 answer with an error text."""
    metadata = body.get("search_metadata") if isinstance(body.get("search_metadata"), dict) else {}
    if isinstance(body.get("error"), str) and not body.get("organic_results"):
        if metadata.get("status") == "Error":
            _, message = provider_details("serpapi", body, "")
            fail("provider_error", "SerpAPI reported a failed search.", provider_message=message, http_status=200)
        return [], False, 0
    items = [item(entry.get("title"), entry.get("link"), entry.get("snippet"), config, date=entry.get("date"))
             for entry in listed(body, "organic_results")]
    pagination = body.get("serpapi_pagination")
    more = isinstance(pagination, dict) and isinstance(pagination.get("next"), str)
    information = body.get("search_information") if isinstance(body.get("search_information"), dict) else {}
    return [entry for entry in items if entry], more, total(information.get("total_results"))


def searxng_page(body, config):
    """SearXNG results[] with url, title, content, engines and publishedDate."""
    if not isinstance(body.get("results"), list):
        fail("protocol", MESSAGES["searxng_protocol"])
    items = [item(entry.get("title"), entry.get("url"), entry.get("content"), config, date=entry.get("publishedDate"),
                  engines=entry.get("engines") if isinstance(entry.get("engines"), list) else [entry.get("engine")])
             for entry in listed(body, "results")]
    found = total(body.get("number_of_results"))
    return [entry for entry in items if entry], None, found or None


def article_url(language, title):
    """Canonical article link built like MediaWiki's wfUrlencode."""
    return "https://" + language + ".wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe=";@$!*(),/~:")


def wikipedia_page(body, config, language):
    """MediaWiki list=search results with continuation and total hits."""
    query = body.get("query")
    if not isinstance(query, dict):
        fail("protocol", "Wikipedia returned no query result.")
    items = []
    for entry in listed(query, "search"):
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            items.append(item(title, article_url(language, title.strip()), entry.get("snippet"), config))
    more = isinstance(body.get("continue"), dict) and type(body["continue"].get("sroffset")) is int
    info = query.get("searchinfo") if isinstance(query.get("searchinfo"), dict) else {}
    return [entry for entry in items if entry], more, total(info.get("totalhits"))


def https_url(value):
    link = result_url(value)
    return link[0] if link and link[0].startswith("https://") else None


def cut(text, limit):
    """Cut at a word boundary within limit characters; returns (text, truncated)."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip() + "…", True


def summary(body, config, request):
    """The intro of one article from prop=extracts|info|pageprops, or a coded failure."""
    query = body.get("query")
    if query is None:
        fail("not_found", MESSAGES["no_article"])
    if not isinstance(query, dict):
        fail("protocol", "Wikipedia returned a malformed query result.")
    pages = listed(query, "pages")
    if not pages:
        fail("not_found", MESSAGES["no_article"])
    page = min(pages, key=lambda entry: entry.get("index") if type(entry.get("index")) is int else 0)
    if page.get("invalid") is True:
        _, message = provider_details("wikipedia", {"error": {"info": page.get("invalidreason")}}, "")
        fail("bad_request", "Wikipedia rejected the title as invalid.", provider_message=message)
    title = clean(page.get("title"), TITLE_LIMIT)
    if page.get("missing") is True or not title or type(page.get("pageid")) is not int:
        fail("not_found", MESSAGES["no_article"])
    raw = page.get("extract") if isinstance(page.get("extract"), str) else ""
    paragraphs = ["".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in line).strip()
                  for line in raw.replace("\r", "").split("\n")]
    text, truncated = cut("\n".join(line for line in paragraphs if line), config["max_text_chars"])
    language = request["language"]
    url = https_url(page.get("canonicalurl")) or https_url(page.get("fullurl")) or article_url(language, title)
    redirected = None
    for key in ("redirects", "normalized"):
        moves = listed(query, key)
        if moves and isinstance(moves[0].get("from"), str):
            redirected = clean(moves[0]["from"], TITLE_LIMIT)
            break
    disambiguation = isinstance(page.get("pageprops"), dict) and "disambiguation" in page["pageprops"]
    host = urlsplit(url).hostname
    return {"request_id": request["request_id"], "operation": "wikipedia_summary", "provider": "wikipedia",
            "language": language, "query": request["query"], "requested_title": request["title"], "title": title,
            "page_id": page["pageid"], "url": url, "extract": text, "truncated": truncated,
            "disambiguation": disambiguation, "redirected_from": redirected if redirected != title else None,
            "count": 1, "results": [{"rank": 1, "title": title, "url": url, "snippet": text, "source": "wikipedia",
                                     "site": host}]}


PARSERS = {"brave": brave_page, "serpapi": serpapi_page, "searxng": searxng_page}


def page_items(body, config, language):
    """Normalized items, the provider's more-results signal and its total estimate."""
    if config["provider"] == "wikipedia":
        return wikipedia_page(body, config, language)
    return PARSERS[config["provider"]](body, config)
