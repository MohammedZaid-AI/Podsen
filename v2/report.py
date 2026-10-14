"""Evaluate every baseline on every labeled backlog and print one plain-text table.

    python -m v2.report                        # DATA_DIR/labels/
    python -m v2.report --dir v2/sample_data   # bundled FAKE samples

Confidence intervals are a percentile bootstrap over BACKLOGS (2000 rounds, fixed
seed): the unit of evidence is a backlog someone labeled, not an episode.
"""

import argparse
import sys

from v2 import baselines, dataset, metrics

ROUNDS = 2000
# Below this many backlogs no comparison is called, whatever the intervals say.
MIN_BACKLOGS = 5


def _checked(name, picks, budget):
    # The hard cap is the one promise every method makes; check it, don't trust it.
    total = sum(p["duration_min"] for p in picks)
    if total > budget:
        raise AssertionError(f"{name} went over budget: {total} > {budget} min")
    return picks


def _run_backlog(name, bl):
    eps, budget, intent = bl["episodes"], bl["budget_minutes"], bl["intent"]
    labels, groups = bl["labels"], bl.get("duplicate_groups", [])
    dropped = []
    if name == "random_fill":
        runs = []
        for s in range(baselines.RANDOM_SEEDS):
            picks, skips = baselines.random_fill(eps, budget, intent, seed=s)
            runs.append(metrics.score(_checked(name, picks, budget), skips, labels, budget, groups))
        out = {m: metrics.mean(r[m] for r in runs) for m in metrics.METRICS}
        out["groups_touched"] = sum(r["groups_touched"] for r in runs) / len(runs)
        out["groups_total"] = len(groups)
        return out, 0
    if name == "v1_llm":
        picks, skips, dropped = baselines.v1_llm_with_stats(eps, budget, intent)
    else:
        picks, skips = baselines.METHODS[name](eps, budget, intent)
    return metrics.score(_checked(name, picks, budget), skips, labels, budget, groups), len(dropped)


def evaluate(backlogs, methods=None, seed=0, rounds=ROUNDS) -> dict:
    results = {}
    for name in methods or list(baselines.METHODS):
        row = {"status": "ok", "note": "", "per_backlog": [], "dropped": [], "summary": {}}
        try:
            for bl in backlogs:
                scores, n_dropped = _run_backlog(name, bl)
                row["per_backlog"].append(scores)
                row["dropped"].append(n_dropped)
        except baselines.Unavailable as e:
            row.update(status="skipped", note=str(e), per_backlog=[], dropped=[])
        except Exception as e:  # one failed backlog fails the row: partial must not look complete
            row.update(status="failed", note=f"{type(e).__name__}: {str(e)[:160]}", per_backlog=[])
        if row["status"] == "ok":
            for m in metrics.METRICS:
                vals = [s[m] for s in row["per_backlog"]]
                row["summary"][m] = (
                    metrics.mean(vals),
                    metrics.bootstrap_ci(vals, rounds=rounds, seed=seed),
                    sum(v is not None for v in vals),
                )
            row["groups"] = (
                sum(s["groups_touched"] for s in row["per_backlog"]),
                sum(s["groups_total"] for s in row["per_backlog"]),
            )
        results[name] = row
    return results


def _cell(summary):
    mean, ci, _ = summary
    if mean is None:
        return "n/a"
    if ci is None:
        return f"{mean:.2f} (no CI)"
    return f"{mean:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]"


def _groups_cell(groups):
    touched, total = groups
    if not total:
        return "n/a"
    shown = f"{touched:.0f}" if float(touched).is_integer() else f"{touched:.1f}"
    return f"{shown} of {total}"


def _row_cells(name, row):
    return [name, *(_cell(row["summary"][m]) for m in metrics.METRICS), _groups_cell(row["groups"])]


def _table(results) -> str:
    header = ["method", *metrics.METRICS, "groups_touched"]
    ok = [_row_cells(n, r) for n, r in results.items() if r["status"] == "ok"]
    widths = [max(len(r[i]) for r in [header, *ok]) for i in range(len(header))]
    widths[0] = max(widths[0], *(len(n) for n in results))

    def line(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    out = [line(header), line(["-" * w for w in widths])]
    for name, row in results.items():
        if row["status"] == "ok":
            out.append(line(_row_cells(name, row)))
        else:  # the note spans the metric columns instead of widening them
            out.append(f"{name.ljust(widths[0])}  {row['status'].upper()}, {row['note']}")
    return "\n".join(out)


def _compare(results) -> list[str]:
    base = results.get("newest_first")
    if not base or base["status"] != "ok":
        return ["newest_first did not run, so nothing can be compared against it."]
    out = []
    for name, row in results.items():
        if name == "newest_first":
            continue
        if row["status"] != "ok":
            out.append(f"{name}: {row['status']} ({row['note']}), so it cannot be compared.")
            continue
        for m in metrics.METRICS:
            (a, a_ci, _), (b, b_ci, _) = row["summary"][m], base["summary"][m]
            if a is None or b is None:
                out.append(f"{name} vs newest_first on {m}: n/a for at least one of them.")
                continue
            if a_ci is None or b_ci is None:
                out.append(
                    f"{name} vs newest_first on {m}: {a:.2f} vs {b:.2f}, too few backlogs "
                    "for an interval. Not a meaningful difference yet."
                )
                continue
            if a_ci[0] <= b_ci[1] and b_ci[0] <= a_ci[1]:
                out.append(
                    f"{name} vs newest_first on {m}: {a:.2f} vs {b:.2f}, intervals overlap. "
                    "Not a meaningful difference yet."
                )
                continue
            n = min(row["summary"][m][2], base["summary"][m][2])
            if n < MIN_BACKLOGS:
                # With a handful of backlogs the bootstrap has only a few distinct
                # resamples, so its interval is far narrower than the real uncertainty.
                out.append(
                    f"{name} vs newest_first on {m}: {a:.2f} vs {b:.2f}, intervals do not "
                    f"overlap, but only {n} backlog(s) (need {MIN_BACKLOGS}+). "
                    "Not a meaningful difference yet."
                )
                continue
            better = (a < b) if m in metrics.LOWER_IS_BETTER else (a > b)
            out.append(
                f"{name} vs newest_first on {m}: {a:.2f} vs {b:.2f}, intervals do not overlap. "
                f"{name} is {'BETTER' if better else 'WORSE'}."
            )
    return out


def render(results, backlogs, source="") -> str:
    n_eps = sum(len(b["labels"]) for b in backlogs)
    lines = []
    if any(b.get("fake") for b in backlogs):
        lines.append("*** FAKE SAMPLE DATA: these numbers say nothing about real picks. ***")
    lines.append(f"Backlogs: {len(backlogs)}   Labeled episodes: {n_eps}   Source: {source}")
    lines.append(
        f"Mean over backlogs, 95% bootstrap CI over backlogs ({ROUNDS} rounds, fixed seed). "
        f"random_fill is averaged over {baselines.RANDOM_SEEDS} seeds per backlog."
    )
    lines.append("")
    lines.append(_table(results))
    lines.append("")
    v1 = results.get("v1_llm")
    if v1 and v1["status"] == "ok":
        n = sum(v1["dropped"])
        hit = sum(1 for d in v1["dropped"] if d)
        lines.append(
            f"v1_llm over-budget picks dropped by the hard cap: {n} pick(s), "
            f"in {hit} of {len(backlogs)} backlog(s)."
        )
        lines.append("")
    lines.append("Compared with newest_first:")
    lines += [f"- {s}" for s in _compare(results)]
    lines.append("")
    lines.append(
        "Notes: skips = every backlog episode not picked. pick_hit_rate counts wanted and "
        "would_regret_skipping as hits. regret_rate: lower is better. n/a = not computable "
        "for that backlog (e.g. no duplicate groups) and left out of the mean. "
        "groups_touched: duplicate groups the method picked at least one episode from, "
        "summed over backlogs (random_fill: average over seeds). A group with nothing "
        "picked counts as caught in duplicate_catch_rate but was not touched."
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m v2.report", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=None, help="folder of label files (default: DATA_DIR/labels)")
    ap.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    args = ap.parse_args(argv)

    directory = args.dir or dataset.labels_dir()
    try:
        backlogs = dataset.load_dir(directory)
    except dataset.DatasetError as e:
        print(f"Cannot load labels: {e}", file=sys.stderr)
        return 2
    if not backlogs:
        print(
            f"No label files in {directory}. Label a backlog at /label, "
            "or try: python -m v2.report --dir v2/sample_data",
            file=sys.stderr,
        )
        return 1
    print(render(evaluate(backlogs, seed=args.seed), backlogs, source=str(directory)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
