"""Labeled backlog files: one JSON file per backlog under DATA_DIR/labels/.

{
  "name": "weekday commute",
  "budget_minutes": 45,
  "intent": "free text: what I want out of this listening slot",
  "episodes": [{id, title, show, description, duration_min, released, has_transcript}],
  "labels": {"<episode id>": "wanted" | "not_wanted" | "would_regret_skipping"},
  "duplicate_groups": [["<id>", "<id>"], ...]          # optional
}

Optional metadata keys: "feeds", "created", "fake", "note".
Episodes may carry ONLY the RSS fields below: a file containing a Spotify field such
as "uri" is rejected, so Spotify data cannot leak into the evaluation by copy-paste.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from v2.rss import EPISODE_FIELDS

LABELS = ("wanted", "not_wanted", "would_regret_skipping")
REQUIRED_EPISODE_FIELDS = ("id", "title", "show", "duration_min")
OPTIONAL_TOP = {"duplicate_groups", "feeds", "created", "fake", "note"}
REQUIRED_TOP = {"name", "budget_minutes", "intent", "episodes", "labels"}


class DatasetError(ValueError):
    """A label file is malformed. The message says which file and what to fix."""


def labels_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")) / "labels"


def validate(data, source: str = "<data>") -> dict:
    """Check a backlog dict and return it unchanged, or raise DatasetError."""

    def fail(msg):
        raise DatasetError(f"{source}: {msg}")

    if not isinstance(data, dict):
        fail("top level must be a JSON object")
    missing = REQUIRED_TOP - set(data)
    if missing:
        fail(f"missing required key(s): {', '.join(sorted(missing))}")
    unknown = set(data) - REQUIRED_TOP - OPTIONAL_TOP
    if unknown:
        fail(f"unknown key(s): {', '.join(sorted(unknown))}")

    if not isinstance(data["name"], str) or not data["name"].strip():
        fail("name must be a non-empty string")
    budget = data["budget_minutes"]
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 600:
        fail(f"budget_minutes must be a whole number from 1 to 600, got {budget!r}")
    if not isinstance(data["intent"], str):
        fail("intent must be a string (it may be empty)")

    episodes = data["episodes"]
    if not isinstance(episodes, list) or not episodes:
        fail("episodes must be a non-empty list")
    ids = []
    for i, ep in enumerate(episodes):
        where = f"episodes[{i}]"
        if not isinstance(ep, dict):
            fail(f"{where} must be an object")
        extra = set(ep) - set(EPISODE_FIELDS)
        if extra:
            fail(
                f"{where} has field(s) not allowed in V2 data: {', '.join(sorted(extra))}. "
                "Only public RSS fields are allowed: " + ", ".join(EPISODE_FIELDS)
            )
        for f in REQUIRED_EPISODE_FIELDS:
            if f not in ep:
                fail(f"{where} is missing {f!r}")
        if not isinstance(ep["id"], str) or not ep["id"]:
            fail(f"{where}.id must be a non-empty string")
        d = ep["duration_min"]
        if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
            fail(f"{where} ({ep['id']}) duration_min must be a positive whole number, got {d!r}")
        ids.append(ep["id"])
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        fail(f"duplicate episode id(s): {', '.join(dupes[:5])}")
    id_set = set(ids)

    labels = data["labels"]
    if not isinstance(labels, dict):
        fail("labels must be an object mapping episode id -> label")
    stray = set(labels) - id_set
    if stray:
        fail(f"labels for unknown episode id(s): {', '.join(sorted(stray)[:5])}")
    unlabeled = [x for x in ids if x not in labels]
    if unlabeled:
        fail(f"{len(unlabeled)} episode(s) have no label, e.g. {unlabeled[0]!r}")
    for eid, lab in labels.items():
        if lab not in LABELS:
            fail(f"label for {eid!r} is {lab!r}; must be one of {', '.join(LABELS)}")

    groups = data.get("duplicate_groups", [])
    if not isinstance(groups, list):
        fail("duplicate_groups must be a list of lists of episode ids")
    grouped = set()
    for g, group in enumerate(groups):
        if not isinstance(group, list) or len(group) < 2:
            fail(f"duplicate_groups[{g}] must list at least 2 episode ids")
        for eid in group:
            if eid not in id_set:
                fail(f"duplicate_groups[{g}] names unknown episode id {eid!r}")
            if eid in grouped:
                fail(f"episode {eid!r} is in more than one duplicate group")
            grouped.add(eid)
    return data


def load(path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise DatasetError(f"{path}: cannot read ({e.strerror or e})") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise DatasetError(f"{path}: not valid JSON (line {e.lineno}: {e.msg})") from e
    return validate(data, str(path))


def load_dir(directory) -> list[dict]:
    """Load every *.json directly in `directory` (drafts/ and other subfolders are ignored)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise DatasetError(f"{directory}: no such folder")
    return [load(p) for p in sorted(directory.glob("*.json"))]


def save(data: dict, directory=None) -> Path:
    """Validate, then write to a new file named after the backlog. Never overwrites."""
    validate(data, "new backlog")
    directory = Path(directory or labels_dir())
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", data["name"].lower()).strip("-")[:40] or "backlog"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{slug}-{stamp}.json"
    n = 2
    while path.exists():
        path = directory / f"{slug}-{stamp}-{n}.json"
        n += 1
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path
