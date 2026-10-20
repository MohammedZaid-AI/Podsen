"""Speed and responsiveness: the timing log, one pooled connection per request,
stale-while-revalidate caches, and the one progressive-enhancement script.

No network: reuses the fake Supabase, Apple and feeds from test_v2_app.py.
"""

import logging
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import views  # noqa: E402
from conftest import FAKE_ANON_KEY, FAKE_SERVICE_ROLE_KEY  # noqa: E402
from test_v2_app import add, client, login, net, run_today, supa, user_id_for  # noqa: E402,F401
from v2 import backlog, db, links, refresh, rss, trending  # noqa: E402

LINE = re.compile(r"^(GET|POST) (/[^ ?]*) (\d{3}) (\d+)ms db=(\d+)q/(\d+)ms conn=(\d+)/new(\d+) "
                  r"feeds=(\d+)/(\d+)ms apple=(\d+)/(\d+)ms$")


@pytest.fixture
def timing_lines():
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("podsen.timing")
    h = Grab()
    log.addHandler(h)
    yield lines
    log.removeHandler(h)


# --- the timing log ---------------------------------------------------------------------------

def test_timing_log_has_one_line_per_request_and_no_secrets_or_emails(client, supa, net, timing_lines,
                                                                      monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "admin-key-s3cret-value")
    uid = login(client, "secret.person@example.com")             # ?code= in the callback URL
    add(client, "101", "102", "103")
    client.get("/v2/shows?q=history")
    _, run_id = run_today(client, 30, email="secret.person@example.com")
    client.get(f"/v2/run/{run_id}")
    client.get("/admin?key=admin-key-s3cret-value")
    client.get("/v2/auth/callback?code=leaky-code-123&error=x&error_description=y")
    client.post("/v2/account/delete", data={"confirm": "DELETE"})

    assert len(timing_lines) == len(client.responses), "one line per request"
    text = "\n".join(timing_lines)
    for secret in ("secret.person@example.com", "secret.person", "admin-key-s3cret-value", "leaky-code-123",
                   FAKE_SERVICE_ROLE_KEY, FAKE_ANON_KEY, "history", "csrf_token", uid):
        assert secret not in text, secret
    assert "?" not in text, "never a query string"
    for line in timing_lines:
        assert LINE.match(line), line


def test_timing_line_counts_db_feeds_and_apple(client, supa, net, timing_lines):
    login(client)
    add(client, "101", "102", "103")
    client.get("/v2/shows")                 # caches the trending chart (2 Apple calls, once)
    client.get("/v2/shows?q=history")
    search = timing_lines[-1]
    m = LINE.match(search)
    assert m and int(m.group(11)) == 1, "one Apple search counted"
    assert int(m.group(5)) >= 1, "its database query counted"


# --- connection pool ---------------------------------------------------------------------------

def test_one_pooled_connection_per_request_and_the_pool_is_reused(client, supa, net, timing_lines):
    login(client)
    add(client, "101", "102", "103")
    engine = db.engine()
    start = len(timing_lines)
    for _ in range(4):
        for path in ("/v2/today", "/v2/shows", "/v2/account"):
            client.get(path)
    _, run_id = run_today(client, 30)
    client.get(f"/v2/run/{run_id}")
    assert db.engine() is engine, "same engine (and pool) across requests"
    for line in timing_lines[start:]:
        m = LINE.match(line)
        assert int(m.group(7)) <= 1, f"at most one pool checkout per request: {line}"
        assert int(m.group(8)) == 0, f"no new connection once the pool is warm: {line}"


def test_pages_need_few_queries(client, supa, net, timing_lines):
    login(client)
    add(client, "101", "102", "103")
    _, run_id = run_today(client, 30)
    client.get("/v2/shows")                 # first visit stores the trending chart
    queries = {}
    for path in ("/v2/today", "/v2/shows", "/v2/account", f"/v2/run/{run_id}"):
        client.get(path)
        queries[path.split("/")[2]] = int(LINE.match(timing_lines[-1]).group(5))
    client.post("/v2/today", data={"minutes": "30"})
    queries["POST today"] = int(LINE.match(timing_lines[-1]).group(5))
    assert queries == {"today": 1, "shows": 1, "account": 1, "run": 1, "POST today": 2}, queries


# --- stale-while-revalidate ---------------------------------------------------------------------

def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the request itself made a network call")

    monkeypatch.setattr(rss, "fetch_feed", boom)


def test_a_stale_feed_is_served_from_cache_without_a_network_call(client, supa, net, monkeypatch, timing_lines):
    login(client)
    add(client, "101", "102", "103")           # warms the caches (inline in tests)
    later = db.time.time() + backlog.CACHE_SECONDS + 600
    monkeypatch.setattr(db.time, "time", lambda: later)   # every feed is now stale
    monkeypatch.setattr(refresh, "MODE", "record")
    _no_network(monkeypatch)

    run, _ = run_today(client, 30)
    assert run["payload"]["picks"], "picks come from the stale cache"
    assert "feeds=0/" in timing_lines[-1]
    assert {k for k in refresh.recorded if k[0] == "feed"} == {
        ("feed", "https://feeds.example/a"), ("feed", "https://feeds.example/b"), ("feed", "https://feeds.example/c")}


def test_background_refresh_updates_the_cache(client, supa, net, monkeypatch):
    login(client)
    add(client, "101", "102", "103")
    later = db.time.time() + backlog.CACHE_SECONDS + 600
    monkeypatch.setattr(db.time, "time", lambda: later)
    before = len(net["fetches"])
    run_today(client, 30)                      # inline mode: the refresh runs at once
    assert len(net["fetches"]) == before + 3
    _, age = db.cache_entry("feed", "https://feeds.example/a")
    assert age < 5, "refreshed"


def test_only_a_missing_cache_makes_the_user_wait(client, supa, net, monkeypatch, timing_lines):
    login(client)
    monkeypatch.setattr(refresh, "MODE", "record")  # adding a show warms nothing now
    add(client, "101", "102", "103")
    run_today(client, 30)
    assert "feeds=3/" in timing_lines[-1], "no cache at all: fetched while the user waits"
    run_today(client, 30)
    assert "feeds=0/" in timing_lines[-1]


def test_stale_apple_lists_and_trending_are_served_and_refreshed_in_the_background(client, supa, net,
                                                                                  monkeypatch):
    login(client)
    client.get("/v2/shows")                    # caches the trending chart
    add(client, "101", "102", "103")
    later = db.time.time() + max(links.APPLE_CACHE_SECONDS, trending.CACHE_SECONDS) + 600
    monkeypatch.setattr(db.time, "time", lambda: later)
    monkeypatch.setattr(refresh, "MODE", "record")
    lookups, charts = len(net["episode_lookups"]), len(net["top_calls"])
    run_today(client, 21)
    html = client.get("/v2/shows").get_data(as_text=True)
    assert "Trending" in html and 'value="104"' in html, "stale chart still shown"
    assert len(net["episode_lookups"]) == lookups and len(net["top_calls"]) == charts, "no waiting"
    kinds = {k[0] for k in refresh.recorded}
    assert {"apple", "trending"} <= kinds


def test_a_refresh_is_never_queued_twice(monkeypatch):
    monkeypatch.setattr(refresh, "MODE", "thread")
    started = []
    import threading

    gate = threading.Event()
    assert refresh.schedule(("feed", "x"), lambda: (started.append(1), gate.wait(5)))
    assert not refresh.schedule(("feed", "x"), lambda: started.append(2)), "already in flight"
    gate.set()


def test_the_cache_keeps_only_recent_episodes_without_descriptions():
    from datetime import date

    parsed = {"show": "S", "episodes": [
        {"id": "new", "released": "2026-10-10", "description": "long text", "duration_min": 10},
        {"id": "old", "released": "2025-01-01", "description": "x", "duration_min": 10},
        {"id": "undated", "released": None, "duration_min": 10}]}
    out = backlog.for_cache(parsed, today=date(2026, 10, 17))
    assert [e["id"] for e in out["episodes"]] == ["new"]
    assert "description" not in out["episodes"][0]
    assert out["undated"] == 1


# --- the one script, and forms that work without it ----------------------------------------------

class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.scripts, self.handlers, self.cur = [], 0, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.handlers += [k for k in a if k.startswith("on")]
        if tag == "script":
            self.scripts += 1
        if tag == "form":
            self.cur = {"attrs": a, "inputs": [], "buttons": []}
            self.forms.append(self.cur)
        elif self.cur is not None and tag in ("input", "button"):
            (self.cur["buttons"] if tag == "button" or a.get("type") == "submit" else self.cur["inputs"]).append(a)

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None


def test_the_script_is_present_once_and_under_30_lines():
    js = views.PROGRESSIVE_JS
    body = js.split("<script>")[1].split("</script>")[0]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert 0 < len(lines) < 30, len(lines)
    page = views.page("Podsen", "<p>x</p>")
    # Two inline scripts, no libraries: busy-state on forms, and the install button.
    assert page.count("<script") == 2 and "src=" not in js + views.INSTALL_JS
    assert js in page and views.INSTALL_JS in page
    assert "pageshow" in js and "submit" in js and "Working..." in js


def test_the_install_script_is_small_and_only_reveals_things():
    js = views.INSTALL_JS
    body = js.split("<script>")[1].split("</script>")[0]
    assert 0 < len([ln for ln in body.splitlines() if ln.strip()]) < 15
    assert "beforeinstallprompt" in js and ".prompt()" in js
    # Never touches forms, never fetches, never registers a service worker.
    for banned in ("form", "fetch(", "serviceWorker", "XMLHttpRequest", "localStorage"):
        assert banned not in body, banned


def test_today_time_buttons_say_finding_your_picks(client, supa, net):
    login(client)
    add(client, "101", "102", "103")
    html = client.get("/v2/today").get_data(as_text=True)
    form = html.split('action="/v2/today"')[1].split("</form>")[0]
    assert 'data-busy-note="Finding your picks..."' in form
    assert '<p class="busy-note" hidden aria-live="polite"></p>' in form


def test_buttons_have_pressed_hover_and_focus_states():
    css = views.page("Podsen", "")
    for state in ("button:active", "button:hover", "button:focus-visible", ".btn:active", ".btn:focus-visible"):
        assert state in css, state


def _pages(client):
    uid = login(client)
    add(client, "101", "102")                       # under 3 shows: trending form on Today
    pages = {"today_trending": client.get("/v2/today").get_data(as_text=True)}
    add(client, "103")
    _, run_id = run_today(client, 21)
    for name, path in (("today", "/v2/today"), ("shows", "/v2/shows?q=x"), ("run", f"/v2/run/{run_id}"),
                       ("account", "/v2/account"), ("privacy", "/v2/privacy")):
        pages[name] = client.get(path).get_data(as_text=True)
    return uid, pages


def test_every_form_is_a_plain_html_form(client, supa, net):
    _, pages = _pages(client)
    checked = 0
    for name, html in pages.items():
        p = Forms()
        p.feed(html)
        assert p.scripts == 2 and not p.handlers, f"{name}: no inline handlers, only the two scripts"
        for f in p.forms:
            method = f["attrs"].get("method", "get").lower()
            assert f["attrs"].get("action", "").startswith("/v2"), (name, f["attrs"])
            assert any(b.get("type", "submit") == "submit" for b in f["buttons"]), (name, f["attrs"])
            if method == "post":
                assert any(i.get("name") == "csrf_token" for i in f["inputs"]), (name, f["attrs"])
            checked += 1
    assert checked >= 15


def _form_data(f) -> dict:
    """What a browser with JavaScript off sends: the form's own fields, the first
    checkbox ticked, and the pressed (first) submit button's name and value."""
    data = {i["name"]: i.get("value", "") for i in f["inputs"]
            if i.get("name") and i.get("type") in ("hidden", None, "text", "number")}
    if f["attrs"]["action"] == "/v2/account/delete":
        data["confirm"] = "DELETE"
    ticks = [i for i in f["inputs"] if i.get("type") == "checkbox"]
    if ticks:
        data[ticks[0]["name"]] = ticks[0]["value"]
    button = next(b for b in f["buttons"] if b.get("type", "submit") == "submit")
    if button.get("name"):
        data[button["name"]] = button.get("value", "")
    return data


def _post_forms(client, html, skip=()):
    p = Forms()
    p.feed(html)
    out = []
    for f in p.forms:
        action = f["attrs"]["action"]
        if f["attrs"].get("method", "get").lower() != "post" or action in skip:
            continue
        if action in ("/v2/logout", "/v2/account/delete") and action in out:
            continue  # the session is gone after the first one (Account has two Log out forms)
        r = client.post(action, data=_form_data(f), csrf=False)  # only the form's own csrf field
        assert r.status_code in (200, 302), (action, r.status_code)
        out.append(action)
    return out


def test_every_form_still_posts_without_javascript(client, supa, net):
    """Submit each form exactly as a browser with JS off would."""
    _, pages = _pages(client)
    ending = ("/v2/logout", "/v2/account/delete")  # these end the session: last, on their own
    posted = []
    for name in ("run", "today", "today_trending", "shows"):
        posted += _post_forms(client, pages[name], skip=ending)
    posted += _post_forms(client, client.get("/v2/account").get_data(as_text=True),
                          skip=("/v2/account/delete",))          # the account page's Log out
    login(client)
    posted += _post_forms(client, client.get("/v2/account").get_data(as_text=True),
                          skip=("/v2/logout",))                  # then Delete account
    for action in ("/v2/heard", "/v2/feedback", "/v2/today", "/v2/shows/add-many", "/v2/shows/remove",
                   "/v2/logout", "/v2/account/delete"):
        assert action in posted, action


# --- episode descriptions: stored once, in the background, never read on the Today path ---------

def _feed_with_descriptions(n_desc="v1"):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    def item(guid, title, days, desc):
        when = format_datetime(datetime.now(timezone.utc) - timedelta(days=days))
        return (f"<item><title>{title}</title><guid>{guid}</guid><pubDate>{when}</pubDate>"
                f"<itunes:duration>1200</itunes:duration><description>{desc}</description>"
                f'<enclosure url="https://cdn.example/{guid}.mp3" type="audio/mpeg" length="1"/></item>')

    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
            'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Desc Show</title>'
            + item("d1", "Recent one", 1, f"About the first thing ({n_desc})")
            + item("d2", "Old one", 400, f"An old episode ({n_desc})")
            + "</channel></rss>").encode()


def _desc_feeds(monkeypatch, version="v1"):
    from test_v2_app import FEEDS

    feeds = dict(FEEDS)
    feeds["https://feeds.example/a"] = _feed_with_descriptions(version)
    monkeypatch.setattr(rss, "fetch_feed", lambda url: feeds[url])


def test_a_refresh_stores_descriptions_once(client, supa, net, monkeypatch):
    _desc_feeds(monkeypatch, "v1")
    login(client)
    add(client, "101", "102", "103")                   # warms the cache: background refresh (inline here)
    url = "https://feeds.example/a"
    got = db.episode_descriptions(url, ["d1", "d2"])
    assert got == {"d1": "About the first thing (v1)", "d2": "An old episode (v1)"}, \
        "every episode, not just the recent ones the feed cache keeps"
    # the feed cache itself stays description-free
    cached, _ = db.cache_entry("feed", url)
    assert all("description" not in e for e in cached["episodes"])

    # a later refresh with edited text neither duplicates nor rewrites a stored row
    _desc_feeds(monkeypatch, "v2")
    backlog.refresh_feed(url)
    assert db.episode_descriptions(url, ["d1", "d2"])["d1"] == "About the first thing (v1)"
    from sqlalchemy import func, select

    with db.engine().connect() as c:
        n = c.execute(select(func.count()).select_from(db.episodes).where(db.episodes.c.feed_url == url)).scalar()
    assert n == 2


def test_a_cache_miss_queues_the_description_write_instead_of_waiting(client, supa, net, monkeypatch):
    _desc_feeds(monkeypatch)
    login(client)
    monkeypatch.setattr(refresh, "MODE", "record")     # nothing runs in the background now
    add(client, "101", "102", "103")
    refresh.recorded.clear()
    run_today(client, 30)                              # no cache: feeds fetched while waiting
    queued = {k for k in refresh.recorded if k[0] == "episodes"}
    assert ("episodes", "https://feeds.example/a") in queued and len(queued) == 3
    assert db.episode_descriptions("https://feeds.example/a", ["d1"]) == {}, "not written inline"


def test_the_today_path_never_reads_the_episodes_table(client, supa, net, monkeypatch):
    from sqlalchemy import event

    _desc_feeds(monkeypatch)
    login(client)
    add(client, "101", "102", "103")
    monkeypatch.setattr(refresh, "MODE", "record")     # background work is not the Today path
    statements = []
    eng = db.engine()
    listener = lambda conn, cursor, statement, *a: statements.append(statement)  # noqa: E731
    event.listen(eng, "before_cursor_execute", listener)
    try:
        client.get("/v2/today")
        run, run_id = run_today(client, 30)
        client.get(f"/v2/run/{run_id}")
        later = db.time.time() + backlog.CACHE_SECONDS + 600   # stale caches, too
        monkeypatch.setattr(db.time, "time", lambda: later)
        run_today(client, 30)
    finally:
        event.remove(eng, "before_cursor_execute", listener)
    assert statements, "the scan saw the queries"
    hits = [s for s in statements if re.search(r"\bepisodes\b", s)]
    assert hits == [], hits  # apple_episodes_cache does not match \bepisodes\b


def test_episodes_table_is_in_the_setup_sql_with_rls():
    sql = (ROOT / "v2" / "supabase_setup.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS episodes (" in sql
    assert "PRIMARY KEY (feed_url, guid)" in sql
    assert 'ALTER TABLE "episodes" ENABLE ROW LEVEL SECURITY;' in sql
