"""Storage for the V2 app, on SQLAlchemy Core so one code path runs everywhere.

Production: Supabase Postgres via DATABASE_URL. Tests and local fallback: SQLite at
DATA_DIR/podsen_v2.db when DATABASE_URL is unset. Users are keyed by their Supabase
auth user id. Real user data never lives in git (DATA_DIR is gitignored).
"""

import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

from sqlalchemy import (
    Column, Float, ForeignKey, Integer, MetaData, String, Table, Text, create_engine,
    delete, func, select, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", String, primary_key=True),  # Supabase auth user id (uuid)
    Column("email", String, nullable=False, index=True),
    Column("created", Float, nullable=False),
)
shows = Table(
    "shows", metadata,
    Column("user_id", String, ForeignKey("users.id"), primary_key=True),
    Column("itunes_id", String, primary_key=True),
    Column("name", Text, nullable=False),
    Column("artist", Text),
    Column("feed_url", Text, nullable=False),
    Column("artwork_url", Text),
    Column("apple_url", Text),
    Column("added", Float, nullable=False),
)
heard = Table(
    "heard", metadata,
    Column("user_id", String, ForeignKey("users.id"), primary_key=True),
    Column("episode_id", Text, primary_key=True),
    Column("ts", Float, nullable=False),
)
feed_cache = Table(
    "feed_cache", metadata,
    Column("feed_url", Text, primary_key=True),
    Column("fetched", Float, nullable=False),
    Column("payload", Text, nullable=False),
)
apple_episodes_cache = Table(
    "apple_episodes_cache", metadata,
    Column("itunes_id", String, primary_key=True),
    Column("fetched", Float, nullable=False),
    Column("payload", Text, nullable=False),
)
runs = Table(
    "runs", metadata,
    Column("id", String, primary_key=True),
    Column("user_id", String, ForeignKey("users.id"), nullable=False, index=True),
    Column("ts", Float, nullable=False),
    Column("budget", Integer, nullable=False),
    Column("engine", String, nullable=False),
    Column("payload", Text, nullable=False),
)
feedback = Table(
    "feedback", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("user_id", String, ForeignKey("users.id"), nullable=False),
    Column("ts", Float, nullable=False),
    Column("verdict", String, nullable=False),
    Column("budget", Integer, nullable=False),
    Column("engine", String, nullable=False),
    Column("picks", Text, nullable=False),
)
USER_TABLES = (feedback, runs, heard, shows)  # deletion order: children first


# --- engine ------------------------------------------------------------------------------

def database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        p = Path(os.environ.get("DATA_DIR", "./data")) / "podsen_v2.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p.resolve().as_posix()}"
    # Supabase hands out postgres:// or postgresql:// URLs; we use the psycopg 3 driver.
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + _encode_password(url[len(prefix):])
    return url


def _encode_password(rest: str) -> str:
    """URL-encode the password in "user:password@host...".

    Supabase's dashboard pastes the password in raw, so one containing @ : / # ? breaks
    URL parsing (the first @ is taken as the end of the password). A hostname can never
    contain @, so the LAST @ is the real separator. An already-encoded password is
    decoded first, so it is never encoded twice.
    """
    userinfo, at, hostpart = rest.rpartition("@")
    if not at or ":" not in userinfo:
        return rest
    user, password = userinfo.split(":", 1)
    return f"{user}:{quote(unquote(password), safe='')}@{hostpart}"


_engine = None
_engine_url = None


def engine():
    """One engine per URL, created (and the schema ensured) on first use.

    Nothing connects at import time, so importing the app never touches a database.
    """
    global _engine, _engine_url
    url = database_url()
    if _engine is not None and _engine_url == url:
        return _engine
    if _engine is not None:
        _engine.dispose()
    kwargs = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        # Supabase's transaction pooler (port 6543) can't hold prepared statements.
        kwargs["connect_args"] = {"prepare_threshold": None}
    eng = create_engine(url, **kwargs)
    ensure_schema(eng)
    _engine, _engine_url = eng, url
    return eng


def ensure_schema(eng) -> None:
    """Create missing tables. On Postgres, also make sure RLS is on for every table.

    Supabase exposes the public schema through its REST API to anyone holding the anon
    key. RLS with no policies blocks all of that; our server connects directly as the
    table owner and is not affected. Done here as well as in supabase_setup.sql so a
    table created by the app can never end up readable through the API.
    """
    if eng.dialect.name == "sqlite":
        with eng.connect() as c:
            cols = {r[1] for r in c.execute(text("PRAGMA table_info(users)"))}
        if "password_hash" in cols:
            raise RuntimeError(
                "This SQLite file is from the old email+password version of Podsen. Copy it "
                "to Postgres with `python -m v2.migrate_sqlite`, then rename it (for example "
                "to podsen_v2.old.db) so the app can start a fresh local database."
            )
    metadata.create_all(eng)
    if eng.dialect.name == "postgresql":
        with eng.begin() as c:
            for t in metadata.sorted_tables:
                c.execute(text(f'ALTER TABLE "{t.name}" ENABLE ROW LEVEL SECURITY'))


def _upsert(conn, table, values: dict, keys: list[str], update: bool = True):
    ins = (pg_insert if conn.dialect.name == "postgresql" else sqlite_insert)(table).values(**values)
    if update:
        stmt = ins.on_conflict_do_update(
            index_elements=keys, set_={k: v for k, v in values.items() if k not in keys}
        )
    else:
        stmt = ins.on_conflict_do_nothing(index_elements=keys)
    conn.execute(stmt)


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


# --- users -------------------------------------------------------------------------------

def upsert_user(user_id: str, email: str) -> None:
    with engine().begin() as c:
        existing = c.execute(select(users.c.id).where(users.c.id == user_id)).first()
        if existing:
            c.execute(users.update().where(users.c.id == user_id).values(email=email))
        else:
            c.execute(users.insert().values(id=user_id, email=email, created=time.time()))


def get_user(user_id: str):
    with engine().connect() as c:
        row = c.execute(select(users).where(users.c.id == user_id)).first()
    return dict(row._mapping) if row else None


def get_user_by_email(email: str):
    with engine().connect() as c:
        row = c.execute(select(users).where(users.c.email == email)).first()
    return dict(row._mapping) if row else None


def delete_user(user_id: str) -> None:
    """Remove the user and every row that belongs to them, in one transaction."""
    with engine().begin() as c:
        for t in USER_TABLES:
            c.execute(delete(t).where(t.c.user_id == user_id))
        c.execute(delete(users).where(users.c.id == user_id))


def count_rows(user_id: str) -> dict:
    with engine().connect() as c:
        out = {"users": c.execute(select(func.count()).select_from(users).where(users.c.id == user_id)).scalar()}
        for t in (shows, heard, runs, feedback):
            out[t.name] = c.execute(select(func.count()).select_from(t).where(t.c.user_id == user_id)).scalar()
    return out


# --- shows -------------------------------------------------------------------------------

def add_show(user_id: str, show: dict) -> None:
    with engine().begin() as c:
        _upsert(c, shows, {
            "user_id": user_id, "itunes_id": show["itunes_id"], "name": show["name"],
            "artist": show.get("artist"), "feed_url": show["feed_url"],
            "artwork_url": show.get("artwork_url"), "apple_url": show.get("apple_url"),
            "added": time.time(),
        }, ["user_id", "itunes_id"])


def remove_show(user_id: str, itunes_id: str) -> None:
    with engine().begin() as c:
        c.execute(delete(shows).where(shows.c.user_id == user_id, shows.c.itunes_id == itunes_id))


def list_shows(user_id: str) -> list[dict]:
    with engine().connect() as c:
        return _rows(c.execute(select(shows).where(shows.c.user_id == user_id).order_by(shows.c.added)))


# --- heard -------------------------------------------------------------------------------

def mark_heard(user_id: str, episode_id: str) -> None:
    with engine().begin() as c:
        _upsert(c, heard, {"user_id": user_id, "episode_id": episode_id, "ts": time.time()},
                ["user_id", "episode_id"], update=False)


def heard_ids(user_id: str) -> set[str]:
    with engine().connect() as c:
        return {r.episode_id for r in c.execute(select(heard.c.episode_id).where(heard.c.user_id == user_id))}


# --- caches (public RSS / Apple data, not user data) -------------------------------------

def _cached(table, key_col, key, max_age_s):
    with engine().connect() as c:
        row = c.execute(select(table.c.fetched, table.c.payload).where(key_col == key)).first()
    if row is None or time.time() - row.fetched > max_age_s:
        return None
    return json.loads(row.payload)


def cached_feed(feed_url: str, max_age_s: float):
    return _cached(feed_cache, feed_cache.c.feed_url, feed_url, max_age_s)


def store_feed(feed_url: str, parsed: dict) -> None:
    with engine().begin() as c:
        _upsert(c, feed_cache, {"feed_url": feed_url, "fetched": time.time(),
                                "payload": json.dumps(parsed, ensure_ascii=False)}, ["feed_url"])


def cached_apple_episodes(itunes_id: str, max_age_s: float):
    return _cached(apple_episodes_cache, apple_episodes_cache.c.itunes_id, itunes_id, max_age_s)


def store_apple_episodes(itunes_id: str, episodes: list[dict]) -> None:
    with engine().begin() as c:
        _upsert(c, apple_episodes_cache, {"itunes_id": itunes_id, "fetched": time.time(),
                                          "payload": json.dumps(episodes, ensure_ascii=False)},
                ["itunes_id"])


# --- runs + feedback ---------------------------------------------------------------------

def save_run(user_id: str, budget: int, engine_name: str, payload: dict) -> str:
    run_id = uuid.uuid4().hex
    with engine().begin() as c:
        c.execute(runs.insert().values(
            id=run_id, user_id=user_id, ts=time.time(), budget=budget, engine=engine_name,
            payload=json.dumps(payload, ensure_ascii=False),
        ))
    return run_id


def get_run(run_id: str, user_id: str):
    """Only the owner's run; anyone else gets None, same as a missing run."""
    with engine().connect() as c:
        row = c.execute(select(runs).where(runs.c.id == run_id, runs.c.user_id == user_id)).first()
    if row is None:
        return None
    r = dict(row._mapping)
    return {**r, "payload": json.loads(r["payload"])}


def save_feedback(run: dict, verdict: str) -> None:
    # Copied from the stored run, never from the form: what we record is what was shown.
    picks = [
        {"id": p["id"], "title": p["title"], "show": p["show"], "duration_min": p["duration_min"]}
        for p in run["payload"]["picks"]
    ]
    with engine().begin() as c:
        c.execute(feedback.insert().values(
            run_id=run["id"], user_id=run["user_id"], ts=time.time(), verdict=verdict,
            budget=run["budget"], engine=run["engine"], picks=json.dumps(picks, ensure_ascii=False),
        ))


def latest_feedback(run_id: str):
    with engine().connect() as c:
        row = c.execute(select(feedback.c.verdict).where(feedback.c.run_id == run_id)
                        .order_by(feedback.c.id.desc()).limit(1)).first()
    return row.verdict if row else None


def all_runs() -> list[dict]:
    """Every run for /admin: what was picked, for whom (user id only), and any failures."""
    with engine().connect() as c:
        rows = _rows(c.execute(select(runs).order_by(runs.c.ts)))
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        out.append({
            "id": r["id"], "user_id": r["user_id"], "ts": r["ts"], "budget": r["budget"],
            "engine": r["engine"], "backlog_size": p.get("backlog_size"),
            "picks": [{k: x.get(k) for k in ("id", "title", "show", "duration_min")}
                      for x in p.get("picks", [])],
            "skips": len(p.get("skips", [])), "failed": p.get("failed", []),
        })
    return out


def all_feedback() -> list[dict]:
    with engine().connect() as c:
        return _rows(c.execute(select(feedback).order_by(feedback.c.id)))
