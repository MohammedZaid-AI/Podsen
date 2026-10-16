"""Copy data from the old local SQLite file (email+password version) into Postgres.

    python -m v2.migrate_sqlite --dry-run      # show what would be copied
    python -m v2.migrate_sqlite                # copy it

Before running: run v2/supabase_setup.sql once, then sign in to Podsen with Google
once per account you want to keep. Old accounts are matched to Google accounts by
email; an old account with no Google sign-in yet is reported and skipped.

Copies shows, heard episodes, runs and feedback, rewriting the old numeric user ids
to Supabase user ids. Safe to run twice: rows already copied are skipped. The feed and
Apple caches are not copied; they rebuild themselves. Never prints DATABASE_URL.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import select

from v2 import db


def _load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if not os.environ.get("PODSEN_SKIP_DOTENV"):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _old_rows(conn, table: str) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")] if exists else []


def migrate(source: Path, dry_run: bool = False, out=print) -> dict:
    if not source.is_file():
        raise SystemExit(f"No old database at {source}.")
    if not (os.environ.get("DATABASE_URL") or "").strip():
        raise SystemExit("DATABASE_URL is not set; it must point at the Postgres database to copy into.")
    if source.resolve().as_posix() in db.database_url():
        raise SystemExit("DATABASE_URL points at the old file itself; nothing to copy into.")

    old = sqlite3.connect(source)
    old.row_factory = sqlite3.Row
    old_users = _old_rows(old, "users")

    id_map = {}
    for u in old_users:
        target = db.get_user_by_email(u["email"].strip().lower())
        if target:
            id_map[u["id"]] = target["id"]
            out(f"  {u['email']}: matched to your Google account")
        else:
            out(f"  {u['email']}: SKIPPED, sign in with Google as this email once, then run again")

    counts = {"shows": 0, "heard": 0, "runs": 0, "feedback": 0, "already_there": 0}
    with db.engine().begin() as c:
        for row in _old_rows(old, "shows"):
            if row["user_id"] not in id_map:
                continue
            key = (db.shows.c.user_id == id_map[row["user_id"]]) & (db.shows.c.itunes_id == row["itunes_id"])
            if c.execute(select(db.shows.c.itunes_id).where(key)).first():
                counts["already_there"] += 1
                continue
            counts["shows"] += 1
            if not dry_run:
                c.execute(db.shows.insert().values({**row, "user_id": id_map[row["user_id"]]}))

        for row in _old_rows(old, "heard"):
            if row["user_id"] not in id_map:
                continue
            key = (db.heard.c.user_id == id_map[row["user_id"]]) & (db.heard.c.episode_id == row["episode_id"])
            if c.execute(select(db.heard.c.episode_id).where(key)).first():
                counts["already_there"] += 1
                continue
            counts["heard"] += 1
            if not dry_run:
                c.execute(db.heard.insert().values({**row, "user_id": id_map[row["user_id"]]}))

        copied_runs = set()
        for row in _old_rows(old, "runs"):
            if row["user_id"] not in id_map:
                continue
            copied_runs.add(row["id"])
            if c.execute(select(db.runs.c.id).where(db.runs.c.id == row["id"])).first():
                counts["already_there"] += 1
                continue
            counts["runs"] += 1
            if not dry_run:
                c.execute(db.runs.insert().values({**row, "user_id": id_map[row["user_id"]]}))

        for row in _old_rows(old, "feedback"):
            if row["user_id"] not in id_map or row["run_id"] not in copied_runs:
                continue
            same = ((db.feedback.c.run_id == row["run_id"]) & (db.feedback.c.ts == row["ts"])
                    & (db.feedback.c.verdict == row["verdict"]))
            if c.execute(select(db.feedback.c.id).where(same)).first():
                counts["already_there"] += 1
                continue
            counts["feedback"] += 1
            if not dry_run:
                values = {k: v for k, v in row.items() if k != "id"}  # Postgres assigns new ids
                c.execute(db.feedback.insert().values({**values, "user_id": id_map[row["user_id"]]}))
    old.close()

    verb = "Would copy" if dry_run else "Copied"
    out(f"{verb}: {counts['shows']} shows, {counts['heard']} heard, {counts['runs']} runs, "
        f"{counts['feedback']} feedback ({counts['already_there']} already in Postgres, skipped).")
    out(f"Accounts matched: {len(id_map)} of {len(old_users)}.")
    return counts


def main(argv=None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="python -m v2.migrate_sqlite", description=__doc__.split("\n")[0])
    default = Path(os.environ.get("DATA_DIR", "./data")) / "podsen_v2.db"
    ap.add_argument("--source", type=Path, default=default, help=f"old SQLite file (default {default})")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied, change nothing")
    args = ap.parse_args(argv)
    migrate(args.source, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
