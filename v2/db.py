"""SQLite storage for the V2 app: DATA_DIR/podsen_v2.db, stdlib sqlite3 only.

One short-lived connection per call keeps this safe across Flask's threads without a
pool. Real user data lives here, under the gitignored DATA_DIR, never in git.
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
  email TEXT NOT NULL,
  ts REAL NOT NULL,
  ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS login_failures_email ON login_failures (email, ts);
CREATE TABLE IF NOT EXISTS shows (
  user_id INTEGER NOT NULL REFERENCES users (id),
  itunes_id TEXT NOT NULL,
  name TEXT NOT NULL,
  artist TEXT,
  feed_url TEXT NOT NULL,
  artwork_url TEXT,
  apple_url TEXT,
  added REAL NOT NULL,
  PRIMARY KEY (user_id, itunes_id)
);
CREATE TABLE IF NOT EXISTS heard (
  user_id INTEGER NOT NULL REFERENCES users (id),
  episode_id TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY (user_id, episode_id)
);
CREATE TABLE IF NOT EXISTS feed_cache (
  feed_url TEXT PRIMARY KEY,
  fetched REAL NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS apple_episodes_cache (
  itunes_id TEXT PRIMARY KEY,
  fetched REAL NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users (id),
  ts REAL NOT NULL,
  budget INTEGER NOT NULL,
  engine TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs (id),
  user_id INTEGER NOT NULL REFERENCES users (id),
  ts REAL NOT NULL,
  verdict TEXT NOT NULL,
  budget INTEGER NOT NULL,
  engine TEXT NOT NULL,
  picks TEXT NOT NULL
);
"""


def _migrate(conn) -> None:
    # A database created before failures were counted per IP lacks the column.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(login_failures)")}
    if "ip" not in cols:
        conn.execute("ALTER TABLE login_failures ADD COLUMN ip TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures (ip, ts)")


def path() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")) / "podsen_v2.db"


@contextmanager
def connect():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- users + login throttling ---------------------------------------------------

def create_user(email: str, password_hash: str) -> int | None:
    """Returns the new id, or None if the email is taken."""
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (email, password_hash, created) VALUES (?, ?, ?)",
                (email, password_hash, time.time()),
            )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid


def get_user_by_email(email: str):
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user(user_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def recent_failures(email: str, ip: str, window_s: float) -> tuple[list[float], list[float]]:
    """Failure times in the window: (for this email from this IP, for this IP overall).

    Counting the email per IP means someone guessing at your account from their
    machine locks only themselves out, not you logging in from yours.
    """
    with connect() as c:
        c.execute("DELETE FROM login_failures WHERE ts < ?", (time.time() - window_s,))
        pair = c.execute(
            "SELECT ts FROM login_failures WHERE email = ? AND ip = ? ORDER BY ts", (email, ip)
        ).fetchall()
        by_ip = c.execute(
            "SELECT ts FROM login_failures WHERE ip = ? ORDER BY ts", (ip,)
        ).fetchall()
    return [r["ts"] for r in pair], [r["ts"] for r in by_ip]


def record_failure(email: str, ip: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO login_failures (email, ts, ip) VALUES (?, ?, ?)", (email, time.time(), ip)
        )


def clear_failures(email: str, ip: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM login_failures WHERE email = ? AND ip = ?", (email, ip))


def delete_user(user_id: int) -> None:
    """Remove the user and every row that belongs to them, in one transaction."""
    with connect() as c:
        row = c.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        c.execute("DELETE FROM feedback WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM runs WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM heard WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM shows WHERE user_id = ?", (user_id,))
        if row:
            c.execute("DELETE FROM login_failures WHERE email = ?", (row["email"],))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_rows(user_id: int) -> dict:
    with connect() as c:
        return {
            t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (user_id,)).fetchone()[0]
            for t, col in (("users", "id"), ("shows", "user_id"), ("heard", "user_id"),
                           ("runs", "user_id"), ("feedback", "user_id"))
        }


# --- shows ---------------------------------------------------------------------------

def add_show(user_id: int, show: dict) -> None:
    with connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO shows
               (user_id, itunes_id, name, artist, feed_url, artwork_url, apple_url, added)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, show["itunes_id"], show["name"], show.get("artist"),
                show["feed_url"], show.get("artwork_url"), show.get("apple_url"), time.time(),
            ),
        )


def remove_show(user_id: int, itunes_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM shows WHERE user_id = ? AND itunes_id = ?", (user_id, itunes_id))


def list_shows(user_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM shows WHERE user_id = ? ORDER BY added", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- heard -----------------------------------------------------------------------------

def mark_heard(user_id: int, episode_id: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO heard (user_id, episode_id, ts) VALUES (?, ?, ?)",
            (user_id, episode_id, time.time()),
        )


def heard_ids(user_id: int) -> set[str]:
    with connect() as c:
        rows = c.execute("SELECT episode_id FROM heard WHERE user_id = ?", (user_id,)).fetchall()
    return {r["episode_id"] for r in rows}


# --- feed cache (public RSS data, not user data) ----------------------------------------

def cached_feed(feed_url: str, max_age_s: float):
    with connect() as c:
        row = c.execute(
            "SELECT fetched, payload FROM feed_cache WHERE feed_url = ?", (feed_url,)
        ).fetchone()
    if row is None or time.time() - row["fetched"] > max_age_s:
        return None
    return json.loads(row["payload"])


def store_feed(feed_url: str, parsed: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO feed_cache (feed_url, fetched, payload) VALUES (?, ?, ?)",
            (feed_url, time.time(), json.dumps(parsed, ensure_ascii=False)),
        )


def cached_apple_episodes(itunes_id: str, max_age_s: float):
    with connect() as c:
        row = c.execute(
            "SELECT fetched, payload FROM apple_episodes_cache WHERE itunes_id = ?", (itunes_id,)
        ).fetchone()
    if row is None or time.time() - row["fetched"] > max_age_s:
        return None
    return json.loads(row["payload"])


def store_apple_episodes(itunes_id: str, episodes: list[dict]) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO apple_episodes_cache (itunes_id, fetched, payload) VALUES (?, ?, ?)",
            (itunes_id, time.time(), json.dumps(episodes, ensure_ascii=False)),
        )


# --- runs + feedback --------------------------------------------------------------------

def save_run(user_id: int, budget: int, engine: str, payload: dict) -> str:
    run_id = uuid.uuid4().hex
    with connect() as c:
        c.execute(
            "INSERT INTO runs (id, user_id, ts, budget, engine, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, user_id, time.time(), budget, engine, json.dumps(payload, ensure_ascii=False)),
        )
    return run_id


def get_run(run_id: str, user_id: int):
    """Only the owner's run; anyone else gets None, same as a missing run."""
    with connect() as c:
        row = c.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)
        ).fetchone()
    if row is None:
        return None
    return {**dict(row), "payload": json.loads(row["payload"])}


def save_feedback(run: dict, verdict: str) -> None:
    # Copied from the stored run, never from the form: what we record is what was shown.
    picks = [
        {"id": p["id"], "title": p["title"], "show": p["show"], "duration_min": p["duration_min"]}
        for p in run["payload"]["picks"]
    ]
    with connect() as c:
        c.execute(
            """INSERT INTO feedback (run_id, user_id, ts, verdict, budget, engine, picks)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run["id"], run["user_id"], time.time(), verdict, run["budget"], run["engine"],
             json.dumps(picks, ensure_ascii=False)),
        )


def latest_feedback(run_id: str):
    with connect() as c:
        row = c.execute(
            "SELECT verdict FROM feedback WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
    return row["verdict"] if row else None


def all_feedback() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY id").fetchall()]
