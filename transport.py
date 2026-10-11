"""Own each provider exchange in a killable child process and run the two operations.

The API key travels only through the child's private stdin pipe. Cancellation,
Stop and deadlines terminate the whole child process group. Every operation is a
read: HTTP 429 is retried after a bounded Retry-After, HTTP 5xx and network
failures after a bounded backoff.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import logic

WORKER = Path(__file__).with_name("search_worker.py")
ENVIRONMENT = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
OUTPUT_LIMIT = 3 * 8388608 + 65536


class WorkerCall:
    """One running exchange. poll() is short enough for Stop-aware loops."""

    def __init__(self, job):
        self.payload = json.dumps(job, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.child = subprocess.Popen([sys.executable, "-I", "-B", "-u", str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            env=dict(ENVIRONMENT))
        self.first, self.output = True, None

    def poll(self, timeout=0.05):
        """Return the worker result once finished, else None after at most timeout seconds."""
        if self.output is not None:
            return self.output
        try:
            output, _ = self.child.communicate(input=self.payload if self.first else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.first = False
            return None
        self.first = False
        if len(output) > OUTPUT_LIMIT:
            self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        else:
            try:
                value = json.loads(output)
                self.output = value if isinstance(value, dict) else {"status": "error", "code": "protocol", "phase": "receive"}
            except (ValueError, UnicodeError):
                self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        self.close()
        return self.output

    def close(self):
        """Terminate and reap only this process group; idempotent."""
        child = self.child
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout):
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


def interpret(outcome, provider, token):
    """Map a worker outcome to the provider body or a structured, secret-free error."""
    if outcome.get("status") == "response":
        return logic.provider_result(outcome, provider, token)
    code = outcome.get("code")
    if code in ("tls", "destination", "network", "timeout"):
        logic.fail(code)
    logic.fail("protocol")


def run_job(job, *, deadline, cancel):
    """Run one worker exchange while honouring the caller deadline and cancellation."""
    budget = deadline - time.monotonic()
    if budget <= 0.5:
        logic.fail("timeout")
    if cancel():
        logic.fail("cancelled")
    worker = WorkerCall({**job, "timeout_sec": float(budget)})
    try:
        while True:
            outcome = worker.poll(0.05)
            if outcome is not None:
                return outcome
            if cancel():
                logic.fail("cancelled")
            if time.monotonic() >= deadline + 1:
                logic.fail("timeout")
    finally:
        worker.close()


def pause(seconds, cancel):
    """Cancellation-aware wait used only between a retryable failure and the next attempt."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel():
            logic.fail("cancelled")
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


class Session:
    """One block operation: a shared deadline, cancellation and per-request retry budget."""

    def __init__(self, config, token, cancel=lambda: False):
        self.config, self.token, self.cancel = config, token, cancel
        self.deadline = time.monotonic() + config["timeout_sec"]
        self.attempts = self.requests = 0

    def get(self, target):
        """One provider request, retried within max_retries, max_retry_wait_sec and the deadline."""
        self.requests += 1
        tries = 0
        while True:
            tries += 1
            self.attempts += 1
            job = {"provider": self.config["provider"], "origin": target["origin"], "path": target["path"],
                   "query": target["query"], "token": self.token, "policy": logic.address_policy(self.config),
                   "ca_file": self.config["ca_file"], "max_response_bytes": self.config["max_response_bytes"]}
            try:
                outcome = run_job(job, deadline=self.deadline, cancel=self.cancel)
                return interpret(outcome, self.config["provider"], self.token)
            except logic.SearchError as exc:
                wait = logic.retry_delay(exc, tries, self.config, self.deadline - time.monotonic())
                if wait is None:
                    exc.attempts = self.attempts
                    raise
                pause(wait, self.cancel)


def collect(pool, seen, items):
    """Append items whose URL was not seen yet; returns how many were new."""
    added = 0
    for entry in items:
        if entry["url"] not in seen:
            seen.add(entry["url"])
            pool.append(entry)
            added += 1
    return added


def search(session, request):
    """One search: a single provider request, or SearXNG pages until the requested window is filled."""
    config = session.config
    count, page = request["count"], request["page"]
    start, end = (page - 1) * count, page * count
    pool, seen, limited = [], set(), False
    if config["provider"] == "searxng":
        pageno, exhausted, estimate, sent = 1, False, None, None
        while len(pool) < end:
            if pageno > config["max_pages"]:
                limited = True
                break
            target, sent = logic.search_target(config, request, pageno)
            last, _, found = logic.page_items(session.get(target), config, request["language"])
            estimate = estimate or found
            collect(pool, seen, last)
            pageno += 1
            if not last:
                exhausted = True
                break
        window = pool[start:end]
        # SearXNG has no result count per page: more exist while the last page was not empty.
        has_more = len(pool) > end or (len(pool) == end and not exhausted)
    else:
        target, sent = logic.search_target(config, request)
        items, more, estimate = logic.page_items(session.get(target), config, request["language"])
        collect(pool, seen, items)
        window = pool[:count]
        has_more = bool(more) or len(pool) > count
    results = [{"rank": start + index + 1, **entry} for index, entry in enumerate(window)]
    has_more = has_more and page < logic.MAX_PAGE
    return {"request_id": request["request_id"], "operation": "search", "provider": config["provider"],
            "query": request["query"], "page": page, "requested": count, "count": len(results), "results": results,
            "has_more": has_more, "next_page": page + 1 if has_more else None, "limited": limited,
            "total_estimate": estimate,
            "filters": sent, "requests": session.requests, "attempts": session.attempts}


def wikipedia_summary(session, request):
    """One MediaWiki Action API request by title (redirects resolved) or by best search match."""
    body = session.get(logic.summary_target(session.config, request))
    result = logic.summary(body, session.config, request)
    result.update(requests=session.requests, attempts=session.attempts)
    return result


def run(config, token, request, cancel=lambda: False):
    """Execute one validated request; returns the document for the results output."""
    session = Session(config, token, cancel)
    try:
        if request["operation"] == "wikipedia_summary":
            return wikipedia_summary(session, request)
        return search(session, request)
    except logic.SearchError as exc:
        exc.attempts = exc.attempts or session.attempts
        raise
