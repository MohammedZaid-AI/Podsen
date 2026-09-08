"""Port of test.mjs plus coverage for the two-backend rank layer.

Transports are stubbed here so both backends can be exercised without an API key or
a running Ollama. That is test-double wiring, not product mock data -- the app's own
flow still refuses to show anything that didn't come from real Spotify calls.
"""

import json
import os
import sys
import time
import types

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))

import rank as rank_mod  # noqa: E402
import spotify  # noqa: E402
import store  # noqa: E402
import views  # noqa: E402


# --- unheard detection (ported 1:1 from test.mjs) ----------------------------

@pytest.mark.parametrize(
    "ep,played,expected",
    [
        ({"id": "a", "resume_point": {"fully_played": False, "resume_position_ms": 0}}, None, True),
        ({"id": "a", "resume_point": {"fully_played": True, "resume_position_ms": 0}}, None, False),
        ({"id": "a", "resume_point": {"fully_played": False, "resume_position_ms": 12000}}, None, False),
        ({"id": "a", "is_playable": False, "resume_point": {}}, None, False),
        ({"id": "a"}, None, True),  # no resume data -> degraded "unheard"
        ({"id": "a", "resume_point": {}}, {"a"}, False),  # filtered by recently-played
    ],
)
def test_is_unheard(ep, played, expected):
    assert spotify.is_unheard(ep, played) is expected


# --- model-output JSON extraction --------------------------------------------

def test_extract_json_tolerates_chatter():
    t = 'Sure:\n{"picks":[{"n":1,"reason":"r"}],"skips":[]}\nThat\'s it.'
    assert rank_mod.extract_json(t)["picks"][0]["n"] == 1


def test_extract_json_raises_without_json():
    with pytest.raises(ValueError):
        rank_mod.extract_json("no json here")


def test_extract_json_ignores_braces_in_surrounding_prose():
    """Reasoning models emit braces in their prose; a first-{..last-} slice breaks."""
    t = (
        'Let me think. The shape is {n, reason} for each entry.\n'
        '{"picks":[{"n":2,"reason":"r"}],"skips":[]}\n'
        'Done. (schema was {picks, skips})'
    )
    assert rank_mod.extract_json(t)["picks"][0]["n"] == 2


def test_extract_json_rejects_object_without_picks_or_skips():
    with pytest.raises(ValueError):
        rank_mod.extract_json('{"unrelated": 1}')


# --- hydration: index -> episode, and the pick/skip overlap guard -------------

CANDIDATES = [
    {"id": "e1", "title": "One", "show": "S", "duration_min": 20},
    {"id": "e2", "title": "Two", "show": "S", "duration_min": 30},
    {"id": "e3", "title": "Three", "show": "S", "duration_min": 40},
]


def test_parse_maps_indices_and_drops_bad_ones():
    text = json.dumps(
        {
            "picks": [{"n": 1, "reason": "because one"}, {"n": 99, "reason": "out of range"}],
            "skips": [{"n": 3, "reason": "keeps"}, {"n": 2, "reason": ""}],
        }
    )
    out = rank_mod.parse(text, CANDIDATES)
    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert out["picks"][0]["reason"] == "because one"
    assert [s["id"] for s in out["skips"]] == ["e3"]  # n=99 dropped, empty reason dropped


def test_parse_never_lists_an_episode_as_both_pick_and_skip():
    text = json.dumps(
        {"picks": [{"n": 1, "reason": "a"}], "skips": [{"n": 1, "reason": "b"}, {"n": 2, "reason": "c"}]}
    )
    out = rank_mod.parse(text, CANDIDATES)
    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert [s["id"] for s in out["skips"]] == ["e2"]


# --- backend selection --------------------------------------------------------

def test_default_backend_is_ollama(monkeypatch):
    """No API key required out of the box: the local backend is the default."""
    monkeypatch.delenv("RANK_BACKEND", raising=False)
    assert rank_mod._backend_name() == "ollama"


def test_claude_is_never_reached_without_being_asked_for(monkeypatch):
    """An absent ANTHROPIC_API_KEY must not break the default path."""
    monkeypatch.delenv("RANK_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def boom():
        raise AssertionError("Claude client must not be constructed on the ollama path")

    monkeypatch.setattr(rank_mod, "_anthropic", boom)
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    monkeypatch.setattr(
        rank_mod.requests, "post", lambda *a, **k: FakeOllama({"message": {"content": ANSWER}})
    )
    assert [p["id"] for p in rank_mod.rank(CANDIDATES, 30)["picks"]] == ["e1"]


def test_claude_without_a_key_says_so_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(rank_mod, "_client", None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        rank_mod._anthropic()


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "gpt5")
    with pytest.raises(RuntimeError, match="not supported"):
        rank_mod._backend_name()


def test_both_backends_registered():
    assert set(rank_mod._BACKENDS) == {"claude", "ollama"}


ANSWER = json.dumps({"picks": [{"n": 1, "reason": "why one"}], "skips": [{"n": 2, "reason": "keeps"}]})


def test_claude_backend_end_to_end(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "claude")
    seen = {}

    class FakeMessages:
        def create(self, **kw):
            seen.update(kw)
            block = types.SimpleNamespace(type="text", text=ANSWER)
            return types.SimpleNamespace(content=[block])

    monkeypatch.setattr(rank_mod, "_anthropic", lambda: types.SimpleNamespace(messages=FakeMessages()))
    out = rank_mod.rank(CANDIDATES, 30)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert [s["id"] for s in out["skips"]] == ["e2"]
    assert seen["model"] == rank_mod.ANTHROPIC_MODEL
    assert seen["system"] == rank_mod.SYSTEM
    assert "30 minutes" in seen["messages"][0]["content"]


class FakeOllama:
    """Minimal stand-in for a native /api/chat response."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload

    def json(self):
        return self._payload


def test_ollama_backend_end_to_end(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["json"] = kw["json"]
        return FakeOllama({"done_reason": "stop", "message": {"content": ANSWER}})

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    out = rank_mod.rank(CANDIDATES, 45)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    # native endpoint, not /v1 — only this one honours num_ctx
    assert seen["url"].endswith("/api/chat")
    assert seen["json"]["model"] == rank_mod.OLLAMA_MODEL


def test_ollama_sets_num_ctx_per_request(monkeypatch):
    """The context fix is baked into the request, not left to a machine-wide env var."""
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw["json"])
        return FakeOllama({"done_reason": "stop", "message": {"content": ANSWER}})

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    rank_mod.rank(CANDIDATES, 30)

    assert seen["options"]["num_ctx"] == rank_mod.OLLAMA_NUM_CTX
    assert rank_mod.OLLAMA_NUM_CTX > 4096  # above Ollama's silent default
    assert seen["format"] == "json"


def test_ollama_base_url_strips_legacy_v1_suffix():
    assert rank_mod._normalize_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert rank_mod._normalize_base("http://host:11434/") == "http://host:11434"
    assert rank_mod._normalize_base("http://host:11434") == "http://host:11434"


def test_ollama_context_overflow_gives_an_actionable_error(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    monkeypatch.setattr(
        rank_mod.requests,
        "post",
        lambda *a, **k: FakeOllama(
            {
                "done_reason": "length",
                "prompt_eval_count": 8000,
                "eval_count": 192,
                "message": {"content": "", "thinking": "..."},
            }
        ),
    )
    with pytest.raises(RuntimeError, match="OLLAMA_NUM_CTX"):
        rank_mod.rank(CANDIDATES, 30)


def test_ollama_falls_back_to_thinking_when_content_empty(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    monkeypatch.setattr(
        rank_mod.requests,
        "post",
        lambda *a, **k: FakeOllama({"done_reason": "stop", "message": {"content": "", "thinking": ANSWER}}),
    )
    assert [p["id"] for p in rank_mod.rank(CANDIDATES, 30)["picks"]] == ["e1"]


# --- preflight: fail with something the user can act on ----------------------

def test_preflight_explains_when_ollama_is_down(monkeypatch):
    monkeypatch.setattr(rank_mod, "_preflight_done", False)

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(rank_mod.requests, "get", boom)
    with pytest.raises(RuntimeError, match="Can't reach Ollama"):
        rank_mod._preflight()


def test_preflight_explains_when_model_not_pulled(monkeypatch):
    monkeypatch.setattr(rank_mod, "_preflight_done", False)
    monkeypatch.setattr(
        rank_mod.requests,
        "get",
        lambda *a, **k: types.SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"models": [{"name": "something-else:1b"}]}
        ),
    )
    with pytest.raises(RuntimeError, match=f"ollama pull {rank_mod.OLLAMA_MODEL}"):
        rank_mod._preflight()


def test_preflight_passes_when_model_present(monkeypatch):
    monkeypatch.setattr(rank_mod, "_preflight_done", False)
    monkeypatch.setattr(
        rank_mod.requests,
        "get",
        lambda *a, **k: types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": rank_mod.OLLAMA_MODEL}]},
        ),
    )
    rank_mod._preflight()  # must not raise


# --- malformed JSON: one corrective retry before giving up -------------------

def test_bad_json_is_retried_once_and_succeeds(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["messages"][-1]["content"])
        # first reply has an unescaped quote inside the reason — exactly the
        # failure seen from gemma4 in live testing
        broken = '{"picks":[{"n":1,"reason":"he said "hello" loudly"}],"skips":[]}'
        payload = broken if len(calls) == 1 else ANSWER
        return FakeOllama({"done_reason": "stop", "message": {"content": payload}})

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    out = rank_mod.rank(CANDIDATES, 30)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert len(calls) == 2, "should have retried exactly once"
    assert "could not be parsed as JSON" in calls[1], "retry must carry a corrective hint"
    assert "could not be parsed" not in calls[0], "first attempt must be the clean prompt"


def test_bad_json_twice_fails_with_a_clear_message(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    calls = []

    def fake_post(url, **kw):
        calls.append(1)
        return FakeOllama({"done_reason": "stop", "message": {"content": "sorry, no idea"}})

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="unparseable JSON twice"):
        rank_mod.rank(CANDIDATES, 30)
    assert len(calls) == 2, "exactly one retry, then give up"


def test_retry_also_covers_the_claude_backend(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "claude")
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw["messages"][0]["content"])
            payload = "not json at all" if len(calls) == 1 else ANSWER
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=payload)])

    monkeypatch.setattr(
        rank_mod, "_anthropic", lambda: types.SimpleNamespace(messages=FakeMessages())
    )
    assert [p["id"] for p in rank_mod.rank(CANDIDATES, 30)["picks"]] == ["e1"]
    assert len(calls) == 2


def test_backends_send_different_backlog_sizes():
    """The local model gets a smaller prompt than Claude; both honour their LIMITS."""
    many = [dict(CANDIDATES[0], id=f"e{i}") for i in range(80)]
    c_max, c_chars = rank_mod.LIMITS["claude"]
    o_max, o_chars = rank_mod.LIMITS["ollama"]

    c_cands, _ = rank_mod.build_prompt(many, 30, c_max, c_chars)
    o_cands, _ = rank_mod.build_prompt(many, 30, o_max, o_chars)

    assert len(c_cands) == c_max and len(o_cands) == o_max
    assert o_max <= c_max and o_chars <= c_chars, "local prompt must not exceed Claude's"


# --- event store: format must stay readable by the old Node events.jsonl ------

def test_store_roundtrip_and_line_format(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DIR", tmp_path)
    store.append("events.jsonl", {"sid": "x", "type": "triage", "minutes": 30})
    store.append("events.jsonl", {"sid": "x", "type": "feedback", "useful": "yes"})

    rows = store.read_all("events.jsonl")
    assert [r["type"] for r in rows] == ["triage", "feedback"]

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2
    first = json.loads(raw[0])
    assert list(first)[0] == "ts"  # ts leads the object, as in the Node version
    assert first["ts"].endswith("Z") and "T" in first["ts"]


def test_store_read_all_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DIR", tmp_path)
    assert store.read_all("nope.jsonl") == []


def test_store_preserves_unicode_unescaped(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DIR", tmp_path)
    store.append("events.jsonl", {"title": "Trump drinks Venezuela’s milkshake"})
    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "’" in raw  # not ’


# --- time budget is enforced in code, not left to the model ---------------------
# Regression: a live 15-minute request returned a 61-minute episode, and a 60-minute
# request returned 119 minutes of audio. The model does not respect hard numbers.

BACKLOG = [
    {"id": "s1", "title": "Short", "show": "A", "duration_min": 12, "description": "d"},
    {"id": "m1", "title": "Medium", "show": "B", "duration_min": 28, "description": "d"},
    {"id": "l1", "title": "Long", "show": "C", "duration_min": 61, "description": "d"},
    {"id": "x1", "title": "Epic", "show": "D", "duration_min": 95, "description": "d"},
]


def test_budget_filter_excludes_anything_longer_than_the_budget():
    fitting, shortest = rank_mod.fit_to_budget(BACKLOG, 15)
    assert [e["id"] for e in fitting] == ["s1"]  # the 61-min episode must not survive
    assert shortest is None


def test_budget_filter_allows_a_small_overrun_only():
    fitting, _ = rank_mod.fit_to_budget(BACKLOG, 60)
    ids = [e["id"] for e in fitting]
    assert "l1" in ids, "61 min for a 60 min budget is within tolerance"
    assert "x1" not in ids, "95 min for a 60 min budget is not"


def test_budget_filter_reports_the_shortest_when_nothing_fits():
    fitting, shortest = rank_mod.fit_to_budget(BACKLOG, 5)
    assert fitting == [] and shortest == 12


def test_budget_filter_ignores_episodes_with_no_duration():
    fitting, _ = rank_mod.fit_to_budget([{"id": "z", "duration_min": 0}] + BACKLOG, 30)
    assert "z" not in [e["id"] for e in fitting]


def test_rank_never_returns_a_pick_over_budget(monkeypatch):
    """The end-to-end guarantee: 15 minutes in, nothing longer than 15 minutes out."""
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    # model tries to pick everything it is shown, including anything long
    greedy = json.dumps({"picks": [{"n": 1, "reason": "r"}, {"n": 2, "reason": "r"}],
                         "skips": [{"n": 3, "reason": "k"}]})
    monkeypatch.setattr(
        rank_mod.requests, "post",
        lambda *a, **k: FakeOllama({"done_reason": "stop", "message": {"content": greedy}}),
    )
    out = rank_mod.rank(BACKLOG, 15)
    assert all(p["duration_min"] <= 15 * rank_mod.BUDGET_TOLERANCE for p in out["picks"])
    assert "l1" not in [p["id"] for p in out["picks"]]


def test_rank_says_nothing_fits_instead_of_overshooting(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")

    def must_not_call(*a, **k):
        raise AssertionError("model must not be called when nothing fits")

    monkeypatch.setattr(rank_mod.requests, "post", must_not_call)
    out = rank_mod.rank(BACKLOG, 5)
    assert out["picks"] == [] and out["skips"] == []
    assert out["shortest_unfit"] == 12


def test_model_only_ever_sees_episodes_that_fit(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    monkeypatch.setattr(rank_mod, "_preflight", lambda: None)
    seen = {}

    def spy(url, **kw):
        seen["prompt"] = kw["json"]["messages"][-1]["content"]
        return FakeOllama({"done_reason": "stop", "message": {"content": ANSWER}})

    monkeypatch.setattr(rank_mod.requests, "post", spy)
    rank_mod.rank(BACKLOG, 30)
    assert "Epic" not in seen["prompt"] and "Long" not in seen["prompt"]
    assert "Short" in seen["prompt"] and "Medium" in seen["prompt"]


def test_nothing_fits_page_tells_the_user_the_shortest_option():
    html = views.nothing_fits(15, 47, 44)
    assert "Nothing in your backlog fits 15 minutes" in html
    assert "47 min" in html


# --- playlist privacy: verify, never assume -------------------------------------

def test_done_page_warns_when_spotify_made_the_playlist_public():
    html = views.done("sid", [], {"url": "https://open.spotify.com/playlist/x", "public": True})
    assert "public" in html and 'class="warn"' in html


def test_done_page_is_quiet_when_the_playlist_is_private():
    html = views.done("sid", [], {"url": "https://open.spotify.com/playlist/x", "public": False})
    assert 'class="warn"' not in html


def test_create_playlist_forces_private_and_reports_the_real_state(monkeypatch):
    """Spotify ignores public:false at creation, so it must be set and read back."""
    calls = []

    def fake_call(sess, path, method="GET", json_body=None, retry=True):
        calls.append((method, path.split("?")[0], json_body))
        if method == "POST" and path.endswith("/playlists"):
            return {"id": "pl1", "external_urls": {"spotify": "https://x/pl1"}}
        if method == "GET" and path.startswith("/playlists/pl1"):
            return {"public": False}
        return None

    monkeypatch.setattr(spotify, "_call", fake_call)
    out = spotify.create_playlist({}, "user", "n", "d", ["spotify:episode:1"])

    assert out["public"] is False
    assert ("PUT", "/playlists/pl1", {"public": False}) in calls, "must force privacy"
    assert any(m == "GET" and p.startswith("/playlists/pl1") for m, p, _ in calls), "must verify"


def test_create_playlist_reports_public_when_spotify_refuses(monkeypatch):
    def fake_call(sess, path, method="GET", json_body=None, retry=True):
        if method == "POST" and path.endswith("/playlists"):
            return {"id": "pl1", "external_urls": {"spotify": "https://x/pl1"}}
        if method == "GET" and path.startswith("/playlists/pl1"):
            return {"public": True}  # what Spotify actually did in the live run
        return None

    monkeypatch.setattr(spotify, "_call", fake_call)
    assert spotify.create_playlist({}, "user", "n", "d", [])["public"] is True


# --- fan-out: a show that fails must be reported, never silently dropped ---------

class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload or {}

    def json(self):
        return self._payload


def _episode(eid):
    return {
        "id": eid,
        "uri": f"spotify:episode:{eid}",
        "name": f"Ep {eid}",
        "description": "desc",
        "duration_ms": 1_500_000,
        "release_date": "2026-09-01",
        "resume_point": {"fully_played": False, "resume_position_ms": 0},
    }


SHOWS = [
    {"id": "s1", "name": "Good Show", "publisher": "P"},
    {"id": "s2", "name": "Expired Show", "publisher": "P"},
    {"id": "s3", "name": "Limited Show", "publisher": "P"},
    {"id": "s4", "name": "Exploding Show", "publisher": "P"},
]


@pytest.fixture
def fanout(monkeypatch):
    session = {
        "tokens": {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_at": (time.time() + 600) * 1000,
        }
    }

    def fake_call(sess, path, *a, **k):
        if path.startswith("/me/shows"):
            return {"items": [{"show": s} for s in SHOWS], "next": None}
        if path.startswith("/me/player/recently-played"):
            return {"items": []}
        raise AssertionError(f"unexpected sequential call: {path}")

    def fake_request(token, path, method="GET", json_body=None):
        assert token == "tok"
        if "/shows/s1/" in path:
            return FakeResp(200, {"items": [_episode("e1")]})
        if "/shows/s2/" in path:
            return FakeResp(401)  # token died mid-fan-out
        if "/shows/s3/" in path:
            return FakeResp(429)  # rate limited by the burst
        raise requests.exceptions.ConnectTimeout("boom")

    monkeypatch.setattr(spotify, "_call", fake_call)
    monkeypatch.setattr(spotify, "_request", fake_request)
    return session


def test_fanout_completes_for_shows_that_succeeded(fanout):
    """(1) One good show still yields its episodes despite three failures."""
    out = spotify.get_backlog(fanout)
    assert [e["id"] for e in out["episodes"]] == ["e1"]
    assert out["episodes"][0]["show"] == "Good Show"


def test_fanout_records_which_show_failed_and_why(fanout):
    """(2) Every failure is recorded with the show and a distinguishable reason."""
    out = spotify.get_backlog(fanout)
    by_show = {d["show"]: d for d in out["dropped"]}

    assert set(by_show) == {"Expired Show", "Limited Show", "Exploding Show"}
    assert by_show["Expired Show"]["code"] == "token_expired"
    assert by_show["Limited Show"]["code"] == "rate_limited"
    assert by_show["Exploding Show"]["code"] == "network_error"
    assert by_show["Exploding Show"]["error"] == "ConnectTimeout"
    # the show id is recorded too, so admin dump can identify it unambiguously
    assert by_show["Expired Show"]["id"] == "s2"


def test_shows_scanned_never_counts_shows_we_failed_to_read(fanout):
    out = spotify.get_backlog(fanout)
    assert out["showsFollowed"] == 4
    assert out["showsScanned"] == 1  # not 4 — the page must not overstate coverage


def test_clean_fanout_reports_nothing_dropped(monkeypatch):
    session = {
        "tokens": {"access_token": "tok", "refresh_token": "r", "expires_at": (time.time() + 600) * 1000}
    }
    monkeypatch.setattr(
        spotify,
        "_call",
        lambda s, p, *a, **k: {"items": [{"show": SHOWS[0]}], "next": None}
        if p.startswith("/me/shows")
        else {"items": []},
    )
    monkeypatch.setattr(
        spotify, "_request", lambda *a, **k: FakeResp(200, {"items": [_episode("e1")]})
    )
    out = spotify.get_backlog(session)
    assert out["dropped"] == []
    assert out["showsScanned"] == out["showsFollowed"] == 1


def test_results_page_tells_the_user_the_backlog_was_incomplete(fanout):
    """(3) The user sees the gap on the page, not just in events.jsonl."""
    out = spotify.get_backlog(fanout)
    html = views.results("sid", 30, [], [], 1, out["showsScanned"], out["dropped"])

    assert "Couldn't check 3 of your shows" in html
    for name in ("Expired Show", "Limited Show", "Exploding Show"):
        assert name in html
    assert "token expired mid-scan" in html
    assert "hit Spotify&#x27;s rate limit" in html or "rate limit" in html


def test_results_page_is_clean_when_nothing_dropped():
    html = views.results("sid", 30, [], [], 5, 3, [])
    assert "Couldn't check" not in html
    assert 'class="warn"' not in html


def test_empty_backlog_still_warns_when_shows_were_dropped():
    """"Inbox zero" is a lie if we couldn't read half the shows."""
    dropped = [{"show": "Expired Show", "id": "s2", "code": "token_expired", "reason": "token expired mid-scan"}]
    assert "Couldn't check 1 of your shows" in views.dropped_note(dropped)
    assert views.dropped_note([]) == ""


def test_dropped_note_counts_distinct_shows_not_rows():
    dupes = [
        {"show": "A", "id": "s1", "code": "rate_limited", "reason": "hit Spotify's rate limit"},
        {"show": "A", "id": "s1", "code": "rate_limited", "reason": "hit Spotify's rate limit"},
    ]
    assert "1 of your shows" in views.dropped_note(dupes)


def test_dropped_note_escapes_show_names():
    dropped = [{"show": "<script>x</script>", "id": "s9", "code": "rate_limited", "reason": "hit Spotify's rate limit"}]
    note = views.dropped_note(dropped)
    assert "<script>x</script>" not in note
    assert "&lt;script&gt;" in note


# --- views: escaping is the one place a bad episode title could inject HTML ---

def test_views_escape_episode_titles():
    html = views.results(
        "sid1", 30,
        [{"id": "e1", "title": "<script>x</script>", "show": "S", "duration_min": 20, "reason": "r"}],
        [], 1, 1,
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


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
