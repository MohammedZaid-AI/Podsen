"""Exact-episode Apple Podcasts links for picks.

Match order: episodeGuid == RSS guid (our episode id), then an exact title match
(trimmed, case-insensitive). No match -> no episode link, and the page says
"Search on Apple Podcasts" with the show page instead, so the label never
promises more than the link does.
"""

from v2 import db, itunes

APPLE_CACHE_SECONDS = 6 * 3600


def _norm(title) -> str:
    return " ".join(str(title or "").split()).casefold()


def match_episode(ep: dict, apple_episodes: list[dict]) -> str | None:
    """trackViewUrl of the Apple episode matching `ep`, or None."""
    guid = str(ep.get("id") or "").strip()
    if guid:
        for a in apple_episodes:
            if a["guid"].strip() == guid:
                return a["url"]
    title = _norm(ep.get("title"))
    if title:
        for a in apple_episodes:
            if _norm(a["title"]) == title:
                return a["url"]
    return None


def _apple_episodes(itunes_id: str) -> list[dict]:
    cached = db.cached_apple_episodes(itunes_id, APPLE_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        found = itunes.episodes(itunes_id)
    except Exception:
        # A failed lookup only costs the exact link; the show-page fallback still
        # works. Not cached, so the next run tries again.
        return []
    db.store_apple_episodes(itunes_id, found)
    return found


def add_apple_episode_urls(picks: list[dict]) -> list[dict]:
    """Return picks with `apple_episode_url` set where an exact match exists."""
    per_show = {}
    out = []
    for p in picks:
        sid = p.get("itunes_id")
        if sid and sid not in per_show:
            per_show[sid] = _apple_episodes(sid)
        out.append({**p, "apple_episode_url": match_episode(p, per_show.get(sid, []))})
    return out
