"""The V2 user app under /v2: accounts, shows, today's picks, play links, feedback.

No network: the iTunes API and every RSS feed are stubbed. Each test gets its own
DATA_DIR, so its SQLite database never touches real data.
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from views import esc  # noqa: E402
from v2 import backlog, db, itunes, rss  # noqa: E402

PASSWORD = "correct horse battery"

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


class CsrfClient:
    """Test client that sends this session's CSRF token with every /v2 POST, as the
    real forms do. `csrf=False` sends none, to prove the server rejects that."""

    def __init__(self, c):
        self.c = c

    def __getattr__(self, name):
        return getattr(self.c, name)

    def token(self):
        with self.c.session_transaction() as s:
            tok = s.get("v2_csrf")
        if not tok:
            self.c.get("/v2/login")  # any form page issues one
            with self.c.session_transaction() as s:
                tok = s.get("v2_csrf")
        return tok

    def post(self, path, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf and path.startswith("/v2"):
            data.setdefault("csrf_token", self.token())
        return self.c.post(path, data=data, **kw)


@pytest.fixture
def client(monkeypatch, tmp_path, net):
    import app as app_mod

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app_mod.app.config["TESTING"] = True
    return CsrfClient(app_mod.app.test_client())


def signup(c, email="me@example.com", password=PASSWORD):
    return c.post("/v2/signup", data={"email": email, "password": password})


def add(c, *ids):
    for cid in ids:
        assert c.post("/v2/shows/add", data={"itunes_id": cid}).status_code == 302


def run_today(c, minutes):
    r = c.post("/v2/today", data={"minutes": str(minutes)})
    assert r.status_code == 302, r.get_data(as_text=True)
    run_id = r.headers["Location"].rsplit("/", 1)[1]
    uid = db.get_user_by_email("me@example.com")["id"]
    return db.get_run(run_id, uid), run_id


# --- accounts ----------------------------------------------------------------------------

def test_signup_creates_an_account_and_logs_in(client):
    r = signup(client, "Me@Example.com ")
    assert r.status_code == 302 and r.headers["Location"].endswith("/v2/shows")
    user = db.get_user_by_email("me@example.com")
    assert user is not None
    assert PASSWORD not in user["password_hash"], "password must be hashed"
    assert client.get("/v2/shows").status_code == 200


def test_signup_rejects_duplicates_and_weak_passwords(client):
    signup(client)
    client.post("/v2/logout")
    r = signup(client)
    assert r.status_code == 400 and "already exists" in r.get_data(as_text=True)
    assert signup(client, "x@example.com", "short").status_code == 400
    assert signup(client, "not-an-email", PASSWORD).status_code == 400


def test_login_with_the_right_password(client):
    signup(client)
    client.post("/v2/logout")
    assert client.get("/v2/today").status_code == 302, "logged out after logout"
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD})
    assert r.status_code == 302 and r.headers["Location"].endswith("/v2/today")
    assert client.get("/v2/today").status_code == 200


def test_wrong_password_and_unknown_email_get_the_same_answer(client):
    signup(client)
    client.post("/v2/logout")
    wrong = client.post("/v2/login", data={"email": "me@example.com", "password": "nope-nope"})
    nobody = client.post("/v2/login", data={"email": "who@example.com", "password": "nope-nope"})
    assert wrong.status_code == nobody.status_code == 401
    assert "Wrong email or password" in wrong.get_data(as_text=True)
    assert "Wrong email or password" in nobody.get_data(as_text=True)
    assert client.get("/v2/today").status_code == 302


def test_login_is_rate_limited_per_email(client, monkeypatch):
    signup(client)
    signup(client, "other@example.com")
    client.post("/v2/logout")
    for _ in range(5):
        client.post("/v2/login", data={"email": "me@example.com", "password": "wrong-pass"})

    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD})
    assert r.status_code == 429, "even the right password is refused while locked"
    assert "Too many failed attempts" in r.get_data(as_text=True)

    other = client.post("/v2/login", data={"email": "other@example.com", "password": PASSWORD})
    assert other.status_code == 302, "the limit is per email"
    client.post("/v2/logout")

    later = db.time.time() + 16 * 60
    monkeypatch.setattr(db.time, "time", lambda: later)
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD})
    assert r.status_code == 302, "unlocked once the 15-minute window has passed"


def test_logout_leaves_the_v1_session_alone(client):
    signup(client)
    with client.session_transaction() as s:
        s["tokens"] = {"access_token": "v1"}
    client.post("/v2/logout")
    with client.session_transaction() as s:
        assert "v2_user" not in s and s["tokens"] == {"access_token": "v1"}


# --- shows -------------------------------------------------------------------------------

def test_search_add_and_remove_a_show(client):
    signup(client)
    html = client.get("/v2/shows?q=anything").get_data(as_text=True)
    assert "Second Show" in html and 'name="itunes_id" value="102"' in html
    assert "https://img.example/102.jpg" in html

    add(client, "102")
    html = client.get("/v2/shows").get_data(as_text=True)
    assert "My shows (1)" in html and "Second Show" in html
    assert [s["feed_url"] for s in db.list_shows(1)] == ["https://feeds.example/b"]

    client.post("/v2/shows/remove", data={"itunes_id": "102"})
    assert "My shows (0)" in client.get("/v2/shows").get_data(as_text=True)


def test_show_without_a_public_feed_cannot_be_added(client):
    signup(client)
    html = client.get("/v2/shows?q=exclusive").get_data(as_text=True)
    assert "No public feed, can not be analyzed" in html
    assert 'name="itunes_id" value="105"' not in html
    r = client.post("/v2/shows/add", data={"itunes_id": "105"})
    assert r.status_code == 400
    assert db.list_shows(1) == []


def test_feed_url_comes_from_apple_not_from_the_form(client):
    signup(client)
    client.post("/v2/shows/add", data={"itunes_id": "102", "feed_url": "http://127.0.0.1/x"})
    assert db.list_shows(1)[0]["feed_url"] == "https://feeds.example/b"


def test_three_shows_are_required(client):
    signup(client)
    add(client, "101", "102")
    assert "Add at least 3 shows" in client.get("/v2/shows").get_data(as_text=True)
    assert "You need at least 3 shows" in client.get("/v2/today").get_data(as_text=True)
    assert client.post("/v2/today", data={"minutes": "30"}).status_code == 400


# --- today -------------------------------------------------------------------------------

@pytest.mark.parametrize("minutes", [1, 5, 8, 15, 30, 45, 60, 90, 200, 600])
def test_today_never_goes_over_budget(client, minutes):
    signup(client)
    add(client, "101", "102", "103", "104")
    run, _ = run_today(client, minutes)
    picks = run["payload"]["picks"]
    assert sum(p["duration_min"] for p in picks) <= minutes
    ids = {p["id"] for p in picks} | {s["id"] for s in run["payload"]["skips"]}
    assert ids == {"a1", "a2", "b1", "b2", "b3", "c1", "c2", "c3", "d1"}, "30-day window, nothing lost"
    assert run["engine"] == "newest_first" and run["budget"] == minutes


def test_custom_minutes_work(client):
    signup(client)
    add(client, "101", "102", "103")
    r = client.post("/v2/today", data={"minutes": "custom", "custom_minutes": "23"})
    assert r.status_code == 302
    assert client.post("/v2/today", data={"minutes": "custom", "custom_minutes": "9999"}).status_code == 400


def test_heard_episodes_disappear(client):
    signup(client)
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
    signup(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    client.post("/v2/heard", data={"run": run_id, "episode": "never-shown"})
    assert db.heard_ids(1) == set()


def test_play_links_are_escaped_and_url_encoded(client):
    signup(client)
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
    FEEDS_JS = dict(FEEDS)
    FEEDS_JS["https://feeds.example/a"] = make_feed("Show A", [_item("js", "JS", 5, 1, audio="javascript:alert(1)")])
    monkeypatch.setattr(rss, "fetch_feed", lambda url: FEEDS_JS[url])
    signup(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 7)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)
    assert "javascript:" not in html
    assert "No playable audio in this feed" in html


def test_feedback_is_saved_with_picks_budget_and_engine(client):
    signup(client)
    add(client, "101", "102", "103")
    run, run_id = run_today(client, 45)

    client.post("/v2/feedback", data={"run": run_id, "verdict": "maybe-later"})
    assert db.all_feedback() == [], "only yes / somewhat / no"

    r = client.post("/v2/feedback", data={"run": run_id, "verdict": "somewhat"})
    assert r.status_code == 302
    (fb,) = db.all_feedback()
    assert fb["verdict"] == "somewhat" and fb["budget"] == 45 and fb["engine"] == "newest_first"
    import json
    assert [p["id"] for p in json.loads(fb["picks"])] == [p["id"] for p in run["payload"]["picks"]]
    assert "you said &ldquo;somewhat&rdquo;" in client.get(f"/v2/run/{run_id}").get_data(as_text=True)


def test_a_feed_failure_is_shown_not_dropped(client, net):
    signup(client)
    add(client, "101", "102", "103")
    net["fail"].add("https://feeds.example/b")
    _, run_id = run_today(client, 60)
    html = client.get(f"/v2/run/{run_id}").get_data(as_text=True)
    assert "Could not load 1 show: Second Show (TimeoutError: feed timed out)" in html
    assert "C one" in html, "other shows still produce picks"


def test_feeds_are_cached_for_an_hour(client, net, monkeypatch):
    signup(client)
    add(client, "101", "102", "103")
    run_today(client, 30)
    run_today(client, 30)
    assert len(net["fetches"]) == 3, "second run within the hour uses the cache"

    later = db.time.time() + backlog.CACHE_SECONDS + 1
    monkeypatch.setattr(db.time, "time", lambda: later)
    run_today(client, 30)
    assert len(net["fetches"]) == 6


def test_another_users_run_is_not_found(client):
    signup(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    client.post("/v2/logout")
    signup(client, "other@example.com")
    assert client.get(f"/v2/run/{run_id}").status_code == 404
    client.post("/v2/feedback", data={"run": run_id, "verdict": "no"})
    assert db.all_feedback() == []


def test_pages_need_a_login(client):
    for path in ("/v2/shows", "/v2/today", "/v2/run/" + "0" * 32):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/v2/login")


def test_privacy_page_is_linked_and_lists_what_we_store(client):
    assert 'href="/v2/privacy"' in client.get("/v2/").get_data(as_text=True)
    html = client.get("/v2/privacy").get_data(as_text=True)
    for item in ("email", "hashed", "shows", "heard", "feedback", "never sell"):
        assert item in html.lower()


def test_landing_has_sign_up_and_log_in(client):
    html = client.get("/v2").get_data(as_text=True)
    assert 'href="/v2/signup"' in html and 'href="/v2/login"' in html


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
    signup(client)
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
    signup(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert 'href="https://podcasts.apple.com/ep/by-title"' in link
    assert link.endswith(">Open in Apple Podcasts</a>")


def test_no_apple_match_falls_back_to_the_show_page_with_an_honest_label(client, net):
    net["apple"]["101"] = [apple_episode("nope", "Not this one", "https://podcasts.apple.com/ep/x")]
    signup(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert 'href="https://podcasts.apple.com/us/podcast/id101?uo=4&amp;x=1"' in link
    assert link.endswith(">Search on Apple Podcasts</a>")


def test_apple_being_down_never_breaks_a_run(client, net):
    net["apple_down"] = True
    signup(client)
    add(client, "101", "102", "103")
    assert _apple_link_html(client).endswith(">Search on Apple Podcasts</a>")


def test_apple_episode_lookup_is_cached_per_show_for_6_hours(client, net, monkeypatch):
    from v2 import links

    net["apple"]["101"] = [apple_episode("a1", "x", "https://podcasts.apple.com/ep/a1")]
    signup(client)
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
    signup(client)
    add(client, "101", "102", "103")
    link = _apple_link_html(client)
    assert "javascript:" not in link and link.endswith(">Search on Apple Podcasts</a>")


# --- 30-day login -------------------------------------------------------------------------

def _session_cookie(r):
    return next(h for h in r.headers.getlist("Set-Cookie") if h.startswith("podsen="))


def test_login_lasts_30_days_and_log_out_ends_it(client):
    from email.utils import parsedate_to_datetime

    signup(client)
    client.post("/v2/logout")
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD})
    cookie = _session_cookie(r)
    expires = parsedate_to_datetime(cookie.split("Expires=")[1].split(";")[0])
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


# --- delete account -----------------------------------------------------------------------

def test_delete_account_needs_the_password(client):
    signup(client)
    add(client, "101", "102", "103")
    r = client.post("/v2/account/delete", data={"password": "wrong-pass"})
    assert r.status_code == 401 and "was not deleted" in r.get_data(as_text=True)
    assert db.get_user_by_email("me@example.com") is not None


def test_delete_account_removes_the_user_and_all_their_rows(client):
    signup(client, "other@example.com")
    add(client, "101")
    client.post("/v2/logout")

    signup(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    uid = db.get_user_by_email("me@example.com")["id"]
    first = db.get_run(run_id, uid)["payload"]["picks"][0]["id"]
    client.post("/v2/heard", data={"run": run_id, "episode": first})
    client.post("/v2/feedback", data={"run": run_id, "verdict": "yes"})
    assert all(n > 0 for n in db.count_rows(uid).values())

    assert "Delete my account" in client.get("/v2/account").get_data(as_text=True)
    r = client.post("/v2/account/delete", data={"password": PASSWORD})
    assert r.status_code == 200 and "Account deleted" in r.get_data(as_text=True)

    assert db.count_rows(uid) == {"users": 0, "shows": 0, "heard": 0, "runs": 0, "feedback": 0}
    assert client.get("/v2/today").status_code == 302, "logged out"
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD})
    assert r.status_code == 401
    other = db.get_user_by_email("other@example.com")["id"]
    assert db.count_rows(other)["shows"] == 1, "other users are untouched"


def test_privacy_page_explains_deletion(client):
    html = client.get("/v2/privacy").get_data(as_text=True)
    assert "Delete my account" in html and 'href="/v2/account"' in html


# --- CSRF ---------------------------------------------------------------------------------

def test_every_v2_post_without_a_valid_token_is_rejected(client):
    signup(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    posts = [
        ("/v2/signup", {"email": "new@example.com", "password": PASSWORD}),
        ("/v2/login", {"email": "me@example.com", "password": PASSWORD}),
        ("/v2/shows/add", {"itunes_id": "104"}),
        ("/v2/shows/remove", {"itunes_id": "101"}),
        ("/v2/today", {"minutes": "30"}),
        ("/v2/heard", {"run": run_id, "episode": "a1"}),
        ("/v2/feedback", {"run": run_id, "verdict": "yes"}),
        ("/v2/account/delete", {"password": PASSWORD}),
        ("/v2/logout", {}),
    ]
    for path, data in posts:
        assert client.post(path, data=data, csrf=False).status_code == 400, path
        assert client.post(path, data={**data, "csrf_token": "forged"}, csrf=False).status_code == 400, path

    assert db.get_user_by_email("new@example.com") is None
    assert len(db.list_shows(1)) == 3
    assert db.heard_ids(1) == set() and db.all_feedback() == []
    assert db.get_user_by_email("me@example.com") is not None
    assert client.get("/v2/today").status_code == 200, "still logged in"


def test_a_token_from_another_session_is_rejected(client):
    import app as app_mod

    stranger = CsrfClient(app_mod.app.test_client())
    their_token = stranger.token()
    signup(client)
    r = client.post("/v2/shows/add", data={"itunes_id": "101", "csrf_token": their_token}, csrf=False)
    assert r.status_code == 400 and db.list_shows(1) == []


def _forms_carry(html, token, path):
    forms = html.split('<form method="post"')[1:]
    for form in forms:
        assert f'name="csrf_token" value="{token}"' in form.split("</form>")[0], path
    return len(forms)


def test_every_v2_post_form_carries_the_token(client):
    checked = sum(_forms_carry(client.get(p).get_data(as_text=True), client.token(), p)
                  for p in ("/v2/login", "/v2/signup"))
    before_login = client.token()

    signup(client)
    assert client.token() != before_login, "a fresh token is issued at login"
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    for path in ("/v2/shows?q=x", "/v2/shows", "/v2/today", f"/v2/run/{run_id}", "/v2/account"):
        checked += _forms_carry(client.get(path).get_data(as_text=True), client.token(), path)
    assert checked >= 12, "the scan must actually find the forms"


# --- login throttling: per email AND per IP -----------------------------------------------

ATTACKER = {"REMOTE_ADDR": "198.51.100.7"}
VICTIM = {"REMOTE_ADDR": "203.0.113.20"}


def test_someone_else_cannot_lock_you_out(client):
    signup(client)
    client.post("/v2/logout")
    for _ in range(6):
        client.post("/v2/login", data={"email": "me@example.com", "password": "guess-guess"},
                    environ_base=ATTACKER)
    blocked = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD},
                          environ_base=ATTACKER)
    assert blocked.status_code == 429, "the guesser is locked out"

    mine = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD},
                       environ_base=VICTIM)
    assert mine.status_code == 302, "the account owner, elsewhere, is not"


def test_one_ip_cannot_spray_guesses_across_accounts(client):
    signup(client)
    client.post("/v2/logout")
    for i in range(20):
        client.post("/v2/login", data={"email": f"user{i}@example.com", "password": "guess-guess"},
                    environ_base=ATTACKER)
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD},
                    environ_base=ATTACKER)
    assert r.status_code == 429, "20 failures from one IP blocks that IP for every email"
    r = client.post("/v2/login", data={"email": "me@example.com", "password": PASSWORD},
                    environ_base=VICTIM)
    assert r.status_code == 302


def test_old_database_gets_the_ip_column(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = sqlite3.connect(tmp_path / "podsen_v2.db")
    old.execute("CREATE TABLE login_failures (email TEXT NOT NULL, ts REAL NOT NULL)")
    old.execute("INSERT INTO login_failures VALUES ('a@example.com', 1e12)")
    old.commit()
    old.close()
    db.record_failure("a@example.com", "1.2.3.4")
    pair, by_ip = db.recent_failures("a@example.com", "1.2.3.4", 10**13)
    assert len(pair) == 1 and len(by_ip) == 1


# --- data rule ----------------------------------------------------------------------------

def test_v2_package_never_imports_spotify():
    code = (
        "import sys, pkgutil, importlib, v2\n"
        "for m in pkgutil.iter_modules(v2.__path__): importlib.import_module('v2.' + m.name)\n"
        "print(sorted(m for m in sys.modules if m.startswith('v2.')), 'spotify' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert "v2.web" in out.stdout and "v2.engine" in out.stdout, "every V2 module was imported"
    assert out.stdout.strip().endswith("False")
