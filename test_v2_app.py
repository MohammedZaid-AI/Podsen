"""The V2 app under /v2: Google sign-in (Supabase), shows, picks, links, feedback.

No network: Supabase Auth, the iTunes API and every RSS feed are stubbed, and
conftest.py points DATABASE_URL at nothing (SQLite in a temp dir) and every Supabase
setting at fakes. The fake Supabase checks PKCE for real: a code only exchanges when
sha256(verifier) matches the challenge sent at /authorize.
"""

import base64
import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import pytest
import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from conftest import FAKE_ANON_KEY, FAKE_SERVICE_ROLE_KEY, FAKE_SUPABASE_URL  # noqa: E402
from views import esc  # noqa: E402
from v2 import auth, backlog, db, itunes, rss  # noqa: E402

TRICKY_SHOW = 'Q&A "Live" <Show>'
TRICKY_TITLE = 'Ep 1: <script>alert("x")</script> 50% & more'

CATALOG = {
    "101": {"name": TRICKY_SHOW, "feed": "https://feeds.example/a"},
    "102": {"name": "Second Show", "feed": "https://feeds.example/b"},
    "103": {"name": "Third Show", "feed": "https://feeds.example/c"},
    "104": {"name": "Fourth Show", "feed": "https://feeds.example/d"},
    "105": {"name": "Exclusive Show", "feed": None},  # platform exclusive: no public feed
}


def _itunes_item(cid):
    c = CATALOG[cid]
    item = {
        "collectionId": int(cid),
        "collectionName": c["name"],
        "artistName": "Artist " + cid,
        "artworkUrl100": f"https://img.example/{cid}.jpg",
        "collectionViewUrl": f"https://podcasts.apple.com/us/podcast/id{cid}?uo=4&x=1",
    }
    if c["feed"]:
        item["feedUrl"] = c["feed"]
    return item


def apple_episode(guid, title, url):
    return {"wrapperType": "podcastEpisode", "kind": "podcast-episode",
            "episodeGuid": guid, "trackName": title, "trackViewUrl": url}


def _item(guid, title, minutes, days_ago, audio=None):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    audio = audio or f"https://cdn.example/{guid}.mp3?a=1&b=2"
    return f"""<item><title>{esc(title)}</title><guid>{esc(guid)}</guid>
      <pubDate>{format_datetime(when)}</pubDate>
      <itunes:duration>{minutes * 60}</itunes:duration>
      <enclosure url="{esc(audio)}" type="audio/mpeg" length="1"/></item>"""


def make_feed(show, items):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>{esc(show)}</title>{"".join(items)}</channel></rss>""".encode("utf-8")


FEEDS = {
    "https://feeds.example/a": make_feed(TRICKY_SHOW, [
        _item("a1", TRICKY_TITLE, 20, 1),
        _item("a2", "A two", 45, 3),
        _item("a-old", "Too old", 10, 45),  # outside the 30-day window
    ]),
    "https://feeds.example/b": make_feed("Second Show", [
        _item("b1", "B one", 12, 2), _item("b2", "B two", 33, 4), _item("b3", "B three", 61, 6),
    ]),
    "https://feeds.example/c": make_feed("Third Show", [
        _item("c1", "C one", 8, 1), _item("c2", "C two", 25, 9), _item("c3", "C three", 90, 12),
    ]),
    "https://feeds.example/d": make_feed("Fourth Show", [_item("d1", "D one", 15, 2)]),
}


@pytest.fixture
def net(monkeypatch):
    """Stub iTunes + feeds. `net["fail"]` is a set of feed URLs that will error;
    `net["apple"]` maps a show id to its Apple episode list; `net["episode_lookups"]`
    records each per-show episode lookup."""
    state = {"fail": set(), "fetches": [], "apple": {}, "episode_lookups": [], "apple_down": False}

    def fake_itunes(url):
        if "/lookup?" in url:
            cid = url.split("id=")[1].split("&")[0]
            if "entity=podcastEpisode" in url:
                state["episode_lookups"].append(cid)
                if state["apple_down"]:
                    raise TimeoutError("apple timed out")
                return {"results": [_itunes_item(cid)] + state["apple"].get(cid, [])}
            return {"results": [_itunes_item(cid)] if cid in CATALOG else []}
        return {"results": [_itunes_item(cid) for cid in CATALOG]}

    def fake_fetch(url):
        state["fetches"].append(url)
        if url in state["fail"]:
            raise TimeoutError("feed timed out")
        return FEEDS[url]

    monkeypatch.setattr(itunes, "_get_json", fake_itunes)
    monkeypatch.setattr(rss, "fetch_feed", fake_fetch)
    return state


# --- fake Supabase Auth ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class FakeSupabase:
    """/token?grant_type=pkce and /admin/users/<id>, with real PKCE checking."""

    def __init__(self):
        self.codes, self.used = {}, set()
        self.token_calls, self.admin_calls = [], []
        self.down = False          # raise like an unreachable host
        self.status_500 = False    # answer 500
        self.admin_status = 200

    def issue(self, challenge, user):
        code = uuid.uuid4().hex
        self.codes[code] = (challenge, user)
        return code

    def post(self, url, params=None, json=None, headers=None, timeout=None, **kw):
        self.token_calls.append({"url": url, "params": params, "json": json, "headers": headers})
        if self.down:
            raise requests.ConnectionError("connection refused")
        if self.status_500:
            return FakeResponse(500, {"msg": "internal"})
        assert url == f"{FAKE_SUPABASE_URL}/auth/v1/token" and params == {"grant_type": "pkce"}
        code = json["auth_code"]
        if code not in self.codes or code in self.used:
            return FakeResponse(404, {"error_code": "flow_state_not_found", "msg": "invalid flow state"})
        challenge, user = self.codes[code]
        if _s256(json["code_verifier"]) != challenge:
            return FakeResponse(400, {"error_code": "bad_code_verifier", "msg": "code challenge does not match"})
        self.used.add(code)
        return FakeResponse(200, {"token_type": "bearer", "user": user})

    def delete(self, url, headers=None, timeout=None, **kw):
        self.admin_calls.append({"url": url, "headers": headers})
        if self.down:
            raise requests.ConnectionError("connection refused")
        return FakeResponse(self.admin_status, {})


class Client:
    """Flask test client that sends the CSRF token with /v2 POSTs (as the real forms
    do; `csrf=False` sends none) and records every response for the secret-leak test."""

    def __init__(self, c, supa):
        self.c, self.supa, self.responses = c, supa, []

    def __getattr__(self, name):
        return getattr(self.c, name)

    def _keep(self, r):
        self.responses.append(r)
        return r

    def token(self):
        with self.c.session_transaction() as s:
            tok = s.get("v2_csrf")
        if not tok:
            self.c.get("/v2/privacy")  # any page issues one
            with self.c.session_transaction() as s:
                tok = s.get("v2_csrf")
        return tok

    def get(self, *a, **kw):
        return self._keep(self.c.get(*a, **kw))

    def post(self, path, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf and path.startswith("/v2"):
            data.setdefault("csrf_token", self.token())
        return self._keep(self.c.post(path, data=data, **kw))


@pytest.fixture
def supa(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(auth.requests, "post", fake.post)
    monkeypatch.setattr(auth.requests, "delete", fake.delete)
    return fake


@pytest.fixture
def client(net, supa):
    import app as app_mod

    app_mod.app.config["TESTING"] = True
    return Client(app_mod.app.test_client(), supa)


def user_id_for(email):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "podsen-test:" + email))


def login(c, email="me@example.com"):
    """Sign in through the whole Google/Supabase PKCE flow. Returns the user id."""
    r = c.get("/v2/login")
    assert r.status_code == 302 and r.headers["Location"].startswith(FAKE_SUPABASE_URL), r.headers
    challenge = parse_qs(urlparse(r.headers["Location"]).query)["code_challenge"][0]
    uid = user_id_for(email)
    code = c.supa.issue(challenge, {"id": uid, "email": email})
    r = c.get(f"/v2/auth/callback?code={code}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/v2/today"), r.get_data(as_text=True)
    return uid


def add(c, *ids):
    for cid in ids:
        assert c.post("/v2/shows/add", data={"itunes_id": cid}).status_code == 302


def run_today(c, minutes, email="me@example.com"):
    r = c.post("/v2/today", data={"minutes": str(minutes)})
    assert r.status_code == 302, r.get_data(as_text=True)
    run_id = r.headers["Location"].rsplit("/", 1)[1]
    return db.get_run(run_id, user_id_for(email)), run_id


def logged_in_as(c):
    with c.c.session_transaction() as s:
        return s.get("v2_user")


# --- Google sign-in through Supabase -----------------------------------------------------------

def test_login_redirect_carries_a_valid_s256_code_challenge(client):
    r = client.get("/v2/login")
    assert r.status_code == 302
    loc = urlparse(r.headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{FAKE_SUPABASE_URL}/auth/v1/authorize"
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["provider"] == "google"
    assert q["redirect_to"] == "http://localhost/v2/auth/callback"
    assert q["code_challenge_method"] == "s256"

    with client.c.session_transaction() as s:
        verifier = s["v2_pkce"]
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert q["code_challenge"] == _s256(verifier), "challenge must be base64url(sha256(verifier))"
    assert len(q["code_challenge"]) == 43 and "=" not in q["code_challenge"]
    assert FAKE_ANON_KEY not in r.headers["Location"] and FAKE_SERVICE_ROLE_KEY not in r.headers["Location"]


def test_each_login_gets_a_fresh_verifier(client):
    client.get("/v2/login")
    with client.c.session_transaction() as s:
        first = s["v2_pkce"]
    client.get("/v2/login")
    with client.c.session_transaction() as s:
        assert s["v2_pkce"] != first


def test_callback_url_is_built_from_the_request_host(client):
    r = client.get("/v2/login", base_url="https://podsen.example.org")
    q = parse_qs(urlparse(r.headers["Location"]).query)
    assert q["redirect_to"] == ["https://podsen.example.org/v2/auth/callback"]
    # behind a tunnel, ProxyFix applies the forwarded host and scheme
    r = client.get("/v2/login", headers={"X-Forwarded-Host": "abc.trycloudflare.com",
                                         "X-Forwarded-Proto": "https"})
    q = parse_qs(urlparse(r.headers["Location"]).query)
    assert q["redirect_to"] == ["https://abc.trycloudflare.com/v2/auth/callback"]


def test_callback_logs_in_with_the_supabase_user_id(client, supa):
    uid = login(client, "Me@Example.com")
    assert logged_in_as(client) == uid
    assert db.get_user(uid)["email"] == "me@example.com"
    with client.c.session_transaction() as s:
        assert "v2_pkce" not in s, "the verifier is single use"
    (call,) = supa.token_calls
    assert call["headers"]["apikey"] == FAKE_ANON_KEY, "the exchange uses the anon key"
    assert FAKE_SERVICE_ROLE_KEY not in json.dumps(call)
    assert client.get("/v2/today").status_code == 200


def test_signing_in_again_keeps_the_same_account(client):
    uid = login(client)
    add(client, "101")
    client.post("/v2/logout")
    assert login(client) == uid
    assert len(db.list_shows(uid)) == 1


def test_bad_or_reused_code_fails_clearly(client, supa):
    client.get("/v2/login")
    r = client.get("/v2/auth/callback?code=never-issued")
    assert r.status_code == 400
    assert "already used or has expired" in r.get_data(as_text=True)
    assert logged_in_as(client) is None

    login(client)
    client.post("/v2/logout")
    (used_code,) = supa.used
    client.get("/v2/login")
    r = client.get(f"/v2/auth/callback?code={used_code}")
    assert r.status_code == 400 and "already used or has expired" in r.get_data(as_text=True)
    assert logged_in_as(client) is None


def test_missing_verifier_fails_clearly(client, supa):
    # e.g. the sign-in was started in another browser, or the cookie was lost
    code = supa.issue("whatever", {"id": user_id_for("me@example.com"), "email": "me@example.com"})
    r = client.get(f"/v2/auth/callback?code={code}")
    assert r.status_code == 400
    assert "started in another browser or took too long" in r.get_data(as_text=True)
    assert supa.token_calls == [], "no exchange is attempted without a verifier"
    assert logged_in_as(client) is None


def test_wrong_verifier_is_rejected_by_pkce(client, supa):
    r = client.get("/v2/login")
    challenge = parse_qs(urlparse(r.headers["Location"]).query)["code_challenge"][0]
    code = supa.issue(challenge, {"id": user_id_for("me@example.com"), "email": "me@example.com"})
    with client.c.session_transaction() as s:
        s["v2_pkce"] = "a-different-verifier-" + "x" * 40  # e.g. someone else's code
    r = client.get(f"/v2/auth/callback?code={code}")
    assert r.status_code == 400 and "match this browser" in r.get_data(as_text=True)
    assert logged_in_as(client) is None


def test_user_cancelling_at_google_is_explained(client):
    client.get("/v2/login")
    r = client.get("/v2/auth/callback?error=access_denied&error_description=user+denied")
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "You cancelled signing in with Google" in html and "Continue with Google" in html
    with client.c.session_transaction() as s:
        assert "v2_pkce" not in s


def test_other_provider_errors_are_shown_escaped(client):
    client.get("/v2/login")
    r = client.get("/v2/auth/callback?error=server_error&error_description=<script>x</script>")
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "<script>x</script>" not in html and "&lt;script&gt;" in html


@pytest.mark.parametrize("mode", ["down", "status_500"])
def test_supabase_down_fails_clearly(client, supa, mode):
    r = client.get("/v2/login")
    challenge = parse_qs(urlparse(r.headers["Location"]).query)["code_challenge"][0]
    code = supa.issue(challenge, {"id": "u1", "email": "me@example.com"})
    setattr(supa, mode, True)
    r = client.get(f"/v2/auth/callback?code={code}")
    assert r.status_code == 503
    html = r.get_data(as_text=True)
    assert "try again in a minute" in html.lower()
    assert logged_in_as(client) is None


def test_missing_supabase_settings_are_reported(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    r = client.get("/v2/login")
    assert r.status_code == 503 and "SUPABASE_URL is missing" in r.get_data(as_text=True)


def test_no_password_pages_remain(client):
    assert client.get("/v2/signup").status_code == 404
    assert client.c.post("/v2/login", data={"email": "a@b.c", "password": "x"}).status_code in (400, 405)
    html = client.get("/v2/").get_data(as_text=True)
    assert 'href="/v2/login">Continue with Google</a>' in html
    assert 'type="password"' not in html and "/v2/signup" not in html


# --- shows -------------------------------------------------------------------------------

def test_search_add_and_remove_a_show(client):
    uid = login(client)
    html = client.get("/v2/shows?q=anything").get_data(as_text=True)
    assert "Second Show" in html and 'name="itunes_id" value="102"' in html
    assert "https://img.example/102.jpg" in html

    add(client, "102")
    html = client.get("/v2/shows").get_data(as_text=True)
    assert "My shows (1)" in html and "Second Show" in html
    assert [s["feed_url"] for s in db.list_shows(uid)] == ["https://feeds.example/b"]

    client.post("/v2/shows/remove", data={"itunes_id": "102"})
    assert "My shows (0)" in client.get("/v2/shows").get_data(as_text=True)


def test_show_without_a_public_feed_cannot_be_added(client):
    uid = login(client)
    html = client.get("/v2/shows?q=exclusive").get_data(as_text=True)
    assert "No public feed, can not be analyzed" in html
    assert 'name="itunes_id" value="105"' not in html
    r = client.post("/v2/shows/add", data={"itunes_id": "105"})
    assert r.status_code == 400
    assert db.list_shows(uid) == []


def test_feed_url_comes_from_apple_not_from_the_form(client):
    uid = login(client)
    client.post("/v2/shows/add", data={"itunes_id": "102", "feed_url": "http://127.0.0.1/x"})
    assert db.list_shows(uid)[0]["feed_url"] == "https://feeds.example/b"


def test_three_shows_are_required(client):
    login(client)
    add(client, "101", "102")
    assert "Add at least 3 shows" in client.get("/v2/shows").get_data(as_text=True)
    assert "You need at least 3 shows" in client.get("/v2/today").get_data(as_text=True)
    assert client.post("/v2/today", data={"minutes": "30"}).status_code == 400


# --- today -------------------------------------------------------------------------------

@pytest.mark.parametrize("minutes", [1, 5, 8, 15, 30, 45, 60, 90, 200, 600])
def test_today_never_goes_over_budget(client, minutes):
    login(client)
    add(client, "101", "102", "103", "104")
    run, _ = run_today(client, minutes)
    picks = run["payload"]["picks"]
    assert sum(p["duration_min"] for p in picks) <= minutes
    ids = {p["id"] for p in picks} | {s["id"] for s in run["payload"]["skips"]}
    assert ids == {"a1", "a2", "b1", "b2", "b3", "c1", "c2", "c3", "d1"}, "30-day window, nothing lost"
    assert run["engine"] == "newest_first" and run["budget"] == minutes


def test_custom_minutes_work(client):
    login(client)
    add(client, "101", "102", "103")
    r = client.post("/v2/today", data={"minutes": "custom", "custom_minutes": "23"})
    assert r.status_code == 302
    assert client.post("/v2/today", data={"minutes": "custom", "custom_minutes": "9999"}).status_code == 400


def test_heard_episodes_disappear(client):
    login(client)
    add(client, "101", "102", "103")
    run, run_id = run_today(client, 30)
    first = run["payload"]["picks"][0]["id"]

    r = client.post("/v2/heard", data={"run": run_id, "episode": first})
    assert r.status_code == 302
    assert "Marked as heard" in client.get(f"/v2/run/{run_id}").get_data(as_text=True)

    again, _ = run_today(client, 600)
    seen = {e["id"] for e in again["payload"]["picks"] + again["payload"]["skips"]}
    assert first not in seen


def test_only_shown_episodes_can_be_marked_heard(client):
    uid = login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    client.post("/v2/heard", data={"run": run_id, "episode": "never-shown"})
    assert db.heard_ids(uid) == set()


def test_play_links_are_escaped_and_url_encoded(client):
    login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 21)  # newest that fits: a1 (20 min, tricky title)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)

    assert "<script>alert" not in html
    assert esc(TRICKY_TITLE) in html and esc(TRICKY_SHOW) in html

    query = f"{TRICKY_SHOW} {TRICKY_TITLE}"
    spotify = "https://open.spotify.com/search/" + quote(query, safe="")
    youtube = ("https://www.youtube.com/results?search_query="
               + quote_plus(f'"{TRICKY_TITLE}" {TRICKY_SHOW}'))
    amazon = "https://music.amazon.com/search/" + quote(query, safe="")
    for link in (spotify, youtube, amazon):
        assert f'href="{esc(link)}"' in html, link
        assert "<" not in link and '"' not in link and " " not in link
    assert "%3Cscript%3E" in spotify and "%22Ep+1" in youtube and "%26" in amazon

    assert "Search on YouTube" in html and "Open in YouTube" not in html
    assert "Search on Amazon Music" in html
    assert '<audio controls preload="none" src="https://cdn.example/a1.mp3?a=1&amp;b=2">' in html
    # no Apple episode data in this test: honest fallback to the show page
    assert ('href="https://podcasts.apple.com/us/podcast/id101?uo=4&amp;x=1" target="_blank" '
            'rel="noopener">Search on Apple Podcasts</a>') in html
    assert "Open in Apple Podcasts" not in html


def test_non_http_audio_is_never_rendered(client, monkeypatch):
    feeds_js = dict(FEEDS)
    feeds_js["https://feeds.example/a"] = make_feed("Show A", [_item("js", "JS", 5, 1, audio="javascript:alert(1)")])
    monkeypatch.setattr(rss, "fetch_feed", lambda url: feeds_js[url])
    login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 7)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)
    assert "javascript:" not in html
    assert "No playable audio in this feed" in html


def test_feedback_is_saved_with_picks_budget_and_engine(client):
    login(client)
    add(client, "101", "102", "103")
    run, run_id = run_today(client, 45)

    client.post("/v2/feedback", data={"run": run_id, "verdict": "maybe-later"})
    assert db.all_feedback() == [], "only yes / somewhat / no"

    r = client.post("/v2/feedback", data={"run": run_id, "verdict": "somewhat"})
    assert r.status_code == 302
    (fb,) = db.all_feedback()
    assert fb["verdict"] == "somewhat" and fb["budget"] == 45 and fb["engine"] == "newest_first"
    assert [p["id"] for p in json.loads(fb["picks"])] == [p["id"] for p in run["payload"]["picks"]]
    assert "you said &ldquo;somewhat&rdquo;" in client.get(f"/v2/run/{run_id}").get_data(as_text=True)


def test_a_feed_failure_is_shown_not_dropped(client, net):
    login(client)
    add(client, "101", "102", "103")
    net["fail"].add("https://feeds.example/b")
    _, run_id = run_today(client, 60)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)
    assert "Could not load 1 show: Second Show (TimeoutError: feed timed out)" in html
    assert "C one" in html, "other shows still produce picks"


def test_feeds_are_cached_for_an_hour(client, net, monkeypatch):
    login(client)
    add(client, "101", "102", "103")
    run_today(client, 30)
    run_today(client, 30)
    assert len(net["fetches"]) == 3, "second run within the hour uses the cache"

    later = db.time.time() + backlog.CACHE_SECONDS + 1
    monkeypatch.setattr(db.time, "time", lambda: later)
    run_today(client, 30)
    assert len(net["fetches"]) == 6


def test_another_users_run_is_not_found(client):
    login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    client.post("/v2/logout")
    login(client, "other@example.com")
    assert client.get(f"/v2/run/{run_id}").status_code == 404
    client.post("/v2/feedback", data={"run": run_id, "verdict": "no"})
    assert db.all_feedback() == []


def test_pages_need_a_login(client):
    for path in ("/v2/shows", "/v2/today", "/v2/account", "/v2/run/" + "0" * 32):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/v2/"), path


def test_privacy_page_says_google_and_supabase(client):
    assert 'href="/v2/privacy"' in client.get("/v2/").get_data(as_text=True)
    html = client.get("/v2/privacy").get_data(as_text=True).lower()
    for item in ("email address, from google", "supabase", "shows", "heard", "feedback",
                 "never sell", "type delete"):
        assert item in html, item
    assert "password, hashed" not in html and "failed log-in" not in html


# --- Apple Podcasts: exact episode, or say it's a search --------------------------------

def _apple_link_html(client, minutes=21):
    _, run_id = run_today(client, minutes)  # 21 min -> the one pick is a1 (TRICKY_TITLE)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)
    return html.split("Apple Podcasts</a>")[0].rsplit("<a ", 1)[1] + "Apple Podcasts</a>"


def test_apple_guid_match_wins_over_a_title_match(client, net):
    net["apple"]["101"] = [
        # same title, different guid: listed first, must still lose to the guid match
        apple_episode("other-guid", TRICKY_TITLE, "https://podcasts.apple.com/ep/by-title?i=1"),
        apple_episode("a1", "A different title on Apple", "https://podcasts.apple.com/ep/by-guid?i=2&x=3"),
    ]
    login(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert 'href="https://podcasts.apple.com/ep/by-guid?i=2&amp;x=3"' in link
    assert link.endswith(">Open in Apple Podcasts</a>")


def test_apple_title_fallback_when_no_guid_matches(client, net):
    net["apple"]["101"] = [
        apple_episode("unrelated", "Something else", "https://podcasts.apple.com/ep/wrong"),
        apple_episode("apple-only-guid", "  " + TRICKY_TITLE.upper() + "  ",
                      "https://podcasts.apple.com/ep/by-title"),
    ]
    login(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert 'href="https://podcasts.apple.com/ep/by-title"' in link
    assert link.endswith(">Open in Apple Podcasts</a>")


def test_no_apple_match_falls_back_to_the_show_page_with_an_honest_label(client, net):
    net["apple"]["101"] = [apple_episode("nope", "Not this one", "https://podcasts.apple.com/ep/x")]
    login(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert 'href="https://podcasts.apple.com/us/podcast/id101?uo=4&amp;x=1"' in link
    assert link.endswith(">Search on Apple Podcasts</a>")


def test_apple_being_down_never_breaks_a_run(client, net):
    net["apple_down"] = True
    login(client)
    add(client, "101", "102", "103")
    assert _apple_link_html(client).endswith(">Search on Apple Podcasts</a>")


def test_apple_episode_lookup_is_cached_per_show_for_6_hours(client, net, monkeypatch):
    from v2 import links

    net["apple"]["101"] = [apple_episode("a1", "x", "https://podcasts.apple.com/ep/a1")]
    login(client)
    add(client, "101", "102", "103")
    run_today(client, 21)
    run_today(client, 21)
    assert net["episode_lookups"] == ["101"], "one lookup per show, then the cache"

    later = db.time.time() + links.APPLE_CACHE_SECONDS + 1
    monkeypatch.setattr(db.time, "time", lambda: later)
    run_today(client, 21)
    assert net["episode_lookups"] == ["101", "101"]


def test_non_https_apple_episode_urls_are_ignored(client, net):
    net["apple"]["101"] = [apple_episode("a1", "x", "javascript:alert(1)")]
    login(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert "javascript:" not in link and link.endswith(">Search on Apple Podcasts</a>")


# --- 30-day login ---------------------------------------------------------------------------

def _session_cookie(r):
    return next(h for h in r.headers.getlist("Set-Cookie") if h.startswith("podsen="))


def test_login_lasts_30_days_and_log_out_ends_it(client, supa):
    from email.utils import parsedate_to_datetime

    r = client.get("/v2/login")
    challenge = parse_qs(urlparse(r.headers["Location"]).query)["code_challenge"][0]
    code = supa.issue(challenge, {"id": user_id_for("me@example.com"), "email": "me@example.com"})
    r = client.get(f"/v2/auth/callback?code={code}")
    expires = parsedate_to_datetime(_session_cookie(r).split("Expires=")[1].split(";")[0])
    days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 29.9 < days <= 30.01

    nav = client.get("/v2/today").get_data(as_text=True)
    assert 'action="/v2/logout"' in nav and "Log out" in nav

    r = client.post("/v2/logout")
    cookie = _session_cookie(r)
    if "Expires=" in cookie:  # an emptied session is deleted outright (Expires in 1970)
        gone = parsedate_to_datetime(cookie.split("Expires=")[1].split(";")[0])
        assert gone < datetime.now(timezone.utc), "no 30-day cookie left after log out"
    assert client.get("/v2/today").status_code == 302


# --- delete account --------------------------------------------------------------------------

def test_delete_needs_the_word_delete(client, supa):
    uid = login(client)
    add(client, "101", "102", "103")
    for wrong in ("", "delete", "yes", "DELETE ME"):
        r = client.post("/v2/account/delete", data={"confirm": wrong})
        assert r.status_code == 400 and "Nothing was deleted" in r.get_data(as_text=True), wrong
    assert db.count_rows(uid)["shows"] == 3
    assert supa.admin_calls == []


def test_delete_removes_all_rows_and_the_supabase_auth_user(client, supa):
    other = login(client, "other@example.com")
    add(client, "101")
    client.post("/v2/logout")

    uid = login(client)
    add(client, "101", "102", "103")
    run, run_id = run_today(client, 30)
    client.post("/v2/heard", data={"run": run_id, "episode": run["payload"]["picks"][0]["id"]})
    client.post("/v2/feedback", data={"run": run_id, "verdict": "yes"})
    assert all(n > 0 for n in db.count_rows(uid).values())

    assert "Type DELETE to confirm" in client.get("/v2/account").get_data(as_text=True)
    r = client.post("/v2/account/delete", data={"confirm": " DELETE "})
    assert r.status_code == 200 and "Account deleted" in r.get_data(as_text=True)

    assert db.count_rows(uid) == {"users": 0, "shows": 0, "heard": 0, "runs": 0, "feedback": 0}
    (call,) = supa.admin_calls
    assert call["url"] == f"{FAKE_SUPABASE_URL}/auth/v1/admin/users/{uid}"
    assert call["headers"]["apikey"] == FAKE_SERVICE_ROLE_KEY
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_SERVICE_ROLE_KEY}"
    assert logged_in_as(client) is None and client.get("/v2/today").status_code == 302
    assert db.count_rows(other)["shows"] == 1, "other users are untouched"


def test_delete_says_so_when_the_supabase_user_could_not_be_removed(client, supa):
    uid = login(client)
    add(client, "101", "102", "103")
    supa.admin_status = 500
    r = client.post("/v2/account/delete", data={"confirm": "DELETE"})
    html = r.get_data(as_text=True)
    assert r.status_code == 502 and "Account data deleted" in html
    assert "refused to remove your login" in html
    assert db.count_rows(uid)["shows"] == 0, "our rows are deleted either way"


# --- secrets: the service-role key never leaves the server ------------------------------------

def test_service_role_key_never_appears_in_any_response_or_log(client, supa, net, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    client.get("/v2/")
    client.get("/v2/privacy")
    login(client)
    add(client, "101", "102", "103")
    client.get("/v2/shows?q=x")
    run, run_id = run_today(client, 30)
    client.get(f"/v2/run/{run_id}")
    client.post("/v2/feedback", data={"run": run_id, "verdict": "yes"})
    client.get("/v2/account")
    client.get("/admin?key=s3cret")
    client.get("/v2/auth/callback?code=bad")             # error page
    supa.admin_status = 500
    client.post("/v2/account/delete", data={"confirm": "DELETE"})  # admin call fails
    login(client)
    supa.down = True
    client.post("/v2/account/delete", data={"confirm": "DELETE"})  # Supabase unreachable

    assert len(client.responses) >= 15
    for r in client.responses:
        assert FAKE_SERVICE_ROLE_KEY not in r.get_data(as_text=True), r.request.path
        assert FAKE_SERVICE_ROLE_KEY not in str(r.headers), r.request.path
    assert FAKE_SERVICE_ROLE_KEY not in caplog.text
    assert supa.admin_calls, "the key was really in use during this test"


# --- CSRF ---------------------------------------------------------------------------------

def test_every_v2_post_without_a_valid_token_is_rejected(client, supa):
    uid = login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    posts = [
        ("/v2/shows/add", {"itunes_id": "104"}),
        ("/v2/shows/remove", {"itunes_id": "101"}),
        ("/v2/today", {"minutes": "30"}),
        ("/v2/heard", {"run": run_id, "episode": "a1"}),
        ("/v2/feedback", {"run": run_id, "verdict": "yes"}),
        ("/v2/account/delete", {"confirm": "DELETE"}),
        ("/v2/logout", {}),
    ]
    for path, data in posts:
        assert client.post(path, data=data, csrf=False).status_code == 400, path
        assert client.post(path, data={**data, "csrf_token": "forged"}, csrf=False).status_code == 400, path

    assert len(db.list_shows(uid)) == 3
    assert db.heard_ids(uid) == set() and db.all_feedback() == []
    assert db.get_user(uid) is not None and supa.admin_calls == []
    assert client.get("/v2/today").status_code == 200, "still logged in"


def test_a_token_from_another_session_is_rejected(client):
    import app as app_mod

    stranger = Client(app_mod.app.test_client(), client.supa)
    their_token = stranger.token()
    uid = login(client)
    r = client.post("/v2/shows/add", data={"itunes_id": "101", "csrf_token": their_token}, csrf=False)
    assert r.status_code == 400 and db.list_shows(uid) == []


def _forms_carry(html, token, path):
    forms = html.split('<form method="post"')[1:]
    for form in forms:
        assert f'name="csrf_token" value="{token}"' in form.split("</form>")[0], path
    return len(forms)


def test_every_v2_post_form_carries_the_token(client):
    before_login = client.token()
    login(client)
    assert client.token() != before_login, "a fresh token is issued at login"
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    checked = 0
    for path in ("/v2/shows?q=x", "/v2/shows", "/v2/today", f"/v2/run/{run_id}", "/v2/account"):
        checked += _forms_carry(client.get(path).get_data(as_text=True), client.token(), path)
    assert checked >= 12, "the scan must actually find the forms"


# --- database: SQLite in tests, Postgres in production ---------------------------------------

def test_tests_run_on_sqlite_in_the_temp_data_dir(client, tmp_path):
    login(client)
    eng = db.engine()
    assert eng.dialect.name == "sqlite"
    assert (tmp_path / "podsen_v2.db").is_file()


@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    ("postgresql://u:p@h:6543/postgres", "postgresql+psycopg://u:p@h:6543/postgres"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    # raw special characters in the password, as Supabase's dashboard pastes them
    ("postgresql://postgres.ref:pa@ss#w/rd?@aws-0-x.pooler.supabase.com:6543/postgres",
     "postgresql+psycopg://postgres.ref:pa%40ss%23w%2Frd%3F@aws-0-x.pooler.supabase.com:6543/postgres"),
    # already encoded: left as is, never encoded twice
    ("postgresql://u:pa%40ss@h:5432/db", "postgresql+psycopg://u:pa%40ss@h:5432/db"),
])
def test_database_url_uses_the_psycopg_driver(monkeypatch, raw, expected):
    monkeypatch.setenv("DATABASE_URL", raw)
    assert db.database_url() == expected


def test_a_raw_at_sign_in_the_password_still_parses_to_the_right_host(monkeypatch):
    from sqlalchemy.engine import make_url

    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:p@ss@db.ref.supabase.co:5432/postgres")
    u = make_url(db.database_url())
    assert (u.host, u.port, u.username, u.password) == ("db.ref.supabase.co", 5432, "postgres", "p@ss")


def test_an_old_password_era_sqlite_file_is_refused_clearly(tmp_path):
    old = sqlite3.connect(tmp_path / "podsen_v2.db")
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, created REAL)")
    old.commit()
    old.close()
    with pytest.raises(RuntimeError, match="old email\\+password version"):
        db.engine()


def test_setup_sql_creates_every_table_and_locks_it_with_rls():
    sql = (ROOT / "v2" / "supabase_setup.sql").read_text(encoding="utf-8")
    for t in db.metadata.sorted_tables:
        assert f"CREATE TABLE IF NOT EXISTS {t.name} (" in sql, t.name
        assert f'ALTER TABLE "{t.name}" ENABLE ROW LEVEL SECURITY;' in sql, t.name
    assert "CREATE POLICY" not in sql.upper(), "no policies: the anon key gets nothing"


# --- copying the old SQLite data across --------------------------------------------------------

def _old_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, created REAL);
    CREATE TABLE login_failures (email TEXT, ts REAL, ip TEXT);
    CREATE TABLE shows (user_id INTEGER, itunes_id TEXT, name TEXT, artist TEXT, feed_url TEXT,
                        artwork_url TEXT, apple_url TEXT, added REAL);
    CREATE TABLE heard (user_id INTEGER, episode_id TEXT, ts REAL);
    CREATE TABLE runs (id TEXT, user_id INTEGER, ts REAL, budget INTEGER, engine TEXT, payload TEXT);
    CREATE TABLE feedback (id INTEGER PRIMARY KEY, run_id TEXT, user_id INTEGER, ts REAL,
                           verdict TEXT, budget INTEGER, engine TEXT, picks TEXT);
    INSERT INTO users VALUES (1, 'Me@Example.com', 'scrypt:x', 1.0), (2, 'gone@example.com', 'scrypt:y', 1.0);
    INSERT INTO shows VALUES (1, '101', 'A', 'x', 'https://feeds.example/a', NULL, NULL, 1.0),
                             (2, '102', 'B', 'y', 'https://feeds.example/b', NULL, NULL, 1.0);
    INSERT INTO heard VALUES (1, 'a1', 2.0);
    INSERT INTO runs VALUES ('r1', 1, 3.0, 30, 'newest_first', '{"picks": [], "skips": []}');
    INSERT INTO feedback VALUES (7, 'r1', 1, 4.0, 'yes', 30, 'newest_first', '[]');
    """)
    c.commit()
    c.close()


def test_migration_copies_old_data_to_the_google_account_by_email(tmp_path, monkeypatch):
    from v2 import migrate_sqlite

    source = tmp_path / "old.db"
    _old_db(source)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    db.upsert_user("google-uuid-1", "me@example.com")  # signed in with Google once
    lines = []

    counts = migrate_sqlite.migrate(source, out=lines.append)
    assert (counts["shows"], counts["heard"], counts["runs"], counts["feedback"]) == (1, 1, 1, 1)
    assert [s["itunes_id"] for s in db.list_shows("google-uuid-1")] == ["101"]
    assert db.heard_ids("google-uuid-1") == {"a1"}
    assert db.get_run("r1", "google-uuid-1") is not None
    assert [f["verdict"] for f in db.all_feedback()] == ["yes"]
    assert any("gone@example.com: SKIPPED" in line for line in lines), "unmatched accounts are reported"

    again = migrate_sqlite.migrate(source, out=lines.append)
    assert (again["shows"], again["heard"], again["runs"], again["feedback"]) == (0, 0, 0, 0)
    assert len(db.all_feedback()) == 1, "running twice copies nothing twice"


def test_migration_dry_run_changes_nothing(tmp_path, monkeypatch):
    from v2 import migrate_sqlite

    source = tmp_path / "old.db"
    _old_db(source)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    db.upsert_user("google-uuid-1", "me@example.com")
    counts = migrate_sqlite.migrate(source, dry_run=True, out=lambda *_: None)
    assert counts["shows"] == 1 and db.list_shows("google-uuid-1") == []


def test_migration_refuses_without_a_target(tmp_path, monkeypatch):
    from v2 import migrate_sqlite

    source = tmp_path / "old.db"
    _old_db(source)
    with pytest.raises(SystemExit, match="DATABASE_URL is not set"):
        migrate_sqlite.migrate(source, out=lambda *_: None)


# --- home page, removed V1 routes, /admin ----------------------------------------------

def test_home_page_redirects_to_the_v2_app(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/v2/")


@pytest.mark.parametrize(
    "method,path",
    [("get", "/login"), ("get", "/callback"), ("get", "/app"), ("get", "/shows/all"),
     ("post", "/triage"), ("post", "/confirm"), ("post", "/feedback"), ("get", "/logout")],
)
def test_old_v1_routes_are_gone(client, method, path):
    assert getattr(client.c, method)(path).status_code == 404


def test_admin_needs_the_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    assert client.get("/admin").status_code == 403
    assert client.get("/admin?key=wrong").status_code == 403
    monkeypatch.setenv("ADMIN_KEY", "")
    assert client.get("/admin?key=").status_code == 403


def test_admin_shows_v2_runs_and_feedback_without_personal_data(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    login(client)
    add(client, "101", "102", "103")
    run, run_id = run_today(client, 30)
    client.post("/v2/feedback", data={"run": run_id, "verdict": "yes"})

    r = client.get("/admin?key=s3cret")
    assert r.status_code == 200 and r.mimetype == "application/json"
    data = json.loads(r.get_data(as_text=True))
    (row,) = data["runs"]
    assert row["id"] == run_id and row["budget"] == 30 and row["engine"] == "newest_first"
    assert [p["id"] for p in row["picks"]] == [p["id"] for p in run["payload"]["picks"]]
    assert [f["verdict"] for f in data["feedback"]] == ["yes"]
    assert "me@example.com" not in r.get_data(as_text=True)


# --- page shell (views.page) ---------------------------------------------------------------

import views  # noqa: E402


def test_page_renders_tagline_in_two_parts():
    """The tagline is one sentence split into an italic lede and an upright rest."""
    html = views.page("Podsen", "<p>hi</p>")
    assert f'<span class="lede">{views.HERO_LEDE}</span>' in html
    assert f'<span class="rest">{views.HERO_REST}</span>' in html
    # reassembling the spans must give back the original sentence
    assert f"{views.HERO_LEDE} {views.HERO_REST}" == "Listen to what's worth it."


def test_hero_only_on_the_landing_page():
    """Display type is for the hero; inner pages keep it at brand size."""
    hero = views.page("Podsen", "<p>hi</p>", hero=True)
    inner = views.page("Podsen", "<p>hi</p>")
    assert '<h1 class="hero">' in hero and '<p class="wordmark">' in hero
    assert '<h1 class="hero">' not in inner and '<p class="tagline">' in inner


def test_every_page_has_exactly_one_h1():
    for html in (views.page("Podsen", "<p>hi</p>", hero=True), views.page("Podsen", "<p>hi</p>")):
        assert html.count("<h1") == 1


def test_accent_colour_is_a_variable_not_a_literal():
    html = views.page("Podsen", "<p>hi</p>", hero=True)
    assert "--accent-coral: #E8637A;" in html
    assert "color: var(--accent-coral);" in html
    # the coral must not be hardcoded anywhere outside the token definition
    assert html.count("#E8637A") == 1


# --- data rule ----------------------------------------------------------------------------

def test_v2_package_never_imports_spotify():
    code = (
        "import sys, pkgutil, importlib, v2\n"
        "for m in pkgutil.iter_modules(v2.__path__): importlib.import_module('v2.' + m.name)\n"
        "print(sorted(m for m in sys.modules if m.startswith('v2.')), 'spotify' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert "v2.web" in out.stdout and "v2.auth" in out.stdout, "every V2 module was imported"
    assert out.stdout.strip().endswith("False")
