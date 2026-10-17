"""The ONE place the V2 app chooses what to play. Swap the method here, nowhere else.

Current method: newest_first_varied. The same newest-first fill as the Phase 0
baseline, plus variety: at most one episode per show, and a second episode from a
show only when nothing from any other show fits the time left. The plain baseline in
v2/baselines.py is untouched so the Phase 0 report keeps measuring it; runs and
feedback record ENGINE_NAME so results stay tied to the rule that made them.

The hard budget rule is checked here too, whatever the method. Everything below is
arithmetic in code, never a prompt.
"""

UNUSED_NOTE_MIN = 5    # say why when more than this many minutes are left over
LONGER_EXTRA_MIN = 15  # "If you have a little longer": at most this much over the budget
LONGER_SHOWN = 2

ENGINE_NAME = "newest_first_varied"


def _show_key(ep: dict) -> str:
    return str(ep.get("itunes_id") or ep.get("show") or "")


def _newest_first(backlog: list[dict]) -> list[dict]:
    # Same order as baselines.newest_first: undated last, ties keep backlog order.
    return sorted(backlog, key=lambda e: e.get("released") or "", reverse=True)


def pick(backlog: list[dict], budget_minutes: int, intent: str = ""):
    """Returns (picks, skips). sum(duration_min of picks) <= budget_minutes, always."""
    ordered = _newest_first(backlog)
    picks, total, shows_used = [], 0, set()

    # Pass 1: newest episode that fits from each show, one per show.
    for ep in ordered:
        if _show_key(ep) not in shows_used and total + ep["duration_min"] <= budget_minutes:
            picks.append(ep)
            total += ep["duration_min"]
            shows_used.add(_show_key(ep))

    # Pass 2: same-show fallback. Every show not yet used had all its episodes checked
    # in pass 1 with at least this much time left, so nothing from another show fits;
    # only now may a show contribute a second episode.
    chosen = {id(p) for p in picks}
    for ep in ordered:
        if id(ep) not in chosen and total + ep["duration_min"] <= budget_minutes:
            picks.append(ep)
            chosen.add(id(ep))
            total += ep["duration_min"]

    if total > budget_minutes:
        raise AssertionError(f"{ENGINE_NAME} went over budget: {total} > {budget_minutes} min")
    picked = {id(p) for p in picks}
    return picks, [e for e in backlog if id(e) not in picked]


def unused_time(picks: list[dict], skips: list[dict], budget_minutes: int) -> dict | None:
    """Why time is left over, when more than UNUSED_NOTE_MIN minutes are.

    {"left", "reason", "shortest"}: reason is "nothing_fits" (with the shortest
    unheard episode), "backlog_empty", or "left_unpicked" (an episode that fits was
    not taken; can't happen with this engine, but the page must never claim
    "nothing fits" when something did).
    """
    left = budget_minutes - sum(p["duration_min"] for p in picks)
    if left <= UNUSED_NOTE_MIN:
        return None
    if not skips:
        return {"left": left, "reason": "backlog_empty", "shortest": None}
    if any(s["duration_min"] <= left for s in skips):
        return {"left": left, "reason": "left_unpicked", "shortest": None}
    return {"left": left, "reason": "nothing_fits", "shortest": min(s["duration_min"] for s in skips)}


def a_little_longer(picks: list[dict], skips: list[dict], budget_minutes: int) -> list[dict]:
    """Up to LONGER_SHOWN skipped episodes that would fit with at most LONGER_EXTRA_MIN
    more minutes, smallest extra first. Each gets "total_min" (picks + it) and
    "extra_min". Never a pick: they come from the skips."""
    used = sum(p["duration_min"] for p in picks)
    options = []
    for s in _newest_first(skips):  # ties on extra time go to the newest episode
        extra = used + s["duration_min"] - budget_minutes
        if 0 < extra <= LONGER_EXTRA_MIN:
            options.append((extra, s))
    options.sort(key=lambda t: t[0])  # stable: newest-first order kept within a tie
    return [{**s, "total_min": used + s["duration_min"], "extra_min": extra}
            for extra, s in options[:LONGER_SHOWN]]
