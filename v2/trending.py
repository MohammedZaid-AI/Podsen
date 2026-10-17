"""Trending podcasts per country, from Apple's free top-shows chart, cached 12 hours.

At most two calls to Apple a day per country, shared by every user. A failure never
raises: the page gets an error message and keeps working.
"""

from v2 import db, itunes

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
        cached = db.cached_trending(code, CACHE_SECONDS)
        if cached is None:
            cached = itunes.top_shows(code)
            if not cached:
                raise ValueError("empty chart")  # don't cache an empty answer for 12 hours
            db.store_trending(code, cached)
        out["shows"] = cached
    except Exception:  # Apple down, bad JSON, empty chart: say so, never break the page
        out["error"] = FAILED
    return out
