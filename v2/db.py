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

from contextlib import contextmanager

from flask import g, has_request_context
from sqlalchemy import (
    Column, Float, ForeignKey, Integer, MetaData, String, Table, Text, create_engine,
    delete, func, literal, select, text, union_all,
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
# Episode descriptions, stored once per (feed, guid), written only in the background
# when a feed is refreshed (backlog.refresh_feed / the queued write after a cache miss).
# The picking path never reads this table; the feed cache stays description-free.
episodes = Table(
    "episodes", metadata,
    Column("feed_url", Text, primary_key=True),
    Column("guid", Text, primary_key=True),
    Column("title", Text),
    Column("description", Text),
    Column("released", String),
    Column("stored", Float, nullable=False),
)
trending_cache = Table(
    "trending_cache", metadata,
    Column("country", String, primary_key=True),
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
        # Supabase's Session pooler caps connections per project (15 on the smallest
        # plan). 2 gunicorn workers x (2 + 3) = 10 at most, leaving room for the
        # dashboard and scripts. Recycle before the pooler drops idle connections.
        kwargs.update(pool_size=2, max_overflow=3, pool_recycle=300)
    eng = create_engine(url, **kwargs)
    _instrument(eng)
    ensure_schema(eng)
    _engine, _engine_url = eng, url
    return eng


def _instrument(eng) -> None:
    """Feed the per-request timing (v2/timing.py): statement count and time, pool
    checkouts, and brand-new connections. Only counts and times, never SQL or values."""
    from sqlalchemy import event

    from v2 import timing

    @event.listens_for(eng, "before_cursor_execute")
    def _before(conn, cursor, statement, params, context, executemany):
        conn.info.setdefault("_podsen_t", []).append(time.perf_counter())

    @event.listens_for(eng, "after_cursor_execute")
    def _after(conn, cursor, statement, params, context, executemany):
        started = conn.info["_podsen_t"].pop()
        timing.add("db", (time.perf_counter() - started) * 1000)

    @event.listens_for(eng.pool, "checkout")
    def _checkout(dbapi_conn, record, proxy):
        timing.add("conn")

    @event.listens_for(eng.pool, "connect")
    def _connect(dbapi_conn, record):
        timing.add("new")


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


# --- one connection per request, and a per-request memo -----------------------------------
# Every query used to check out its own pooled connection, and pool_pre_ping turns
# each checkout into an extra round trip (to Singapore from here: ~125 ms each). Now a
# request checks out ONE connection on first use, runs every query on it (each
# function still in its own transaction), and returns it to the pool at teardown.
# Outside a request (background refreshes, scripts) each call uses its own.

@contextmanager
def _tx():
    if not has_request_context():
        with engine().begin() as c:
            yield c
        return
    c = g.get("_podsen_db")
    if c is None or c.closed:
        c = engine().connect()
        g._podsen_db = c
    if c.in_transaction():  # called from inside another _tx on the same request
        yield c
        return
    with c.begin():
        yield c


def close_request_connection(exc=None) -> None:
    """Flask teardown: give the request's connection back to the pool."""
    c = g.pop("_podsen_db", None)
    if c is not None:
        c.close()


def _memo() -> dict:
    if not has_request_context():
        return {}
    if "_podsen_memo" not in g:
        g._podsen_memo = {}
    return g._podsen_memo


def _forget(user_id: str) -> None:
    for key in ("bundle", "shows", "heard"):
        _memo().pop((key, user_id), None)


HEARD_SEP = "\x1f"  # unit separator: never part of an RSS guid in practice


# --- users -------------------------------------------------------------------------------

def upsert_user(user_id: str, email: str) -> None:
    with _tx() as c:
        existing = c.execute(select(users.c.id).where(users.c.id == user_id)).first()
        if existing:
            c.execute(users.update().where(users.c.id == user_id).values(email=email))
        else:
            c.execute(users.insert().values(id=user_id, email=email, created=time.time()))
    _forget(user_id)


_RUN_COLS = ("ts", "budget", "engine", "payload")


def _bundle(user_id: str, run_id: str | None = None, trending_country: str | None = None,
            with_caches: bool = False):
    """The user, their shows and their heard episode ids in ONE query (memoised per
    request): every logged-in page needs the user, and most need the other two.

    Hints fold in what the page will need next, as extra columns of the same query:
    `run_id` -> that run (only if it is this user's) and its latest verdict;
    `trending_country` -> that country's cached trending chart; `with_caches` -> each
    show's cached feed and Apple episode list (for "Find picks"). Each saves a round
    trip (~125 ms to Singapore from here).
    """
    memo = _memo()
    if ("bundle", user_id) in memo:
        return memo[("bundle", user_id)]
    heard_agg = (select(func.aggregate_strings(heard.c.episode_id, HEARD_SEP))
                 .where(heard.c.user_id == user_id).scalar_subquery())
    extra = []
    if run_id:
        own = (runs.c.id == run_id) & (runs.c.user_id == user_id)
        extra += [select(runs.c[col]).where(own).scalar_subquery().label(f"r_{col}") for col in _RUN_COLS]
        extra.append(select(feedback.c.verdict).where(feedback.c.run_id == run_id)
                     .order_by(feedback.c.id.desc()).limit(1).scalar_subquery().label("r_verdict"))
    if trending_country:
        for col in ("fetched", "payload"):
            extra.append(select(trending_cache.c[col]).where(trending_cache.c.country == trending_country)
                         .scalar_subquery().label(f"t_{col}"))
    joined = users.outerjoin(shows, shows.c.user_id == users.c.id)
    if with_caches:
        joined = (joined.outerjoin(feed_cache, feed_cache.c.feed_url == shows.c.feed_url)
                  .outerjoin(apple_episodes_cache, apple_episodes_cache.c.itunes_id == shows.c.itunes_id))
        extra += [feed_cache.c.fetched.label("f_fetched"), feed_cache.c.payload.label("f_payload"),
                  apple_episodes_cache.c.fetched.label("a_fetched"),
                  apple_episodes_cache.c.payload.label("a_payload")]
    stmt = (select(users.c.id.label("u_id"), users.c.email.label("u_email"),
                   users.c.created.label("u_created"), heard_agg.label("u_heard"), *extra, *shows.c)
            .select_from(joined).where(users.c.id == user_id).order_by(shows.c.added))
    with _tx() as c:
        rows = list(c.execute(stmt))
    if not rows:
        result = None
    else:
        first = rows[0]._mapping
        user = {"id": first["u_id"], "email": first["u_email"], "created": first["u_created"]}
        show_list = [{k: r._mapping[k] for k in shows.c.keys()} for r in rows
                     if r._mapping["itunes_id"] is not None]
        heard_set = set(filter(None, (first["u_heard"] or "").split(HEARD_SEP)))
        result = (user, show_list, heard_set)
        if run_id:
            memo[("run", run_id)] = (None if first["r_payload"] is None else
                                     {"id": run_id, "user_id": user_id,
                                      **{col: first[f"r_{col}"] for col in _RUN_COLS}})
            memo[("verdict", run_id)] = first["r_verdict"]
        if trending_country:
            memo[("cache", "trending", trending_country)] = (
                None if first["t_payload"] is None else (first["t_fetched"], first["t_payload"]))
        if with_caches:
            for r in rows:
                m = r._mapping
                if m["itunes_id"] is None:
                    continue
                memo[("cache", "feed", m["feed_url"])] = (
                    None if m["f_payload"] is None else (m["f_fetched"], m["f_payload"]))
                memo[("cache", "apple", m["itunes_id"])] = (
                    None if m["a_payload"] is None else (m["a_fetched"], m["a_payload"]))
    memo[("bundle", user_id)] = result
    return result


def get_user(user_id: str, run_id: str | None = None, trending_country: str | None = None,
             with_caches: bool = False):
    """The user (or None). Optional hints let the same query bring the page's run,
    trending chart or show caches along (see _bundle)."""
    b = _bundle(user_id, run_id, trending_country, with_caches)
    return dict(b[0]) if b else None


def get_user_by_email(email: str):
    with _tx() as c:
        row = c.execute(select(users).where(users.c.email == email)).first()
    return dict(row._mapping) if row else None


def delete_user(user_id: str) -> None:
    """Remove the user and every row that belongs to them, in one transaction."""
    with _tx() as c:
        for t in USER_TABLES:
            c.execute(delete(t).where(t.c.user_id == user_id))
        c.execute(delete(users).where(users.c.id == user_id))
    _forget(user_id)


def count_rows(user_id: str) -> dict:
    with _tx() as c:
        out = {"users": c.execute(select(func.count()).select_from(users).where(users.c.id == user_id)).scalar()}
        for t in (shows, heard, runs, feedback):
            out[t.name] = c.execute(select(func.count()).select_from(t).where(t.c.user_id == user_id)).scalar()
    return out


# --- shows -------------------------------------------------------------------------------

def add_show(user_id: str, show: dict) -> None:
    with _tx() as c:
        _upsert(c, shows, {
            "user_id": user_id, "itunes_id": show["itunes_id"], "name": show["name"],
            "artist": show.get("artist"), "feed_url": show["feed_url"],
            "artwork_url": show.get("artwork_url"), "apple_url": show.get("apple_url"),
            "added": time.time(),
        }, ["user_id", "itunes_id"])
    _forget(user_id)


def remove_show(user_id: str, itunes_id: str) -> None:
    with _tx() as c:
        c.execute(delete(shows).where(shows.c.user_id == user_id, shows.c.itunes_id == itunes_id))
    _forget(user_id)


def list_shows(user_id: str) -> list[dict]:
    b = _bundle(user_id)
    if b is not None:
        return [dict(s) for s in b[1]]
    with _tx() as c:  # shows of a user row that doesn't exist (scripts, tests)
        return _rows(c.execute(select(shows).where(shows.c.user_id == user_id).order_by(shows.c.added)))


# --- heard -------------------------------------------------------------------------------

def mark_heard(user_id: str, episode_id: str) -> None:
    with _tx() as c:
        _upsert(c, heard, {"user_id": user_id, "episode_id": episode_id, "ts": time.time()},
                ["user_id", "episode_id"], update=False)
    _forget(user_id)


def heard_ids(user_id: str) -> set[str]:
    b = _bundle(user_id)
    if b is not None:
        return set(b[2])
    with _tx() as c:
        return {r.episode_id for r in c.execute(select(heard.c.episode_id).where(heard.c.user_id == user_id))}


# --- caches (public RSS / Apple data, not user data) -------------------------------------
# Readers get (payload, age_seconds) or None, whatever the age: stale-while-revalidate
# is the caller's decision (v2/refresh.py), not a hard expiry here.

_CACHES = {
    "feed": (feed_cache, feed_cache.c.feed_url),
    "apple": (apple_episodes_cache, apple_episodes_cache.c.itunes_id),
    "trending": (trending_cache, trending_cache.c.country),
}


def prefetch_caches(feed_urls=(), itunes_ids=()) -> None:
    """Load many feed and Apple cache rows in ONE query into the request memo, so the
    per-show lookups that follow cost no round trips."""
    memo = _memo()
    parts = []
    feed_urls = [k for k in feed_urls if ("cache", "feed", k) not in memo]  # already loaded
    itunes_ids = [k for k in itunes_ids if ("cache", "apple", k) not in memo]
    for kind, keys in (("feed", list(feed_urls)), ("apple", list(itunes_ids))):
        if keys:
            table, key_col = _CACHES[kind]
            parts.append(select(literal(kind).label("kind"), key_col.label("k"), table.c.fetched,
                                table.c.payload).where(key_col.in_(keys)))
    if not parts:
        return
    stmt = parts[0] if len(parts) == 1 else union_all(*parts)
    with _tx() as c:
        rows = list(c.execute(stmt))
    for kind, keys in (("feed", feed_urls), ("apple", itunes_ids)):
        for k in keys:
            memo[("cache", kind, k)] = None  # known miss unless a row below fills it
    for r in rows:
        memo[("cache", r.kind, r.k)] = (r.fetched, r.payload)


def cache_entry(kind: str, key: str):
    """(payload, age_seconds) or None. Uses the request memo when prefetched."""
    memo = _memo()
    if ("cache", kind, key) in memo:
        raw = memo[("cache", kind, key)]
    else:
        table, key_col = _CACHES[kind]
        with _tx() as c:
            row = c.execute(select(table.c.fetched, table.c.payload).where(key_col == key)).first()
        raw = (row.fetched, row.payload) if row else None
        memo[("cache", kind, key)] = raw
    if raw is None:
        return None
    return json.loads(raw[1]), time.time() - raw[0]


def store_cache(kind: str, items: dict) -> None:
    """Upsert several cache rows of one kind in ONE statement: {key: payload}."""
    if not items:
        return
    table, key_col = _CACHES[kind]
    now = time.time()
    values = [{key_col.name: k, "fetched": now, "payload": json.dumps(v, ensure_ascii=False)}
              for k, v in items.items()]
    with _tx() as c:
        ins = (pg_insert if c.dialect.name == "postgresql" else sqlite_insert)(table).values(values)
        c.execute(ins.on_conflict_do_update(index_elements=[key_col.name],
                                            set_={"fetched": ins.excluded.fetched,
                                                  "payload": ins.excluded.payload}))
    memo = _memo()
    for k, v in items.items():
        memo[("cache", kind, k)] = (now, json.dumps(v, ensure_ascii=False))


def _fresh(kind, key, max_age_s):
    e = cache_entry(kind, key)
    return None if e is None or e[1] > max_age_s else e[0]


# Older names, kept for scripts and tests: fresh-only reads and single-row writes.
def cached_feed(feed_url: str, max_age_s: float):
    return _fresh("feed", feed_url, max_age_s)


def store_feed(feed_url: str, parsed: dict) -> None:
    store_cache("feed", {feed_url: parsed})


def cached_apple_episodes(itunes_id: str, max_age_s: float):
    return _fresh("apple", itunes_id, max_age_s)


def store_apple_episodes(itunes_id: str, episodes: list[dict]) -> None:
    store_cache("apple", {itunes_id: episodes})


def cached_trending(country: str, max_age_s: float):
    return _fresh("trending", country, max_age_s)


def store_trending(country: str, shows_list: list[dict]) -> None:
    store_cache("trending", {country: shows_list})


# --- episode descriptions (background writes; not used by the picking path) -------------

def store_descriptions(feed_url: str, eps: list[dict]) -> int:
    """Store the descriptions of a feed's episodes not stored yet; never rewrite one.
    Reads only the guids already stored for this feed, then inserts the new rows.
    Returns how many were added."""
    with _tx() as c:
        have = {r.guid for r in c.execute(select(episodes.c.guid).where(episodes.c.feed_url == feed_url))}
        now = time.time()
        new, seen = [], set()
        for e in eps:
            guid = e.get("id")
            if not guid or guid in have or guid in seen:
                continue
            seen.add(guid)
            new.append({"feed_url": feed_url, "guid": guid, "title": e.get("title"),
                        "description": e.get("description") or "", "released": e.get("released"),
                        "stored": now})
        insert = pg_insert if c.dialect.name == "postgresql" else sqlite_insert
        for i in range(0, len(new), 150):  # 6 columns x 150 rows: under every SQLite variable limit
            c.execute(insert(episodes).values(new[i:i + 150]).on_conflict_do_nothing(
                index_elements=["feed_url", "guid"]))
    return len(new)


def episode_descriptions(feed_url: str, guids) -> dict[str, str]:
    """{guid: description} for later layers. Not called on the Today / picking path."""
    guids = list(guids)
    if not guids:
        return {}
    with _tx() as c:
        rows = c.execute(select(episodes.c.guid, episodes.c.description)
                         .where(episodes.c.feed_url == feed_url, episodes.c.guid.in_(guids)))
        return {r.guid: r.description for r in rows}


# --- runs + feedback ---------------------------------------------------------------------

def save_run(user_id: str, budget: int, engine_name: str, payload: dict) -> str:
    run_id = uuid.uuid4().hex
    with _tx() as c:
        c.execute(runs.insert().values(
            id=run_id, user_id=user_id, ts=time.time(), budget=budget, engine=engine_name,
            payload=json.dumps(payload, ensure_ascii=False),
        ))
    return run_id


def get_run(run_id: str, user_id: str):
    """Only the owner's run; anyone else gets None, same as a missing run. Its latest
    feedback verdict comes back in the same query (and is memoised for
    latest_feedback)."""
    memo = _memo()
    if ("run", run_id) in memo:  # brought along by the request's first query
        r = memo[("run", run_id)]
        return None if r is None else {**r, "payload": json.loads(r["payload"])}
    verdict = (select(feedback.c.verdict).where(feedback.c.run_id == runs.c.id)
               .order_by(feedback.c.id.desc()).limit(1).scalar_subquery())
    with _tx() as c:
        row = c.execute(select(runs, verdict.label("latest_verdict"))
                        .where(runs.c.id == run_id, runs.c.user_id == user_id)).first()
    if row is None:
        return None
    r = dict(row._mapping)
    _memo()[("verdict", run_id)] = r.pop("latest_verdict")
    return {**r, "payload": json.loads(r["payload"])}


def save_feedback(run: dict, verdict: str) -> None:
    # Copied from the stored run, never from the form: what we record is what was shown.
    picks = [
        {"id": p["id"], "title": p["title"], "show": p["show"], "duration_min": p["duration_min"]}
        for p in run["payload"]["picks"]
    ]
    with _tx() as c:
        c.execute(feedback.insert().values(
            run_id=run["id"], user_id=run["user_id"], ts=time.time(), verdict=verdict,
            budget=run["budget"], engine=run["engine"], picks=json.dumps(picks, ensure_ascii=False),
        ))
    _memo().pop(("verdict", run["id"]), None)


def latest_feedback(run_id: str):
    memo = _memo()
    if ("verdict", run_id) in memo:
        return memo[("verdict", run_id)]
    with _tx() as c:
        row = c.execute(select(feedback.c.verdict).where(feedback.c.run_id == run_id)
                        .order_by(feedback.c.id.desc()).limit(1)).first()
    return row.verdict if row else None


def all_runs() -> list[dict]:
    """Every run for /admin: what was picked, for whom (user id only), and any failures."""
    with _tx() as c:
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
    with _tx() as c:
        return _rows(c.execute(select(feedback).order_by(feedback.c.id)))
