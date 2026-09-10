"""Flat append-only event log.

Inspect with: cat $DATA_DIR/events.jsonl   (or GET /admin?key=)

Format is byte-compatible with the Node version's events.jsonl: one JSON object
per line, `ts` first, ISO-8601 with milliseconds and a `Z` suffix, non-ASCII left
raw (JS JSON.stringify does not escape it either).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(os.environ.get("DATA_DIR", "./data"))
DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    # Matches JS new Date().toISOString() exactly: 2026-09-07T14:23:45.123Z
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append(file: str, obj: dict) -> None:
    line = json.dumps({"ts": _now(), **obj}, ensure_ascii=False)
    # Single sub-4KB write with O_APPEND stays atomic across gunicorn workers.
    with open(DIR / file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_all(file: str) -> list[dict]:
    path = DIR / file
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- per-user preferences -----------------------------------------------------
# events.jsonl is an append-only audit log; a preference is mutable state, so it
# lives in its own small file rather than being reconstructed by replaying events.
# ponytail: whole-file read/modify/write, fine for a handful of users. If this ever
# has real concurrency, move it to SQLite rather than adding locking here.
PREFS = "prefs.json"


def read_prefs(user_id: str) -> dict:
    path = DIR / PREFS
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get(user_id, {})
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt prefs file must never break a triage run


def write_pref(user_id: str, key: str, value) -> None:
    path = DIR / PREFS
    try:
        with open(path, "r", encoding="utf-8") as f:
            all_prefs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        all_prefs = {}

    user = all_prefs.setdefault(user_id, {})
    if value is None:
        user.pop(key, None)
    else:
        user[key] = value

    # Write-then-replace: a crash mid-write leaves the old file intact, not a
    # truncated one that would read back as "no preferences".
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(all_prefs, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
