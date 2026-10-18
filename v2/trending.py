"""Trending podcasts per country, from Apple's free top-shows chart, cached 12 hours.

At most two calls to Apple a day per country, shared by every user. A failure never
raises: the page gets an error message and keeps working.
"""

from v2 import db, itunes, refresh

COUNTRIES = {"in": "India", "us": "US", "gb": "UK"}  # the only codes accepted
DEFAULT_COUNTRY = "in"
CACHE_SECONDS = 12 * 3600
FAILED = "Could not load trending shows right now."


def country(raw) -> str:
    code = str(raw or "").strip().lower()
    return code if code in COUNTRIES else DEFAULT_COUNTRY


def get(raw_country) -> dict:
    """{"country", "label", "shows", "error"}. `shows` is [] when `error` is set."""
    code = country(raw_country)
    out = {"country": code, "label": COUNTRIES[code], "shows": [], "error": None}
    try:
        entry = db.cache_entry("trending", code)
        if entry is not None:
            # Served at once, however old; past CACHE_SECONDS refreshed in the background.
            out["shows"], age = entry
            if age > CACHE_SECONDS:
                refresh.schedule(("trending", code), lambda: refresh_chart(code))
            return out
        out["shows"] = _fetch_chart(code)  # no cache at all: the only case that waits
    except Exception:  # Apple down, bad JSON, empty chart: say so, never break the page
        out["error"] = FAILED
    return out


def _fetch_chart(code: str) -> list[dict]:
    shows = itunes.top_shows(code)
    if not shows:
        raise ValueError("empty chart")  # never cache an empty answer
    db.store_trending(code, shows)
    return shows


def refresh_chart(code: str) -> None:
    """Background refresh; on failure the old chart stays."""
    _fetch_chart(code)
