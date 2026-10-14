"""Public RSS feed -> episodes with the field names V1 uses.

Standard library only. Feeds are untrusted input: size and time are capped, only
http(s) to public addresses is fetched, and every interpolation downstream escapes.
"""

import html
import ipaddress
import math
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST = "https://podcastindex.org/namespace/1.0"

DESC_CHARS = 600
TIMEOUT_S = 20
MAX_BYTES = 30 * 1024 * 1024  # long-running shows publish multi-MB feeds
USER_AGENT = "Podsen/2 (+podcast triage research; RSS reader)"

EPISODE_FIELDS = ("id", "title", "show", "description", "duration_min", "released", "has_transcript")


def parse_duration(raw) -> int | None:
    """itunes:duration -> whole minutes, rounded UP.

    Accepts plain seconds ("3600", "3600.5"), MM:SS and HH:MM:SS. Rounding up rather
    than to nearest (V1 rounds) because V2's budget is a hard cap: a 30:20 episode
    must not be counted as fitting 30 minutes.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    if seconds <= 0:
        return None
    return math.ceil(seconds / 60)


def _clean(text: str | None, strip_tags: bool = True) -> str:
    """Unescape entities, collapse whitespace, cap length; strip tags from HTML fields.

    Titles keep strip_tags=False: "Why x < y and y > z" is a real title, and the
    output is escaped on render anyway.
    """
    if not text:
        return ""
    if strip_tags:
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:DESC_CHARS]


def _released(pub_date: str | None) -> str | None:
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date.strip()).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _text(item, tag: str) -> str | None:
    el = item.find(tag)
    return el.text if el is not None else None


def parse_feed(xml_bytes: bytes | str, per_feed: int | None = None) -> dict:
    """Parse one feed. Returns {"show", "episodes", "skipped_no_duration", "skipped_no_id"}.

    Episodes are newest first; `per_feed` keeps only the newest N that have a duration.
    """
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("not an RSS feed (no <channel>)")
    show = _clean(_text(channel, "title"), strip_tags=False) or "Untitled show"

    episodes, no_duration, no_id = [], 0, 0
    for item in channel.findall("item"):
        guid = (_text(item, "guid") or "").strip()
        if not guid:
            # Rare, but some feeds omit guid; the enclosure URL is the next most
            # stable identifier and is what most podcast apps fall back to.
            enc = item.find("enclosure")
            guid = (enc.get("url") if enc is not None else "") or (_text(item, "link") or "")
            guid = guid.strip()
        if not guid:
            no_id += 1
            continue
        minutes = parse_duration(_text(item, f"{{{ITUNES}}}duration"))
        if minutes is None:
            no_duration += 1
            continue
        description = (
            _text(item, "description")
            or _text(item, f"{{{ITUNES}}}summary")
            or _text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        )
        episodes.append(
            {
                "id": guid,
                "title": _clean(_text(item, "title"), strip_tags=False) or "Untitled episode",
                "show": show,
                "description": _clean(description),
                "duration_min": minutes,
                "released": _released(_text(item, "pubDate")),
                "has_transcript": item.find(f"{{{PODCAST}}}transcript") is not None,
            }
        )

    episodes.sort(key=lambda e: e["released"] or "", reverse=True)
    if per_feed:
        episodes = episodes[:per_feed]
    return {
        "show": show,
        "episodes": episodes,
        "skipped_no_duration": no_duration,
        "skipped_no_id": no_id,
    }


# --- fetching ----------------------------------------------------------------

def _check_public(url: str) -> None:
    """Refuse anything but http(s) to a public address.

    /label is reachable by anyone who has the tunnel URL, so a feed URL must not be a
    way to make this machine fetch its own LAN or localhost services.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http(s) feed URLs are allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {parsed.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{parsed.hostname} resolves to a non-public address")


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_PublicOnlyRedirects)


def fetch_feed(url: str) -> bytes:
    _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener.open(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"feed larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def load_feeds(urls: list[str], per_feed: int | None = None, fetch=fetch_feed) -> dict:
    """Fetch and parse several feeds. A failing feed is reported in `failed`, never dropped.

    Episode ids are deduplicated across feeds (first one wins) and the count reported,
    so two feeds sharing a guid cannot silently overwrite each other's labels.
    """
    urls = [u.strip() for u in urls if u and u.strip()]
    urls = list(dict.fromkeys(urls))  # same URL pasted twice

    def one(url):
        try:
            return url, parse_feed(fetch(url), per_feed), None
        except Exception as e:  # report every failure, whatever its kind
            return url, None, f"{type(e).__name__}: {str(e)[:200]}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, urls))

    episodes, feeds, failed, seen = [], [], [], set()
    no_duration = no_id = duplicate_ids = 0
    for url, parsed, err in results:
        if err:
            failed.append({"url": url, "error": err})
            continue
        no_duration += parsed["skipped_no_duration"]
        no_id += parsed["skipped_no_id"]
        kept = 0
        for ep in parsed["episodes"]:
            if ep["id"] in seen:
                duplicate_ids += 1
                continue
            seen.add(ep["id"])
            episodes.append(ep)
            kept += 1
        feeds.append(
            {
                "url": url,
                "show": parsed["show"],
                "episodes": kept,
                "skipped_no_duration": parsed["skipped_no_duration"],
            }
        )
    return {
        "episodes": episodes,
        "feeds": feeds,
        "failed": failed,
        "skipped_no_duration": no_duration,
        "skipped_no_id": no_id,
        "duplicate_ids": duplicate_ids,
    }
