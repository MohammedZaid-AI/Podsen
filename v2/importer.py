"""Bring a listener's shows in one step: an OPML file exported from their podcast app, or
show names pasted one per line.

Nothing here fetches a URL from the upload. Each show is looked up by NAME on Apple's free
search; a feed address in the file is only compared with the feed Apple returns, to know the
match is the same show. What is added still goes through web._add_shows, which takes iTunes
ids and re-reads every feed URL from Apple (the one add path).
"""

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from v2 import itunes

MAX_FILE_BYTES = 1024 * 1024  # an OPML of a few hundred shows is well under 100 KB
MAX_IMPORT = 25               # same as one "Add selected"; Apple's search is rate limited
MAX_NAME = 100
WORKERS = 4


class ImportError_(ValueError):
    """A file we will not read; the message is shown to the listener as is."""


def parse_opml(data: bytes) -> list[dict]:
    """[{"name", "feed_url"}] in file order, duplicates dropped. Refuses anything that is not
    a plain OPML file (no DOCTYPE or entities, so no entity expansion tricks)."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportError_("That file is too big for a podcast list (over 1 MB).")
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ImportError_("That file is not a plain OPML file.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ImportError_("That file could not be read as OPML. Export it again, or paste the show names.")
    if root.tag.lower() != "opml":
        raise ImportError_("That file is not an OPML file. Export it again, or paste the show names.")
    out, seen = [], set()
    for o in root.iter("outline"):
        feed = (o.get("xmlUrl") or o.get("xmlurl") or "").strip()
        name = (o.get("title") or o.get("text") or "").strip()[:MAX_NAME]
        if not feed or not name:
            continue  # folders and blank rows
        key = _feed_key(feed) or name.lower()
        if key not in seen:
            seen.add(key)
            out.append({"name": name, "feed_url": feed})
    return out


def parse_names(text: str) -> list[dict]:
    """One show name per line; blank lines and repeats dropped."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        name = " ".join(line.split())[:MAX_NAME]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "feed_url": None})
    return out


def _feed_key(url) -> str | None:
    """A feed address compared loosely: no scheme, no www, no trailing slash, lower case host."""
    if not isinstance(url, str):
        return None
    p = urlparse(url.strip())
    if not p.netloc:
        return None
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}{'?' + p.query if p.query else ''}"


def _match_one(item: dict, get_json=None) -> dict:
    """{"name", "show" (Apple show or None), "how": "feed" | "name" | "guess" | "none" | "error"}."""
    try:
        results = itunes.search(item["name"], limit=5, get_json=get_json)
    except Exception as e:
        return {**item, "show": None, "how": "error", "error": type(e).__name__}
    with_feed = [s for s in results if s.get("feed_url")]
    want = _feed_key(item.get("feed_url"))
    if want:
        same = next((s for s in with_feed if _feed_key(s["feed_url"]) == want), None)
        if same:
            return {**item, "show": same, "how": "feed"}
    exact = next((s for s in with_feed if s["name"].strip().lower() == item["name"].lower()), None)
    if exact:
        return {**item, "show": exact, "how": "name"}
    if with_feed:
        return {**item, "show": with_feed[0], "how": "guess"}  # shown unticked: the listener decides
    return {**item, "show": None, "how": "none"}


def match(items: list[dict], get_json=None) -> tuple[list[dict], int]:
    """Look up the first MAX_IMPORT items on Apple, a few at a time. Returns (matches in the
    listener's order, how many were left for a second import)."""
    todo, left = items[:MAX_IMPORT], max(0, len(items) - MAX_IMPORT)
    if not todo:
        return [], left
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(todo))) as pool:
        found = list(pool.map(lambda it: _match_one(it, get_json), todo))
    return found, left
