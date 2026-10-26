"""Baselines for V2 set selection. One interface:

    select(backlog, budget_minutes, intent) -> (picks, skips)

`picks` is the set to listen to now; `skips` is everything else in the backlog, so
every method is scored on the same split. The budget is a HARD cap for every method:
sum(duration_min of picks) <= budget_minutes, enforced in code.
"""

import os
import random

import rank
from v2 import preferences as listening


def enforce_budget(picks: list[dict], budget_minutes: int) -> tuple[list[dict], list[dict]]:
    """Keep picks in order while the running total fits; return (kept, dropped)."""
    kept, dropped, total = [], [], 0
    for p in picks:
        if total + p["duration_min"] <= budget_minutes:
            kept.append(p)
            total += p["duration_min"]
        else:
            dropped.append(p)
    return kept, dropped


def _split(backlog, picks):
    chosen = {p["id"] for p in picks}
    return picks, [e for e in backlog if e["id"] not in chosen]


def _fill(ordered: list[dict], budget_minutes: int) -> list[dict]:
    """Walk `ordered`, adding each episode that still fits. An episode too long for the
    remaining time is passed over, not a stop: a shorter one later may still fit."""
    kept, _ = enforce_budget(ordered, budget_minutes)
    return kept


# --- a. newest first ---------------------------------------------------------

def newest_first(backlog, budget_minutes, intent=""):
    # Undated episodes sort last; ties keep backlog order (sorted() is stable).
    ordered = sorted(backlog, key=lambda e: e.get("released") or "", reverse=True)
    return _split(backlog, _fill(ordered, budget_minutes))


# --- b. random fill ----------------------------------------------------------

RANDOM_SEEDS = 200


def random_fill(backlog, budget_minutes, intent="", seed=0):
    ordered = list(backlog)
    random.Random(seed).shuffle(ordered)
    return _split(backlog, _fill(ordered, budget_minutes))


# --- c. V1's LLM ranking, run on RSS episodes --------------------------------

class Unavailable(RuntimeError):
    """The method cannot run here (e.g. no API key). The report shows it as SKIPPED."""


def v1_llm_available() -> tuple[bool, str]:
    try:
        backend = rank._backend_name()
    except RuntimeError as e:
        return False, str(e)
    if backend == "groq" and not (os.environ.get("GROQ_API_KEY") or rank.GROQ_API_KEY).strip():
        return False, "no API key"
    if backend == "claude" and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        return False, "no API key"
    return True, backend


def v1_llm_with_stats(backlog, budget_minutes, intent="", preferences=None):
    """Returns (picks, skips, dropped_over_budget).

    V1 tolerates 10% over per episode (BUDGET_TOLERANCE) and never caps the pair's
    total, so its picks are re-checked here against the hard cap. Dropped picks are
    returned so the report can say how often that happened.

    `preferences` (optional, v2/preferences.py): speed turns every length into listening
    time before rank()'s budget filter and the hard cap below; wants and skips go into
    the prompt as one plain sentence with no number in it.
    """
    ok, why = v1_llm_available()
    if not ok:
        raise Unavailable(why)
    if preferences:
        backlog = listening.at_speed(backlog, preferences["listening_speed"])
    # `intent` is unused: V1 has no such input.
    listener = listening.describe(preferences)
    out = (rank.rank(backlog, budget_minutes, listener=listener) if listener
           else rank.rank(backlog, budget_minutes))
    by_id = {e["id"]: e for e in backlog}
    # Return the backlog's own episode dicts, not V1's copies with a "reason" attached.
    raw = [by_id[p["id"]] for p in out["picks"] if p["id"] in by_id]
    kept, dropped = enforce_budget(raw, budget_minutes)
    picks, skips = _split(backlog, kept)
    return picks, skips, dropped


def v1_llm(backlog, budget_minutes, intent="", preferences=None):
    picks, skips, _ = v1_llm_with_stats(backlog, budget_minutes, intent, preferences)
    return picks, skips


METHODS = {"newest_first": newest_first, "random_fill": random_fill, "v1_llm": v1_llm}
