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

def test_default_backend_is_claude(monkeypatch):
    monkeypatch.delenv("RANK_BACKEND", raising=False)
    assert rank_mod._backend_name() == "claude"


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


def test_ollama_backend_end_to_end(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["json"] = kw["json"]
        seen["headers"] = kw["headers"]
        return types.SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": ANSWER}}]},
        )

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    out = rank_mod.rank(CANDIDATES, 45)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert seen["url"].endswith("/chat/completions")
    assert seen["json"]["model"] == rank_mod.OLLAMA_MODEL
    assert seen["headers"]["Authorization"].startswith("Bearer ")


def test_ollama_context_truncation_gives_an_actionable_error(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")

    def fake_post(url, **kw):
        return types.SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "usage": {"total_tokens": 4096},
                "choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "thinking..."}}],
            },
        )

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="OLLAMA_CONTEXT_LENGTH"):
        rank_mod.rank(CANDIDATES, 30)


def test_ollama_falls_back_to_reasoning_when_content_empty(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "ollama")

    def fake_post(url, **kw):
        return types.SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": "", "reasoning": ANSWER}}]},
        )

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    assert [p["id"] for p in rank_mod.rank(CANDIDATES, 30)["picks"]] == ["e1"]


def test_backends_send_different_backlog_sizes():
    """Ollama's tighter context means a smaller prompt; Claude gets the full backlog."""
    many = [dict(CANDIDATES[0], id=f"e{i}") for i in range(60)]
    c_cands, _ = rank_mod.build_prompt(many, 30, *rank_mod.LIMITS["claude"])
    o_cands, _ = rank_mod.build_prompt(many, 30, *rank_mod.LIMITS["ollama"])
    assert len(c_cands) == 50 and len(o_cands) == 30


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


def test_page_renders_tagline():
    assert "Listen to what's worth it." in views.page("Podsen", "<p>hi</p>")
