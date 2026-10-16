"""rank.py: the one LLM call (Groq by default, Claude optional).

Moved from test_podsen.py when the V1 Spotify flow was removed. rank() is still used
by the v1_llm baseline in v2/baselines.py and by later V2 layers. Transports are
stubbed: no API key and no network needed.
"""

import json
import os
import sys
import types

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))

import rank as rank_mod  # noqa: E402


# --- model-output JSON extraction ---

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



# --- hydration: index -> episode, and the pick/skip overlap guard ---

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



# --- backend selection ---

def test_default_backend_is_groq(monkeypatch):
    """With RANK_BACKEND unset, ranking goes to Groq."""
    monkeypatch.delenv("RANK_BACKEND", raising=False)
    assert rank_mod._backend_name() == "groq"


def test_claude_is_never_reached_without_being_asked_for(monkeypatch, groq):
    """An absent ANTHROPIC_API_KEY must not break the default path."""
    monkeypatch.delenv("RANK_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def boom():
        raise AssertionError("Claude client must not be constructed on the groq path")

    monkeypatch.setattr(rank_mod, "_anthropic", boom)
    monkeypatch.setattr(rank_mod.requests, "post", lambda *a, **k: FakeGroq(ANSWER))
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
    assert set(rank_mod._BACKENDS) == {"claude", "groq"}


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



# --- Groq call handling ---

class FakeGroq:
    """Minimal stand-in for a Groq /chat/completions response."""

    def __init__(self, content, finish_reason="stop", status=200):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = {
            "model": "stub",
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        }
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def groq(monkeypatch):
    """Select groq with a placeholder key so no test depends on, or uses, the real .env one.

    Every test using this also stubs requests.post; nothing here reaches the network.
    """
    monkeypatch.setenv("RANK_BACKEND", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "placeholder-not-a-real-key")


def test_groq_backend_end_to_end(monkeypatch, groq):
    monkeypatch.setenv("GROQ_MODEL", "stub-model")
    monkeypatch.setenv("GROQ_BASE_URL", "https://groq.test/openai/v1/")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["json"] = kw["json"]
        seen["headers"] = kw["headers"]
        return FakeGroq(ANSWER)

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    out = rank_mod.rank(CANDIDATES, 45)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    # a trailing slash on the base URL must not produce a //chat/completions path
    assert seen["url"] == "https://groq.test/openai/v1/chat/completions"
    assert seen["json"]["model"] == "stub-model"
    assert seen["headers"]["Authorization"] == "Bearer placeholder-not-a-real-key"
    assert seen["json"]["messages"][0]["content"].startswith(rank_mod.SYSTEM)
    assert "45 minutes" in seen["json"]["messages"][-1]["content"]


def test_groq_sets_its_output_budget_per_request(monkeypatch, groq):
    """Groq has no num_ctx; the per-request size limit it does take is the completion cap."""
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw["json"])
        return FakeGroq(ANSWER)

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    rank_mod.rank(CANDIDATES, 30)

    assert seen["max_completion_tokens"] == 3000
    assert seen["reasoning_effort"] == "low"  # keeps reasoning from eating that budget


def test_groq_token_limit_gives_an_actionable_error(monkeypatch, groq):
    monkeypatch.setattr(
        rank_mod.requests, "post", lambda *a, **k: FakeGroq("", finish_reason="length")
    )
    with pytest.raises(RuntimeError, match="completion-token limit"):
        rank_mod.rank(CANDIDATES, 30)


def test_groq_explains_when_unreachable(monkeypatch, groq):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(rank_mod.requests, "post", boom)
    with pytest.raises(RuntimeError, match="Could not connect to Groq"):
        rank_mod.rank(CANDIDATES, 30)


def test_groq_without_a_key_says_so_clearly(monkeypatch, groq):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(rank_mod, "GROQ_API_KEY", "")

    def must_not_call(*a, **k):
        raise AssertionError("must not call Groq without a key")

    monkeypatch.setattr(rank_mod.requests, "post", must_not_call)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        rank_mod.rank(CANDIDATES, 30)



# --- malformed JSON: one corrective retry before giving up ---

def test_bad_json_is_retried_once_and_succeeds(monkeypatch, groq):
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["messages"][-1]["content"])
        # first reply has an unescaped quote inside the reason -- the failure seen
        # from gemma4 in live testing
        broken = '{"picks":[{"n":1,"reason":"he said "hello" loudly"}],"skips":[]}'
        return FakeGroq(broken if len(calls) == 1 else ANSWER)

    monkeypatch.setattr(rank_mod.requests, "post", fake_post)
    out = rank_mod.rank(CANDIDATES, 30)

    assert [p["id"] for p in out["picks"]] == ["e1"]
    assert len(calls) == 2, "should have retried exactly once"
    assert "could not be parsed as JSON" in calls[1], "retry must carry a corrective hint"
    assert "could not be parsed" not in calls[0], "first attempt must be the clean prompt"


def test_bad_json_twice_fails_with_a_clear_message(monkeypatch, groq):
    calls = []

    def fake_post(url, **kw):
        calls.append(1)
        return FakeGroq("sorry, no idea")

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
    """Groq gets a smaller prompt than Claude; both honour their LIMITS."""
    many = [dict(CANDIDATES[0], id=f"e{i}") for i in range(80)]
    c_max, c_chars = rank_mod.LIMITS["claude"]
    g_max, g_chars = rank_mod.LIMITS["groq"]

    c_cands, _ = rank_mod.build_prompt(many, 30, c_max, c_chars)
    g_cands, _ = rank_mod.build_prompt(many, 30, g_max, g_chars)

    assert len(c_cands) == c_max and len(g_cands) == g_max
    assert g_max <= c_max and g_chars <= c_chars, "groq prompt must not exceed Claude's"



# --- time budget is enforced in code, not left to the model ---
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


def test_rank_never_returns_a_pick_over_budget(monkeypatch, groq):
    """The end-to-end guarantee: 15 minutes in, nothing longer than 15 minutes out."""
    # model tries to pick everything it is shown, including anything long
    greedy = json.dumps({"picks": [{"n": 1, "reason": "r"}, {"n": 2, "reason": "r"}],
                         "skips": [{"n": 3, "reason": "k"}]})
    monkeypatch.setattr(
        rank_mod.requests, "post",
        lambda *a, **k: FakeGroq(greedy),
    )
    out = rank_mod.rank(BACKLOG, 15)
    assert all(p["duration_min"] <= 15 * rank_mod.BUDGET_TOLERANCE for p in out["picks"])
    assert "l1" not in [p["id"] for p in out["picks"]]


def test_rank_says_nothing_fits_instead_of_overshooting(monkeypatch, groq):

    def must_not_call(*a, **k):
        raise AssertionError("model must not be called when nothing fits")

    monkeypatch.setattr(rank_mod.requests, "post", must_not_call)
    out = rank_mod.rank(BACKLOG, 5)
    assert out["picks"] == [] and out["skips"] == []
    assert out["shortest_unfit"] == 12


def test_model_only_ever_sees_episodes_that_fit(monkeypatch, groq):
    seen = {}

    def spy(url, **kw):
        seen["prompt"] = kw["json"]["messages"][-1]["content"]
        return FakeGroq(ANSWER)

    monkeypatch.setattr(rank_mod.requests, "post", spy)
    rank_mod.rank(BACKLOG, 30)
    assert "Epic" not in seen["prompt"] and "Long" not in seen["prompt"]
    assert "Short" in seen["prompt"] and "Medium" in seen["prompt"]
