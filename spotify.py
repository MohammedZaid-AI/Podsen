"""Plain, reliable code: OAuth + data fetch + playlist creation. No AI here."""

import base64
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, quote

import requests

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
SCOPES = " ".join(
    [
        "user-library-read",  # saved/followed shows + saved episodes
        "user-read-playback-position",  # episode resume_point -> heard/unheard
        "user-read-recently-played",
        "playlist-modify-private",
        "playlist-modify-public",
        "user-read-private",  # user id + country (market)
    ]
)

TIMEOUT = 20

# Why a show's episode fetch was dropped during fan-out. (machine code, human reason).
# 429 is by far the likeliest of these: the fan-out fires up to 50 requests in a few
# seconds and, unlike _call, worker threads deliberately don't retry.
DROP_REASONS = {
    401: ("token_expired", "token expired mid-scan"),
    403: ("forbidden", "Spotify refused access"),
    404: ("not_found", "not available in your region"),
    429: ("rate_limited", "hit Spotify's rate limit"),
}


def _cid() -> str:
    return os.environ.get("SPOTIFY_CLIENT_ID", "")


def _secret() -> str:
    return os.environ.get("SPOTIFY_CLIENT_SECRET", "")


def _redirect() -> str:
    return f"{os.environ.get('BASE_URL', '')}/callback"


def _basic_auth() -> str:
    raw = f"{_cid()}:{_secret()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def auth_url(state: str) -> str:
    params = {
        "client_id": _cid(),
        "response_type": "code",
        "redirect_uri": _redirect(),
        "scope": SCOPES,
        "state": state,
    }
    return f"{ACCOUNTS}/authorize?{urlencode(params)}"


def _tokenize(j: dict) -> dict:
    return {
        "access_token": j.get("access_token"),
        "refresh_token": j.get("refresh_token"),
        "expires_at": time.time() * 1000 + j.get("expires_in", 3600) * 1000 - 60_000,
    }


def exchange_code(code: str) -> dict:
    r = requests.post(
        f"{ACCOUNTS}/api/token",
        headers={"Authorization": _basic_auth()},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect()},
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"token exchange failed: {r.status_code} {r.text[:300]}")
    return _tokenize(r.json())


def refresh(refresh_token: str) -> dict:
    r = requests.post(
        f"{ACCOUNTS}/api/token",
        headers={"Authorization": _basic_auth()},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"token refresh failed: {r.status_code}")
    t = _tokenize(r.json())
    if not t["refresh_token"]:
        t["refresh_token"] = refresh_token  # Spotify often omits it on refresh
    return t


def _ensure_token(session) -> str:
    """Refresh if expired, persist to the session, return a usable access token.

    Assigning back into the Flask session dict is what marks it modified — mutating
    session['tokens'] in place would silently fail to persist.
    """
    if time.time() * 1000 > session["tokens"]["expires_at"]:
        session["tokens"] = refresh(session["tokens"]["refresh_token"])
    return session["tokens"]["access_token"]


def _request(token: str, path: str, method: str = "GET", json_body=None):
    url = path if path.startswith("http") else API + path
    r = requests.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=json_body,
        timeout=TIMEOUT,
    )
    return r


def _call(session, path: str, method: str = "GET", json_body=None, retry: bool = True):
    """Sequential call bound to the session; refreshes and retries once on 401/429."""
    token = _ensure_token(session)
    r = _request(token, path, method, json_body)

    if r.status_code == 401 and retry:
        session["tokens"] = refresh(session["tokens"]["refresh_token"])
        return _call(session, path, method, json_body, retry=False)
    if r.status_code == 429 and retry:
        time.sleep(min(int(r.headers.get("retry-after", 2) or 2), 10))
        return _call(session, path, method, json_body, retry=False)
    if not r.ok:
        raise RuntimeError(f"spotify {r.status_code} on {path}: {r.text[:300]}")
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def get_me(session) -> dict:
    j = _call(session, "/me")
    return {
        "id": j["id"],
        "name": j.get("display_name") or j["id"],
        "country": j.get("country") or "US",
    }


def is_unheard(ep: dict, played_ids: set | None = None) -> bool:
    """An episode is "unheard backlog" if playback never started and it isn't finished.

    If Spotify returns no resume_point (scope/market quirks) we treat it as unheard —
    degraded but usable; recently-played ids still filter obvious repeats.
    ponytail: resume_point + recently-played is the whole heard-detection heuristic.
    Upgrade path: per-episode /episodes/{id} lookups if false "unheard" hurts picks.
    """
    played_ids = played_ids or set()
    if not ep or ep.get("is_playable") is False:
        return False
    if ep.get("id") in played_ids:
        return False
    rp = ep.get("resume_point") or {}
    return not rp.get("fully_played") and (rp.get("resume_position_ms") or 0) == 0


def _recently_played_episode_ids(session) -> set:
    try:
        j = _call(session, "/me/player/recently-played?limit=50")
        ids = set()
        for it in (j or {}).get("items", []):
            t = it.get("track") or it.get("item") or {}
            if t.get("type") == "episode" and t.get("id"):
                ids.add(t["id"])
        return ids
    except Exception:
        return set()


def get_followed_shows(session, max_shows: int = 50) -> list[dict]:
    """The user's saved/followed shows, newest-saved first. One cheap paginated call."""
    shows = []
    url = "/me/shows?limit=50"
    while url and len(shows) < max_shows:
        j = _call(session, url)
        for it in j.get("items", []):
            shows.append(it["show"])
        url = j.get("next")
    return shows[:max_shows]


def get_backlog(
    session,
    max_shows: int = 50,
    per_show: int = 20,
    market: str = "US",
    selected_ids: set | None = None,
) -> dict:
    """Scan the backlog. `selected_ids=None` means every followed show.

    Excluded shows are filtered out *before* the fan-out, so they cost no episode
    requests and never reach the ranking prompt.
    """
    followed = get_followed_shows(session, max_shows)
    use = [s for s in followed if s["id"] in selected_ids] if selected_ids is not None else followed

    played_ids = _recently_played_episode_ids(session)

    # Refresh once up front, then fan out with a plain token. Flask's session is
    # request-context-bound and not thread-safe, so worker threads must not touch it.
    # A worker that fails anyway can't retry — but it must not vanish silently, or the
    # user gets a triage that looks complete and isn't. Failures come back as `dropped`
    # and are surfaced on the results page.
    token = _ensure_token(session)

    def fetch(show):
        """-> (episodes, dropped_or_None). Never raises; a failed show is reported."""
        try:
            r = _request(token, f"/shows/{show['id']}/episodes?limit={per_show}&market={market}")
            if r.ok:
                eps = [dict(e, _show=show) for e in (r.json().get("items") or []) if e]
                return eps, None
            code, reason = DROP_REASONS.get(
                r.status_code, (f"http_{r.status_code}", f"Spotify error {r.status_code}")
            )
            return [], {"show": show["name"], "id": show["id"], "code": code, "reason": reason}
        except Exception as e:
            return [], {
                "show": show["name"],
                "id": show["id"],
                "code": "network_error",
                "reason": "network error or timeout",
                "error": type(e).__name__,
            }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, use))

    per_show_eps = [eps for eps, _ in results]
    dropped = [d for _, d in results if d]

    episodes = []
    for lst in per_show_eps:
        for e in lst:
            if not is_unheard(e, played_ids):
                continue
            show = e["_show"]
            episodes.append(
                {
                    "id": e["id"],
                    "uri": e["uri"],
                    "title": e["name"],
                    "show": show["name"],
                    "publisher": show.get("publisher"),
                    "description": " ".join((e.get("description") or "").split())[:600],
                    "duration_min": round((e.get("duration_ms") or 0) / 60000),
                    "released": e.get("release_date") or "",
                }
            )

    episodes.sort(key=lambda x: x["released"] or "", reverse=True)
    # showsScanned counts shows actually read; dropped ones are reported separately so
    # "N shows" on the results page never overstates what the picks were chosen from.
    return {
        "episodes": episodes,
        "showsScanned": len(use) - len(dropped),
        "showsSelected": len(use),
        "showsFollowed": len(followed),
        "dropped": dropped,
    }


def create_playlist(session, user_id: str, name: str, description: str, uris: list[str]) -> dict:
    """Create a private playlist and verify it is actually private.

    Spotify does not reliably honour `public: false` at creation time - a playlist
    created this way was confirmed readable with an app-only token, i.e. public. So
    the flag is forced with a follow-up PUT and then read back, and the real state is
    returned rather than assumed. The UI promises privacy; it has to be true.
    """
    pl = _call(
        session,
        f"/users/{quote(user_id)}/playlists",
        method="POST",
        json_body={"name": name, "public": False, "description": description},
    )
    pid = pl["id"]
    if uris:
        _call(session, f"/playlists/{pid}/tracks", method="POST", json_body={"uris": uris})

    try:
        _call(session, f"/playlists/{pid}", method="PUT", json_body={"public": False})
    except Exception:
        pass  # playlist exists either way; the read-back below reports the truth

    is_public = True  # assume the worse of the two until confirmed otherwise
    try:
        is_public = bool((_call(session, f"/playlists/{pid}?fields=public") or {}).get("public"))
    except Exception:
        pass

    return {
        "url": (pl.get("external_urls") or {}).get("spotify"),
        "id": pid,
        "public": is_public,
    }
