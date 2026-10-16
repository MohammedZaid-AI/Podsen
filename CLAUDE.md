# Podsen

Podcast triage. Users sign in with Google (Supabase Auth), add the shows they follow
(found through Apple's free iTunes search), and say how much time they have right now. Podsen reads
each show's public RSS feed and picks the set of recent episodes that fits that time,
lists what's safe to skip, and gives play links for each pick: an in-page audio
player, Spotify search, Apple Podcasts (exact episode when it can be matched),
YouTube search and Amazon Music search. Not a discovery tool — only shows they chose.

Target user: follows 10+ shows, 100+ unheard episodes, refuses to unsubscribe.
Wants help deciding, not permission to prune.

**Stage: prototype for ~5 test users. Validating "are the picks any good", nothing else.**

## Commands

```bash
.venv\Scripts\Activate.ps1
flask --app app run --port 3000      # `flask run` reads .env; `python app.py` does NOT
python -m pytest test_rank.py test_v2.py test_v2_app.py -q   # 193 tests, no network
python -m v2.report                  # compare pick methods on data/labels/
python -m v2.report --dir v2/sample_data   # same, on the bundled FAKE samples
python -m v2.migrate_sqlite --dry-run      # copy old local SQLite data into Postgres
```

`/` redirects to the app at `/v2/`. `/label` (hand-label backlogs for evaluation) and
`/admin?key=ADMIN_KEY` (runs + feedback as JSON) are operator pages.

There is **no auto-reload** — after editing, kill the process on port 3000 and
restart, or you will test stale code. Verifying a change against a server that wasn't
restarted has produced false "the fix didn't work" conclusions before.

## Files

| File | Role |
|---|---|
| `app.py` | Flask app: registers the V2 Blueprint, `/` redirect, `/label`, `/admin` |
| `views.py` | shared HTML shell: `esc()` and `page()` (styles, header) |
| `rank.py` | the only LLM call; Groq (default) or Claude behind one `rank()` |
| `v2/web.py` | the app's routes (Blueprint under `/v2`): accounts, shows, today, runs |
| `v2/app_views.py` | the app's HTML builders, play/search links |
| `v2/auth.py` | Google sign-in via Supabase Auth (PKCE, server side) + admin user delete |
| `v2/db.py` | SQLAlchemy Core: Postgres via `DATABASE_URL`, else SQLite `DATA_DIR/podsen_v2.db` |
| `v2/supabase_setup.sql` | tables + RLS for Supabase; run once in the SQL editor |
| `v2/migrate_sqlite.py` | one-off copy of the old email+password SQLite data into Postgres |
| `v2/itunes.py` | Apple iTunes search/lookup, incl. a show's episode list |
| `v2/rss.py` | RSS → episodes (stdlib). Durations round UP: the budget is a hard cap |
| `v2/backlog.py` | a user's backlog: last 30 days of their feeds, minus heard; feed cache |
| `v2/engine.py` | `pick()` — the ONE place the pick method is chosen (now `newest_first`) |
| `v2/links.py` | exact-episode Apple Podcasts links (guid, then exact title) |
| `v2/baselines.py` | `newest_first`, `random_fill`, `v1_llm` (uses `rank.py`) |
| `v2/dataset.py` | label file format + validation, saved under `DATA_DIR/labels/` |
| `v2/metrics.py`, `v2/report.py` | evaluation metrics, bootstrap CIs, `python -m v2.report` |
| `v2/label_views.py` | HTML for `/label` |
| `v2/sample_data/` | 3 **FAKE** labeled backlogs so the report runs without real labels |
| `conftest.py` | test safety net: no test can reach the real Supabase (see below) |

## Lessons that cost real time — keep them

**Never ask the model to respect a hard number.** A 15-minute budget once returned a
61-minute episode and a 60-minute budget returned 119 minutes of audio. Anything
numeric — the time budget above all — is enforced in code. `rank.fit_to_budget()`
filters before the model sees anything; every V2 method goes through a hard cap
(`baselines.enforce_budget`, re-checked in `engine.pick()`).

**Report partial results; never hide them.** A feed that fails to load is shown on
the results page ("Could not load 1 show: X"), never silently dropped — picks from
a partial backlog must not look complete. Same in `/label` and the report (a method
that errors shows FAILED, not a partial row).

**Escape every interpolated value** with `views.esc()`. Episode titles, descriptions,
show names and every URL come from public feeds or Apple and are untrusted. Outbound
search links are URL-encoded *and* HTML-escaped; only http(s) audio and https Apple
links are ever rendered.

**Never claim more than a link does.** "Open in Apple Podcasts" only when the exact
episode was matched; otherwise "Search on Apple Podcasts". YouTube and Amazon Music
are labelled as searches.

## Rules

- **Data rule: public RSS + hand labels only.** Spotify's developer policy bars feeding
  Spotify data into AI/ML. No module named `spotify` may be imported anywhere, and no
  Spotify API URL may appear in the repo; the only Spotify string allowed is the
  `open.spotify.com/search/` link-out. `test_v2.py` enforces this (AST + repo scan) —
  don't weaken it to make something pass.
- No client JS, no build step, no templates directory. Server-rendered strings only.
- No mock data in the product flow. Test doubles are fine; the FAKE samples are for
  the report only and are marked FAKE in their names and contents.
- `data/` and `.env` are gitignored — real user data and labels must never be committed.
- **Secrets:** never print `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` or
  `DATABASE_URL`. The service-role key is used in one place (`auth.delete_auth_user`),
  only in a request header; a test checks every response and log line for it. The
  500 page shows only the error type, never the message (DB errors carry hostnames).
- **Tests never touch real services.** `conftest.py` sets `PODSEN_SKIP_DOTENV=1` before
  anything is imported (so `.env` is never loaded in tests), removes `DATABASE_URL`
  (SQLite in a temp dir) and points Supabase at fakes on a `.invalid` domain.

## How the app works

- **Sign-in:** "Continue with Google" only, via Supabase Auth's PKCE flow, no client
  JS. `/v2/login` makes a code_verifier (kept in the session as `v2_pkce`, single use)
  and redirects to `{SUPABASE_URL}/auth/v1/authorize?provider=google&...&code_challenge`.
  `/v2/auth/callback?code=` exchanges it at `/auth/v1/token?grant_type=pkce` with the
  anon key. The callback URL is built from the request host; Supabase only redirects to
  URLs on its allow-list. Errors (cancelled, used/expired code, missing or wrong
  verifier, Supabase down) each get a plain message and a retry button.
- **Session:** `v2_user` = the Supabase user id; logins last 30 days (permanent
  session, `PERMANENT_SESSION_LIFETIME`). Session keys: only `v2_user`, `v2_csrf`,
  `v2_pkce` (`test_v2.py` enforces this).
- **CSRF:** every `/v2` POST needs `csrf_token` (checked in `web.py` `before_request`,
  400 otherwise). New POST forms must include `_csrf(csrf)`.
- **Delete my account** (`/v2/account`, type DELETE) removes every row of the user's,
  then the Supabase auth user via the admin API. If that last call fails the page says
  so: our data is gone, the sign-in record is not.
- **Database:** SQLAlchemy Core, one code path for Postgres (production, Supabase) and
  SQLite (tests, local fallback). Tables are created on first use; on Postgres the app
  also enables RLS on every table every time, so a table it creates is never readable
  through Supabase's API. The old password-era SQLite file is refused with a clear
  message (copy it with `v2.migrate_sqlite`, then rename it).
- **Add show** takes only the iTunes id and re-looks up the feed URL from Apple, so a
  form can't make the server fetch an arbitrary URL. Shows with no public feed are
  shown as "No public feed, can not be analyzed".
- **Backlog:** last `RECENT_DAYS` (30) of saved feeds minus heard; undated episodes
  are left out. Feeds cached 1 h, Apple episode lists 6 h, both in SQLite.

## Evaluation (Phase 0)

V2 is set selection under a time budget. A new pick method must beat the best earlier
one by a meaningful margin on labeled backlogs or it is cut; simpler wins ties. If
nothing clearly beats `newest_first`, say so.

- Label backlogs at `/label`; files land in `data/labels/`. `python -m v2.report`
  compares methods with 95% bootstrap CIs over backlogs.
- Under 5 backlogs the report never calls a difference, whatever the intervals say.
- `v1_llm` needs `GROQ_API_KEY` (or Claude); without it the row says
  `SKIPPED, no API key`; a call error shows `FAILED`. Its over-budget picks are dropped
  by the hard cap and counted.
- `groups_touched` sits beside `duplicate_catch_rate`: picking nothing from a group
  counts as "caught", so read the two together.
- `/label` is locked to this machine (127.0.0.1 / ::1 with no proxy headers) or
  `?key=ADMIN_KEY`. cloudflared connects from 127.0.0.1, so loopback alone is NOT
  enough — the forwarding-header check keeps tunnel visitors out.

## Supabase setup (once)

1. Supabase dashboard → SQL Editor → run `v2/supabase_setup.sql` (tables + RLS).
2. Authentication → Providers → enable **Google** with a Google OAuth client
   (Google Cloud console; its redirect URI is the one Supabase shows on that page).
3. Authentication → URL Configuration → add every callback to **Redirect URLs**:
   `http://127.0.0.1:3000/v2/auth/callback` and any tunnel/hosted
   `https://<host>/v2/auth/callback`.
4. Put `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` and
   `DATABASE_URL` in `.env`.
5. To keep old local data: sign in with Google once, then
   `python -m v2.migrate_sqlite --dry-run`, then without `--dry-run`, then rename
   `data/podsen_v2.db` (e.g. to `podsen_v2.old.db`).

## Hosting

Run locally and share with `cloudflared tunnel --url http://127.0.0.1:3000` (add the
tunnel's callback URL to Supabase's Redirect URLs). Set `SESSION_SECRET` and
`ADMIN_KEY` first. `Dockerfile` / `fly.toml` / `render.yaml` are
kept but untested since the V2 move; with `DATABASE_URL` on Postgres there is no local
state left besides `/label` drafts and label files under `DATA_DIR`.
Gunicorn needs `--timeout 300`: a first run fetches every saved feed while the user waits.

## Known weak spots

- `rank.py`'s `claude` backend has never run against the live API, only stubs.
- The Amazon Music search link was only partly verified (the route exists; results
  rendering needs a real browser check).
- Apple's episode lookup returns a show's newest 200 episodes; older picks fall back to
  "Search on Apple Podcasts".
- There is no real labeled data yet. Feedback so far is the builder's own.
