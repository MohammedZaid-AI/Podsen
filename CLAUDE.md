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
python -m pytest test_rank.py test_v2.py test_v2_app.py test_engine.py test_production.py test_speed.py -q   # 325 tests, no network
python -m v2.report                  # compare pick methods on data/labels/
python -m v2.report --dir v2/sample_data   # same, on the bundled FAKE samples
python -m v2.migrate_sqlite --dry-run      # copy old local SQLite data into Postgres
```

`/` is the landing page (`landing/`); the app is at `/v2/`. `/label` (hand-label backlogs for evaluation) and
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
| `v2/itunes.py` | Apple iTunes search/lookup (batched), a show's episode list, top chart |
| `v2/trending.py` | trending shows per country (in/us/gb), Apple's chart cached 12 h |
| `v2/rss.py` | RSS → episodes (stdlib). Durations round UP: the budget is a hard cap |
| `v2/backlog.py` | a user's backlog: last 30 days of their feeds, minus heard; feed cache |
| `v2/engine.py` | `pick()` — the ONE place the pick method is chosen (now `newest_first_varied`); unused-time reasons, "a little longer" |
| `v2/links.py` | exact-episode Apple Podcasts links (guid, then exact title) |
| `v2/refresh.py` | stale-while-revalidate: background cache refreshes, one per key |
| `v2/timing.py` | one log line per request: total, db queries, feeds, Apple |
| `v2/baselines.py` | `newest_first`, `random_fill`, `v1_llm` (uses `rank.py`) |
| `v2/dataset.py` | label file format + validation, saved under `DATA_DIR/labels/` |
| `v2/metrics.py`, `v2/report.py` | evaluation metrics, bootstrap CIs, `python -m v2.report` |
| `v2/label_views.py` | HTML for `/label` |
| `v2/sample_data/` | 3 **FAKE** labeled backlogs so the report runs without real labels |
| `static/` | logo (`podsen-logo.png`: transparent mark cut from `zaid pic.jpg`, 3x for 56px), favicon, `apple-touch-icon.png`, 1024px masters in `brand/`, brand icons in `icons/` (Google G; Spotify, Apple Podcasts, YouTube, Amazon from Simple Icons) |
| `landing/`, `sync_landing.py` | copy of `../podsen-landing`, served at `/`; the sync script refreshes it |
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
- **Landing page + Install App (PWA):** `/` serves `landing/`, a copy of the separate project
  `../podsen-landing` (edit there, `python sync_landing.py`, commit `landing/`). It must be on the
  app's address: a browser can only install the site you're on. The app serves `/manifest.json`
  (`app.MANIFEST`: `start_url` `/v2/`, standalone, icons in `static/icon-*.png`; plus
  `related_applications` so Chrome can tell the page it's installed) and `/sw.js`
  (`app.SERVICE_WORKER`): caches NOTHING, only answers a failed GET page load with an offline
  page; forms, images and redirects pass straight through. The landing script registers it and
  runs the button: install prompt on Android/desktop Chrome/Edge, Safari steps on iPhone/iPad,
  "installed" status after; opened from the icon it goes to `/v2/`. The app has no install UI.
- **No client JS in the app except the small progressive enhancement script** (`views.PROGRESSIVE_JS`,
  under 30 lines, no libraries: busy state on submit, no double submits, restored on
  Back), **and every page must work without it.** Forms are plain HTML forms; the
  script never changes what a form sends. No build step, no templates directory;
  server-rendered strings only.
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
  anon key, then lands on My shows (your shows first, newest added on top). The callback URL is built from the request host; Supabase only redirects to
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
- **Trending:** Apple's top-25 chart per country (`?country=in|us|gb`, anything else
  falls back to India), cached 12 h in `trending_cache`. The chart has no feedUrl, so
  one batched lookup marks shows with no public feed (listed, no checkbox). Shown on
  Today while the user has fewer than 3 shows, and on My shows. "Add selected" posts
  only iTunes ids to `/v2/shows/add-many`; at 3+ shows it lands on Today. A failed
  load says "Could not load trending shows right now" and is never cached.
- **One add path:** `_add_shows()` in `web.py` (single Add and Add selected) looks up
  every feed URL from Apple; the form's own values are never stored.
- **Pick rule (`newest_first_varied`):** newest-first fill, at most one episode per
  show; a second from the same show only when nothing from another show fits. It is
  the app's own rule; `baselines.newest_first` stays the plain Phase 0 baseline, and
  runs/feedback record `engine.ENGINE_NAME` so each result stays tied to its rule.
- **Results page:** with more than 5 min unused, a line says why ("27 min left. Nothing
  else fits. Your shortest unheard episode is about 31 min." — or that the backlog is
  used up). "If you have a little longer" lists up to 2 skipped episodes that fit with
  at most 15 extra minutes (never a pick), saved with the run. Durations read
  "about N min" because feeds leave out inserted ads; budget math uses the raw number.
- **Backlog:** last `RECENT_DAYS` (30) of saved feeds minus heard; undated episodes
  are left out. Feeds cached 1 h, Apple episode lists 6 h, trending 12 h, all in the DB.

## Speed

The database is in Supabase **ap-southeast-1 (Singapore)**: from India each round trip
is ~100-125 ms, so speed is round trips. Every request logs one line
(`podsen.timing`): `GET /v2/today 200 194ms db=1q/96ms conn=1/new0 feeds=0/0ms
apple=0/0ms` - path only (never the query string: `?code=`, `?key=`), no emails.

- **One pooled connection per request** (`db._tx()`), returned at teardown. pool_pre_ping
  costs one round trip per checkout, so it now runs once per request, not per query.
- **One query per page:** `db._bundle()` loads the user, their shows and heard ids,
  plus what the page needs next via hints from `login_required` (the run and its
  verdict; the trending chart; for "Find picks", every show's feed and Apple cache).
  Never query per show or per episode in a loop; add to the bundle or prefetch.
- **Stale-while-revalidate:** a cached feed, Apple list or chart is served at once,
  however old, and refreshed in the background (`v2/refresh.py`). Only a cache miss
  makes the user wait. Adding a show warms its caches in the background. Tests run
  refreshes inline (conftest) or record them (`refresh.MODE = "record"`).
- **The feed cache holds only what picking uses:** last 30 + 14 days, no
  descriptions (`backlog.for_cache`). Full feeds were up to 1.5 MB per read.
- **Descriptions live in the `episodes` table** (feed URL + guid, every episode, stored
  once, never rewritten), written only in the background: by `backlog.refresh_feed`,
  or queued after a cache-miss fetch. For later layers (`db.episode_descriptions`);
  the Today / picking path never reads it (`test_speed.py` checks every statement).
- Measured locally against real Supabase, warm: every page ~200 ms, search ~320 ms
  (live Apple), "Find picks" POST ~340 ms. Render should be in Singapore too.

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

## Hosting (Render + Supabase)

`render.yaml` = one Docker web service from `Dockerfile` (gunicorn, 2 workers,
`--timeout 300`, non-root, no access log so `?key=` never reaches logs), health check
`/health` (touches nothing). No disk: data is in Supabase. README "Deploy on Render"
lists every env var.

- **Startup checks:** with `FLASK_ENV=production`, `app.production_problems()` makes
  the app refuse to start if `SESSION_SECRET` is missing/placeholder/<32 chars, or
  `DATABASE_URL` (Postgres) or any `SUPABASE_*` value is missing. Names only, never
  values. A new required setting goes there AND in `render.yaml`
  (`test_production.py` checks every env var the code reads is listed).
- **Cookies** are `Secure` (HTTPS only) in production; `HttpOnly`, `SameSite=Lax` always.
- **`DATABASE_URL` on Render must be the Session pooler** (`*.pooler.supabase.com:5432`);
  the direct `db.<ref>.supabase.co` host is IPv6-only. Pool: 2 + 3 overflow per worker.
- **ProxyFix** trusts one hop, so `request.url_root` is the public https URL and the
  Google callback is right behind Render or a cloudflared tunnel.
- **Placeholders** from `.env.example` count as unset (`app.PLACEHOLDERS`): an
  `ADMIN_KEY` left at the example value disables `/admin` rather than exposing it.

**Mobile:** most users are on phones. Every page was measured at 375px in headless
Chromium: no sideways scroll, tap targets >= 44px (`min-height: 44px`), artwork 48px,
the audio player fits. Keep secondary text under a name, not beside it — a side-by-side
note squeezes a show name into a few letters per line.

## Known weak spots

- `rank.py`'s `claude` backend has never run against the live API, only stubs.
- The Amazon Music search link was only partly verified (the route exists; results
  rendering needs a real browser check).
- Apple's episode lookup returns a show's newest 200 episodes; older picks fall back to
  "Search on Apple Podcasts".
- There is no real labeled data yet. Feedback so far is the builder's own.
