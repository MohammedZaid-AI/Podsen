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


def _timed_get_json(url: str) -> dict:
    """Every Apple call goes through here so the request timing counts it. Looks up
    _get_json at call time, so tests can stub it."""
    from v2 import timing

    with timing.measure("apple"):
        return _get_json(url)


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
    get_json = get_json or _timed_get_json
    url = f"{SEARCH_URL}?{urlencode({'media': 'podcast', 'entity': 'podcast', 'term': term, 'limit': limit})}"
    results = get_json(url).get("results") or []
    return [s for s in (normalize(r) for r in results) if s]


def episodes(itunes_id: str, get_json=None) -> list[dict]:
    """A show's recent episodes on Apple Podcasts (up to 200): guid, title, https URL."""
    if not str(itunes_id).isdigit():
        return []
    get_json = get_json or _timed_get_json
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
    return lookup_many([itunes_id], get_json).get(str(itunes_id))


def lookup_many(ids, get_json=None) -> dict[str, dict]:
    """Look up several shows in one call: {itunes_id: show}. Ids Apple doesn't know are
    simply absent. Non-numeric ids are dropped before anything is sent."""
    wanted = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip().isdigit()))
    get_json = get_json or _timed_get_json
    out = {}
    for start in range(0, len(wanted), 50):
        chunk = wanted[start:start + 50]
        query = urlencode({"id": ",".join(chunk), "entity": "podcast"}, safe=",")
        for r in get_json(f"{LOOKUP_URL}?{query}").get("results") or []:
            s = normalize(r)
            if s and s["itunes_id"] in chunk:
                out[s["itunes_id"]] = s
    return out


# --- trending: Apple's top podcasts chart ----------------------------------------------

TOP_URL = "https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top/25/podcasts.json"


def _first_genre(genres) -> str:
    # Objects ({"genreId", "name"}) per the chart's documented shape; plain strings too,
    # so a format change costs the genre label at worst, never the page.
    names = [g.get("name") if isinstance(g, dict) else g for g in genres or []]
    names = [n for n in names if isinstance(n, str) and n]
    specific = [n for n in names if n != "Podcasts"]  # the umbrella genre says nothing
    return (specific or names or [""])[0]


def top_shows(country: str, get_json=None) -> list[dict]:
    """Apple's top 25 podcasts for a country, each with its public feed checked.

    The chart has no feedUrl, so one batched lookup fills it in: a show whose
    feed_url is None has no public feed and can't be added.
    """
    get_json = get_json or _timed_get_json
    items = (get_json(TOP_URL.format(country=country)).get("feed") or {}).get("results") or []
    chart = [i for i in items if str(i.get("id", "")).isdigit() and i.get("name")]
    feeds = lookup_many([i["id"] for i in chart], get_json)
    return [
        {
            "itunes_id": str(i["id"]),
            "name": i["name"],
            "artist": i.get("artistName") or "",
            "genre": _first_genre(i.get("genres")),
            "artwork_url": _https(i.get("artworkUrl100")),
            "apple_url": _https(i.get("url")),
            "feed_url": (feeds.get(str(i["id"])) or {}).get("feed_url"),
        }
        for i in chart
    ]
