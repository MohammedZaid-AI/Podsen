"""Per-request timing: one log line per request, e.g.

    GET /v2/today 200 2340ms db=12q/1800ms conn=1/new0 feeds=3/400ms apple=1/120ms

db = SQL statements and their time; conn = pool checkouts / brand-new database
connections opened; feeds / apple = network calls and their time. The line holds the
method, the PATH (never the query string: ?code= and ?key= travel there), the status,
counts and milliseconds. Nothing else: no emails, no tokens, no SQL parameters.
"""

import logging
import threading
import time
from contextlib import contextmanager

from flask import g, has_request_context, request

log = logging.getLogger("podsen.timing")
_lock = threading.Lock()


class Stats:
    __slots__ = ("start", "db_n", "db_ms", "conn_n", "new_n", "feeds_n", "feeds_ms",
                 "apple_n", "apple_ms")

    def __init__(self):
        self.start = time.perf_counter()
        self.db_n = self.conn_n = self.new_n = self.feeds_n = self.apple_n = 0
        self.db_ms = self.feeds_ms = self.apple_ms = 0.0


def current() -> Stats | None:
    return g.get("_podsen_timing") if has_request_context() else None


def add(kind: str, ms: float = 0.0, n: int = 1) -> None:
    """kind: "db", "feeds", "apple" (count + ms), or "conn" / "new" (count only)."""
    st = current()
    if st is None:
        return
    with _lock:  # feed fetches report from worker threads
        setattr(st, f"{kind}_n", getattr(st, f"{kind}_n") + n)
        if kind in ("db", "feeds", "apple"):
            setattr(st, f"{kind}_ms", getattr(st, f"{kind}_ms") + ms)


@contextmanager
def measure(kind: str, n: int = 1):
    t = time.perf_counter()
    try:
        yield
    finally:
        add(kind, (time.perf_counter() - t) * 1000, n)


def line(method: str, path: str, status: int, st: Stats) -> str:
    total = (time.perf_counter() - st.start) * 1000
    return (f"{method} {path} {status} {total:.0f}ms db={st.db_n}q/{st.db_ms:.0f}ms "
            f"conn={st.conn_n}/new{st.new_n} feeds={st.feeds_n}/{st.feeds_ms:.0f}ms "
            f"apple={st.apple_n}/{st.apple_ms:.0f}ms")


def install(app) -> None:
    """Start a Stats object per request and log one line when the response is ready."""

    @app.before_request
    def _timing_start():
        g._podsen_timing = Stats()

    @app.after_request
    def _timing_finish(response):
        st = current()
        if st is not None:
            log.info(line(request.method, request.path, response.status_code, st))
        return response
