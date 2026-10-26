"""Listening preferences: three optional questions asked once, before the first triage.

Speed changes the time math only: an episode takes minutes / speed to hear, rounded UP
so the budget stays a hard cap. Wants and skips become one plain sentence for an LLM
picker prompt. No number ever goes into that sentence: anything numeric is enforced in
code (see CLAUDE.md, "Never ask the model to respect a hard number").
"""

import math

SPEEDS = (1.0, 1.5, 2.0)
WANTS = ("learning", "news and current events", "entertainment", "stories", "business")
SKIPS = ("filler", "long interviews", "old news", "promo-heavy shows")
DEFAULTS = {"listening_speed": 1.0, "wants": [], "skips": []}


def clean(speed, wants, skips) -> dict:
    """Only the offered answers, in the offered order. Anything else becomes the default."""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    return {
        "listening_speed": speed if speed in SPEEDS else 1.0,
        "wants": [w for w in WANTS if w in set(wants or [])],
        "skips": [s for s in SKIPS if s in set(skips or [])],
    }


def speed_label(speed: float) -> str:
    return f"{speed:g}x"


def listen_minutes(minutes, speed: float):
    """Minutes it takes to hear an episode at this speed, rounded up (never under 1)."""
    if not minutes or speed == 1.0:
        return minutes
    return max(1, math.ceil(minutes / speed))


def at_speed(episodes: list[dict], speed: float) -> list[dict]:
    """Copies whose duration_min is the listening time at this speed, so every picker and
    every budget check works on what the listener actually spends. raw_min keeps the
    episode's own length."""
    return [{**e, "duration_min": listen_minutes(e.get("duration_min"), speed),
             "raw_min": e.get("duration_min")} for e in episodes]


def describe(prefs: dict | None) -> str:
    """The prompt sentence, e.g. "Listener wants: learning, news and current events.
    Skips: filler, promo-heavy shows." Empty when nothing was chosen. No numbers."""
    if not prefs:
        return ""
    parts = []
    if prefs.get("wants"):
        parts.append("Listener wants: " + ", ".join(prefs["wants"]) + ".")
    if prefs.get("skips"):
        parts.append("Skips: " + ", ".join(prefs["skips"]) + ".")
    return " ".join(parts)
