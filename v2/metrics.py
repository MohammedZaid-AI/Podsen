"""Per-backlog metrics and a bootstrap over backlogs. Pure arithmetic, stdlib only.

A metric that cannot be computed for a backlog (no picks, no skips, no duplicate
groups) is None, shown as "n/a", and left out of the mean rather than counted as 0.
"""

import random

# "would_regret_skipping" is a stronger "wanted"; a pick carrying it is a hit, not a miss.
HIT_LABELS = {"wanted", "would_regret_skipping"}
METRICS = ("pick_hit_rate", "regret_rate", "budget_used", "duplicate_catch_rate")
LOWER_IS_BETTER = {"regret_rate"}


def _share(items, pred):
    return sum(1 for x in items if pred(x)) / len(items) if items else None


def score(picks, skips, labels: dict, budget_minutes: int, duplicate_groups=()) -> dict:
    picked = {p["id"] for p in picks}
    groups = [g for g in (duplicate_groups or []) if g]
    return {
        "pick_hit_rate": _share(picks, lambda p: labels.get(p["id"]) in HIT_LABELS),
        "regret_rate": _share(skips, lambda s: labels.get(s["id"]) == "would_regret_skipping"),
        "budget_used": min(1.0, sum(p["duration_min"] for p in picks) / budget_minutes),
        "duplicate_catch_rate": _share(groups, lambda g: len(picked & set(g)) <= 1),
        # Context for duplicate_catch_rate: picking NOTHING from a group also counts as
        # "at most one", so a method that avoids a topic entirely looks like it caught
        # the duplicate. Groups actually touched shows how many catches were real.
        "groups_touched": sum(1 for g in groups if picked & set(g)),
        "groups_total": len(groups),
    }


def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def bootstrap_ci(values, rounds: int = 2000, seed: int = 0, level: float = 0.95):
    """Percentile bootstrap CI of the mean, resampling backlogs. None if under 2 values."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(sum(rng.choice(vals) for _ in range(n)) / n for _ in range(rounds))
    tail = (1 - level) / 2
    return means[int(rounds * tail)], means[int(rounds * (1 - tail)) - 1]
