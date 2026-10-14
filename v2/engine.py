"""The ONE place the V2 app chooses what to play. Swap the method here, nowhere else.

For now it is newest_first, the Phase 0 baseline: no later layer has beaten it yet
on labeled backlogs. The hard budget rule is checked here too, whatever the method.
"""

from v2 import baselines

ENGINE_NAME = "newest_first"


def pick(backlog: list[dict], budget_minutes: int, intent: str = ""):
    """Returns (picks, skips). sum(duration_min of picks) <= budget_minutes, always."""
    picks, skips = baselines.newest_first(backlog, budget_minutes, intent)
    total = sum(p["duration_min"] for p in picks)
    if total > budget_minutes:
        raise AssertionError(f"{ENGINE_NAME} went over budget: {total} > {budget_minutes} min")
    return picks, skips
