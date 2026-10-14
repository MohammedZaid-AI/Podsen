"""Apple iTunes Search API: find podcasts and their public RSS feed URL. Free, no key.

Shows with no feedUrl (platform exclusives) are returned with feed_url=None so the
UI can say so instead of hiding them.
"""

import json
import urllib.request
from urllib.parse import urlencode, urlparse

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
TIMEOUT_S = 10
USER_AGENT = "Podsen/2 (+podcast triage)"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read(2 * 1024 * 1024))


def _https(url) -> str | None:
    """Only https links are rendered (cover art, Apple Podcasts link)."""
    return url if isinstance(url, str) and urlparse(url).scheme == "https" else None


def normalize(item: dict) -> dict | None:
    if not item.get("collectionId") or not item.get("collectionName"):
        return None
    feed = item.get("feedUrl")
    return {
        "itunes_id": str(item["collectionId"]),
        "name": item["collectionName"],
        "artist": item.get("artistName") or "",
        "feed_url": feed if isinstance(feed, str) and urlparse(feed).scheme in ("http", "https") else None,
        "artwork_url": _https(item.get("artworkUrl100") or item.get("artworkUrl60")),
        "apple_url": _https(item.get("collectionViewUrl")),
    }


def search(term: str, limit: int = 15, get_json=None) -> list[dict]:
    get_json = get_json or _get_json
    url = f"{SEARCH_URL}?{urlencode({'media': 'podcast', 'entity': 'podcast', 'term': term, 'limit': limit})}"
    results = get_json(url).get("results") or []
    return [s for s in (normalize(r) for r in results) if s]


def episodes(itunes_id: str, get_json=None) -> list[dict]:
    """A show's recent episodes on Apple Podcasts (up to 200): guid, title, https URL."""
    if not str(itunes_id).isdigit():
        return []
    get_json = get_json or _get_json
    url = f"{LOOKUP_URL}?{urlencode({'id': itunes_id, 'entity': 'podcastEpisode', 'limit': 200})}"
    out = []
    for r in get_json(url).get("results") or []:
        if r.get("wrapperType") != "podcastEpisode":
            continue  # the first result is the show itself
        link = _https(r.get("trackViewUrl"))
        if link:
            out.append({"guid": r.get("episodeGuid") or "", "title": r.get("trackName") or "", "url": link})
    return out


def lookup(itunes_id: str, get_json=None) -> dict | None:
    """Re-fetch one show by id, so the feed URL we store comes from Apple, not the form."""
    if not str(itunes_id).isdigit():
        return None
    get_json = get_json or _get_json
    results = get_json(f"{LOOKUP_URL}?{urlencode({'id': itunes_id, 'entity': 'podcast'})}").get("results") or []
    for r in results:
        s = normalize(r)
        if s and s["itunes_id"] == str(itunes_id):
            return s
    return None
