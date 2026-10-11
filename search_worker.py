"""Short-lived Web Search GET exchange, run as an isolated child process.

Standard library only. The job, including any API key, arrives on the private
stdin pipe, never in arguments or the environment. One JSON result is written
to stdout. The owning block kills this process group on cancellation, Stop or
deadline; a watchdog also exits if the parent disappears.

The host is resolved once, every address is checked against the job's address
policy, and the connection is made to the checked numeric address, so DNS
cannot rebind a public name to a private address. No redirect, cookie or
compression is used.
"""

import email.utils
import http.client
import ipaddress
import json
import os
import re
import resource
import socket
import ssl
import sys
import threading
import time
from urllib.parse import quote, urlsplit

PROVIDERS = {"searxng", "brave", "serpapi", "wikipedia"}
POLICIES = {"public", "private", "loopback"}
PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,2047}\Z")
QUERY = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{1,8192}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.~+/=-]{8,512}\Z")
PRIVATE = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
USER_AGENT = "BloxSmith-WebSearch/0.1 (+https://github.com/HackInvent/bloxmith-web-search)"
CHUNK = 65536


class Failure(Exception):
    """A content-free transport outcome reported to the parent."""

    def __init__(self, code, phase):
        super().__init__(code)
        self.code, self.phase = code, phase


def remaining(deadline, phase):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Failure("timeout", phase)
    return max(0.001, left)


def allowed(value, policy):
    """Official APIs need public addresses; loopback doubles loopback; SearXNG private LANs only by opt-in."""
    try:
        address = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    if policy == "loopback":
        return address.is_loopback
    if address.is_multicast or address.is_link_local or address.is_unspecified:
        return False
    if address.is_global and not address.is_reserved:
        return True
    return policy == "private" and (address.is_loopback or any(address.version == net.version and address in net
                                                                for net in PRIVATE))


def connection(job, deadline):
    origin = urlsplit(job["origin"])
    scheme, host = origin.scheme, origin.hostname
    port = origin.port or (443 if scheme == "https" else 80)
    if scheme not in ("http", "https") or not host:
        raise Failure("protocol", "connect")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise Failure("network", "connect") from None
    if not infos or len(infos) > 32 or any(not allowed(info[4][0], job["policy"]) for info in infos):
        raise Failure("destination", "connect")
    context = None
    if scheme == "https":
        try:
            context = ssl.create_default_context(cafile=job.get("ca_file") or None)
        except (OSError, ssl.SSLError):
            raise Failure("tls", "connect") from None
    sock = None
    for family, socktype, proto, _, address in infos[:4]:
        candidate = socket.socket(family, socktype, proto)
        candidate.settimeout(remaining(deadline, "connect"))
        try:
            candidate.connect(address)
            sock = candidate
            break
        except OSError:
            candidate.close()
    if sock is None:
        raise Failure("network", "connect")
    try:
        if context is not None:
            sock.settimeout(remaining(deadline, "connect"))
            sock = context.wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=remaining(deadline, "connect"))
        conn.sock = sock
        return conn
    except BaseException:
        sock.close()
        raise


def read_body(conn, response, deadline, limit):
    chunks, total = [], 0
    while True:
        if conn.sock is not None:
            conn.sock.settimeout(remaining(deadline, "receive"))
        piece = response.read1(min(CHUNK, limit + 1 - total)) if total <= limit else b""
        if not piece:
            break
        total += len(piece)
        chunks.append(piece)
        if total > limit:
            break
    return b"".join(chunks), total > limit


def retry_seconds(value):
    """Retry-After as delta seconds or an HTTP date, bounded; None when absent or unreadable."""
    value = (value or "").strip()
    if value.isdigit() and len(value) <= 6:
        return int(value)
    if value and len(value) <= 64:
        try:
            moment = email.utils.parsedate_to_datetime(value)
            return max(0, min(86400, int(moment.timestamp() - time.time())))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def reset_seconds(value):
    """First window of Brave's X-RateLimit-Reset (for example '1, 1419704')."""
    first = (value or "").split(",", 1)[0].strip()
    return int(first) if first.isdigit() and len(first) <= 6 else None


def reject_constant(_value):
    raise ValueError("Non-finite JSON number.")


def validate(job):
    """Re-check what the parent built: provider, policy, path, query and key shapes."""
    provider = job.get("provider")
    if provider not in PROVIDERS or job.get("policy") not in POLICIES:
        raise Failure("protocol", "connect")
    if not PATH.fullmatch(str(job.get("path", ""))) or not QUERY.fullmatch(str(job.get("query", ""))):
        raise Failure("protocol", "connect")
    keyed = provider in ("brave", "serpapi")
    token = job.get("token") or ""
    if keyed != bool(token) or (token and not TOKEN.fullmatch(token)):
        raise Failure("protocol", "connect")


def exchange(job):
    """One GET request/response."""
    validate(job)
    deadline = time.monotonic() + float(job["timeout_sec"])
    phase = "connect"
    conn = None
    try:
        conn = connection(job, deadline)
        conn.sock.settimeout(remaining(deadline, phase))
        phase = "send"
        target = job["path"] + "?" + job["query"]
        if job["provider"] == "serpapi":
            target += "&api_key=" + quote(job["token"], safe="")
        conn.putrequest("GET", target, skip_host=False, skip_accept_encoding=True)
        headers = [("Accept", "application/json"), ("Accept-Encoding", "identity"), ("User-Agent", USER_AGENT),
                   ("Connection", "close")]
        if job["provider"] == "brave":
            headers.append(("X-Subscription-Token", job["token"]))
        for key, value in headers:
            conn.putheader(key, value)
        conn.endheaders()
        phase = "receive"
        conn.sock.settimeout(remaining(deadline, phase))
        response = conn.getresponse()
        data, oversized = read_body(conn, response, deadline, int(job["max_response_bytes"]))
        headers = {key.lower(): value for key, value in response.getheaders()}
        result = {"status": "response", "http_status": response.status,
                  "retry_after_header": retry_seconds(headers.get("retry-after")),
                  "rate_reset_header": reset_seconds(headers.get("x-ratelimit-reset")),
                  "body": None, "body_error": None}
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if oversized:
            result["body_error"] = "too_large"
        elif headers.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
            result["body_error"] = "encoded"
        elif media != "application/json":
            result["body_error"] = "not_json"
        else:
            try:
                result["body"] = json.loads(data.decode("utf-8", errors="strict"), parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                result["body_error"] = "invalid_json"
        return result
    except Failure:
        raise
    except ssl.SSLCertVerificationError:
        raise Failure("tls", phase) from None
    except ssl.SSLError:
        raise Failure("tls" if phase == "connect" else "network", phase) from None
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", phase) from None
    except (OSError, http.client.HTTPException, ValueError):
        raise Failure("network", phase) from None
    finally:
        if conn is not None:
            conn.close()


def emit(value):
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        view = view[written:]


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    parent = os.getppid()
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    try:
        job = json.loads(raw)
        budget = float(job["timeout_sec"])
    except (ValueError, KeyError, TypeError):
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    box = {}

    def target():
        try:
            box["result"] = exchange(job)
        except Failure as failure:
            box["result"] = {"status": "error", "code": failure.code, "phase": failure.phase}
        except Exception:
            box["result"] = {"status": "error", "code": "network", "phase": "receive"}

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    hard_deadline = time.monotonic() + budget + 2
    while worker.is_alive():
        worker.join(0.2)
        if os.getppid() != parent:
            os._exit(3)
        if time.monotonic() > hard_deadline:
            emit({"status": "error", "code": "timeout", "phase": "receive"})
            os._exit(4)
    emit(box["result"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
