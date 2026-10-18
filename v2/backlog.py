"""A user's backlog: recent episodes from their saved shows' public feeds, minus heard.

Parsed feeds are cached in the database. A cached feed is served at once, however
old; past CACHE_SECONDS it is also refreshed in the background (v2/refresh.py). Only
a feed with no cache at all is fetched while the user waits. A feed that fails then
is returned in `failed` for the page to show; it is never silently left out.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from v2 import db, refresh, rss, timing

RECENT_DAYS = 30
CACHE_SECONDS = 3600
# The cache keeps only what picking uses: episodes from the last RECENT_DAYS plus
# this margin, without descriptions (never shown in the app). A long-running show's
# full feed was 1.5 MB per read from the database; its recent part is a few KB.
CACHE_MARGIN_DAYS = 14


def _fetch_parse(feed_url: str):
    # rss.fetch_feed is looked up at call time so tests can stub the network.
    try:
        return feed_url, rss.parse_feed(rss.fetch_feed(feed_url)), None
    except Exception as e:  # any failure is reported to the user, whatever its kind
        return feed_url, None, f"{type(e).__name__}: {str(e)[:160]}"


def for_cache(parsed: dict, today: date | None = None) -> dict:
    """The part of a parsed feed worth caching (see CACHE_MARGIN_DAYS)."""
    today = today or date.today()
    keep_from = (today - timedelta(days=RECENT_DAYS + CACHE_MARGIN_DAYS)).isoformat()
    eps = parsed.get("episodes") or []
    undated = sum(1 for e in eps if not e.get("released")) + int(parsed.get("undated") or 0)
    recent = [{k: v for k, v in e.items() if k != "description"} for e in eps
              if e.get("released") and e["released"] >= keep_from]
    return {"show": parsed.get("show"), "episodes": recent, "undated": undated,
            "skipped_no_duration": parsed.get("skipped_no_duration", 0)}


def refresh_feed(feed_url: str) -> None:
    """Background refresh of one feed's cache. On failure the old copy stays."""
    _, result, err = _fetch_parse(feed_url)
    if err:
        raise RuntimeError(err)
    db.store_feed(feed_url, for_cache(result))
    db.store_descriptions(feed_url, result["episodes"])  # already in the background here


def fetch_missing(feed_urls: list[str]) -> tuple[dict, dict]:
    """Fetch feeds that have no cache, in parallel; store them in one statement.
    Returns (parsed by url, error by url)."""
    parsed, failed = {}, {}
    if not feed_urls:
        return parsed, failed
    # Workers only fetch and parse; all DB writes stay on this thread.
    with timing.measure("feeds", n=len(feed_urls)), ThreadPoolExecutor(max_workers=8) as pool:
        for url, result, err in pool.map(_fetch_parse, feed_urls):
            if err:
                failed[url] = err
            else:
                parsed[url] = result
    # The user is waiting here: queue the description write instead of doing it now.
    for url, p in parsed.items():
        refresh.schedule(("episodes", url), lambda u=url, eps=p["episodes"]: db.store_descriptions(u, eps))
    parsed = {url: for_cache(p) for url, p in parsed.items()}
    db.store_cache("feed", parsed)
    return parsed, failed


def build(user_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    shows = db.list_shows(user_id)
    heard = db.heard_ids(user_id)
    cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()
    # One query for every feed AND Apple cache row these shows need (links.py reads
    # the Apple ones later from the same request memo).
    db.prefetch_caches([s["feed_url"] for s in shows], [s["itunes_id"] for s in shows])

    parsed, missing, stale = {}, [], 0
    for s in shows:
        entry = db.cache_entry("feed", s["feed_url"])
        if entry is None:
            missing.append(s["feed_url"])
            continue
        parsed[s["feed_url"]], age = entry
        if age > CACHE_SECONDS:  # serve it now, refresh it for next time
            stale += refresh.schedule(("feed", s["feed_url"]), lambda u=s["feed_url"]: refresh_feed(u))

    fetched, failed_urls = fetch_missing(missing)
    parsed.update(fetched)

    episodes, seen, failed, undated = [], set(), [], 0
    for s in shows:
        if s["feed_url"] in failed_urls:
            failed.append({"show": s["name"], "error": failed_urls[s["feed_url"]]})
            continue
        undated += int(parsed[s["feed_url"]].get("undated") or 0)
        for ep in parsed[s["feed_url"]]["episodes"]:
            if not ep.get("released"):
                undated += 1
                continue
            if ep["released"] < cutoff or ep["id"] in heard or ep["id"] in seen:
                continue
            seen.add(ep["id"])
            episodes.append({**ep, "apple_url": s.get("apple_url"), "itunes_id": s["itunes_id"]})
    return {"episodes": episodes, "failed": failed, "shows": len(shows), "undated": undated,
            "stale_refreshing": stale}
