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
