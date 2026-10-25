"""v2/engine.py: the app's pick rule (newest_first_varied), unused-time reasons and the
"If you have a little longer" options. Pure arithmetic: no app, no network."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from v2 import baselines, engine  # noqa: E402


def ep(eid, show, minutes, released):
    return {"id": eid, "show": show, "itunes_id": show, "duration_min": minutes, "released": released,
            "title": eid}


def ids(eps):
    return [e["id"] for e in eps]


# --- variety --------------------------------------------------------------------------------

def test_prefers_a_different_show_over_a_newer_second_episode():
    backlog = [ep("A1", "A", 20, "2026-10-10"), ep("A2", "A", 20, "2026-10-09"),
               ep("B1", "B", 20, "2026-10-01")]
    picks, skips = engine.pick(backlog, 40)
    assert ids(picks) == ["A1", "B1"], "A2 is newer than B1 but B1 is from another show"
    assert ids(skips) == ["A2"]
    # the plain Phase 0 baseline is unchanged and still takes A's two newest
    assert ids(baselines.newest_first(backlog, 40)[0]) == ["A1", "A2"]


def test_falls_back_to_the_same_show_only_when_nothing_else_fits():
    backlog = [ep("A1", "A", 20, "2026-10-10"), ep("A2", "A", 15, "2026-10-09"),
               ep("B1", "B", 60, "2026-10-08")]
    picks, _ = engine.pick(backlog, 40)
    assert ids(picks) == ["A1", "A2"], "B1 can't fit, so a second A is better than unused time"


def test_no_fallback_while_another_show_still_fits():
    backlog = [ep("A1", "A", 20, "2026-10-10"), ep("A2", "A", 10, "2026-10-09"),
               ep("B1", "B", 15, "2026-10-01")]
    picks, _ = engine.pick(backlog, 40)
    assert ids(picks) == ["A1", "B1"]  # A2 would also have fitted at first, but B1 wins
    assert sum(p["duration_min"] for p in picks) == 35


def test_one_per_show_across_many_shows():
    backlog = [ep(f"{s}{n}", s, 10, f"2026-10-{20 - n:02d}") for s in "ABCD" for n in range(1, 4)]
    picks, _ = engine.pick(backlog, 40)
    assert sorted(p["show"] for p in picks) == ["A", "B", "C", "D"]


def test_a_single_show_fills_the_time_with_its_own_newest_episodes():
    """One show is enough to use Podsen. Pass 1 takes its newest episode that fits; with
    no other show, the same-show fallback adds more of its episodes that still fit."""
    backlog = [ep("A1", "A", 15, "2026-10-20"), ep("A2", "A", 20, "2026-10-18"), ep("A3", "A", 40, "2026-10-15")]
    picks, skips = engine.pick(backlog, 40)
    assert ids(picks) == ["A1", "A2"] and ids(skips) == ["A3"]
    assert sum(p["duration_min"] for p in picks) <= 40


def test_a_single_show_with_one_fitting_episode_gives_one_pick():
    backlog = [ep("A1", "A", 25, "2026-10-20"), ep("A2", "A", 50, "2026-10-18")]
    picks, skips = engine.pick(backlog, 30)
    assert ids(picks) == ["A1"] and ids(skips) == ["A2"]
    assert engine.pick([], 30) == ([], [])


def test_shows_are_told_apart_by_itunes_id_then_name():
    a = {"id": "x1", "show": "Same Name", "itunes_id": "1", "duration_min": 10, "released": "2026-10-02"}
    b = {"id": "x2", "show": "Same Name", "itunes_id": "2", "duration_min": 10, "released": "2026-10-01"}
    c = {"id": "x3", "show": "Same Name", "itunes_id": "1", "duration_min": 10, "released": "2026-09-30"}
    picks, _ = engine.pick([a, b, c], 20)
    assert ids(picks) == ["x1", "x2"]


def test_engine_name_is_its_own():
    assert engine.ENGINE_NAME == "newest_first_varied"


# --- the hard budget cap --------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_hard_budget_cap_always_holds(seed):
    rng = random.Random(seed)
    backlog = [ep(f"e{i}", rng.choice("ABCDE"), rng.choice([3, 8, 15, 22, 28, 31, 45, 61, 95]),
                  f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}") for i in range(rng.randint(0, 30))]
    for budget in (1, 5, 15, 27, 30, 45, 55, 60, 90, 200):
        picks, skips = engine.pick(backlog, budget)
        assert sum(p["duration_min"] for p in picks) <= budget
        assert sorted(ids(picks) + ids(skips)) == sorted(ids(backlog)), "nothing lost, nothing doubled"
        left = budget - sum(p["duration_min"] for p in picks)
        assert not any(s["duration_min"] <= left for s in skips), "every gap a skip could fill is filled"
        # a show only gives a second episode once no other show's episode fits
        per_show = {}
        for p in picks:
            per_show.setdefault(p["show"], []).append(p)
        if any(len(v) > 1 for v in per_show.values()):
            first_pass = sum(v[0]["duration_min"] for v in per_show.values())
            unpicked_shows = {e["show"] for e in backlog} - set(per_show)
            assert not any(e["duration_min"] <= budget - first_pass
                           for e in backlog if e["show"] in unpicked_shows)


# --- why time is left over --------------------------------------------------------------------

def test_unused_time_names_the_shortest_when_nothing_else_fits():
    picks = [ep("P", "A", 28, "2026-10-16")]
    skips = [ep("S1", "B", 31, "2026-10-15"), ep("S2", "C", 41, "2026-10-14")]
    assert engine.unused_time(picks, skips, 55) == {"left": 27, "reason": "nothing_fits", "shortest": 31}


def test_unused_time_is_quiet_at_five_minutes_or_less():
    picks = [ep("P", "A", 50, "2026-10-16")]
    assert engine.unused_time(picks, [ep("S", "B", 31, "2026-10-15")], 55) is None
    assert engine.unused_time(picks, [], 56) is not None


def test_unused_time_when_the_backlog_is_used_up():
    info = engine.unused_time([ep("P", "A", 10, "2026-10-16")], [], 60)
    assert info == {"left": 50, "reason": "backlog_empty", "shortest": None}


def test_unused_time_never_claims_nothing_fits_when_something_does():
    info = engine.unused_time([ep("P", "A", 10, "2026-10-16")], [ep("S", "B", 20, "2026-10-15")], 60)
    assert info["reason"] == "left_unpicked"


# --- "If you have a little longer" ------------------------------------------------------------

def test_a_little_longer_respects_the_15_minute_limit_and_order():
    picks = [ep("P", "A", 28, "2026-10-16")]
    skips = [ep("S47", "C", 47, "2026-10-13"),   # needs 20 more: too much
             ep("S41", "A", 41, "2026-10-14"),   # 14 more
             ep("S34", "B", 34, "2026-10-12"),   # 7 more
             ep("S31", "B", 31, "2026-10-15")]   # 4 more
    out = engine.a_little_longer(picks, skips, 55)
    assert ids(out) == ["S31", "S34"], "smallest extra first, at most 2"
    assert [(o["total_min"], o["extra_min"]) for o in out] == [(59, 4), (62, 7)]
    assert all(0 < o["extra_min"] <= engine.LONGER_EXTRA_MIN for o in out)


def test_a_little_longer_never_repeats_a_pick():
    backlog = [ep(f"e{i}", "ABC"[i % 3], 10 + 3 * i, f"2026-10-{25 - i:02d}") for i in range(9)]
    for budget in (10, 20, 31, 40, 55, 70):
        picks, skips = engine.pick(backlog, budget)
        out = engine.a_little_longer(picks, skips, budget)
        assert not set(ids(out)) & set(ids(picks))
        assert len(out) <= engine.LONGER_SHOWN
        for o in out:
            assert 0 < o["extra_min"] <= 15 and o["total_min"] == budget + o["extra_min"]


def test_a_little_longer_is_empty_when_nothing_is_close():
    picks = [ep("P", "A", 28, "2026-10-16")]
    assert engine.a_little_longer(picks, [ep("S", "B", 90, "2026-10-15")], 55) == []


def test_ties_on_extra_time_go_to_the_newest_episode():
    skips = [ep("old", "B", 31, "2026-09-01"), ep("new", "C", 31, "2026-10-15")]
    assert ids(engine.a_little_longer([ep("P", "A", 28, "2026-10-16")], skips, 55)) == ["new", "old"]
