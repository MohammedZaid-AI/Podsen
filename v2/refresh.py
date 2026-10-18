"""Stale-while-revalidate: serve cached data now, refresh it in the background.

Feeds, Apple episode lists and trending charts are served from the database cache
even when they are past their freshness window; a refresh is queued here instead of
making the user wait. Only a cache miss is fetched while the user waits.

Each key (e.g. ("feed", url)) is refreshed at most once at a time. A refresh that
fails is logged (type only) and the old cached copy stays in place.

MODE: "thread" (the app) runs refreshes on a small background pool; "inline" runs
them at once (tests, so cache tests stay deterministic); "record" only records them
(tests that prove a request made no network call itself).
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("podsen.refresh")

MODE = "thread"
recorded: list = []
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="podsen-refresh")
_inflight: set = set()
_lock = threading.Lock()


def _run(key, fn) -> None:
    try:
        fn()
    except Exception as e:  # never let a background refresh crash anything
        log.warning("refresh %s failed: %s", key[0], type(e).__name__)
    finally:
        with _lock:
            _inflight.discard(key)


def schedule(key: tuple, fn) -> bool:
    """Queue `fn` to refresh `key` unless that key is already being refreshed."""
    with _lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    if MODE == "record":
        recorded.append(key)
        with _lock:
            _inflight.discard(key)
    elif MODE == "inline":
        _run(key, fn)
    else:
        _pool.submit(_run, key, fn)
    return True
