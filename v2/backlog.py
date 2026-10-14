"""A user's backlog: recent episodes from their saved shows' public feeds, minus heard.

Parsed feeds are cached in SQLite for an hour. A feed that fails is returned in
`failed` for the page to show; it is never silently left out.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from v2 import db, rss

RECENT_DAYS = 30
CACHE_SECONDS = 3600


def _fetch_parse(feed_url: str):
    # rss.fetch_feed is looked up at call time so tests can stub the network.
    try:
        return feed_url, rss.parse_feed(rss.fetch_feed(feed_url)), None
    except Exception as e:  # any failure is reported to the user, whatever its kind
        return feed_url, None, f"{type(e).__name__}: {str(e)[:160]}"


def build(user_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    shows = db.list_shows(user_id)
    heard = db.heard_ids(user_id)
    cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()

    parsed, missing = {}, []
    for s in shows:
        hit = db.cached_feed(s["feed_url"], CACHE_SECONDS)
        if hit is None:
            missing.append(s["feed_url"])
        else:
            parsed[s["feed_url"]] = hit

    failed_urls = {}
    if missing:
        # Workers only fetch and parse; all DB writes stay on this thread.
        with ThreadPoolExecutor(max_workers=8) as pool:
            for url, result, err in pool.map(_fetch_parse, missing):
                if err:
                    failed_urls[url] = err
                else:
                    db.store_feed(url, result)
                    parsed[url] = result

    episodes, seen, failed, undated = [], set(), [], 0
    for s in shows:
        if s["feed_url"] in failed_urls:
            failed.append({"show": s["name"], "error": failed_urls[s["feed_url"]]})
            continue
        for ep in parsed[s["feed_url"]]["episodes"]:
            if not ep.get("released"):
                undated += 1
                continue
            if ep["released"] < cutoff or ep["id"] in heard or ep["id"] in seen:
                continue
            seen.add(ep["id"])
            episodes.append({**ep, "apple_url": s.get("apple_url"), "itunes_id": s["itunes_id"]})
    return {"episodes": episodes, "failed": failed, "shows": len(shows), "undated": undated}
