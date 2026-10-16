# Podsen

Podcast triage. Users make an account, add the shows they follow (found through
Apple's free iTunes search), and say how much time they have right now. Podsen reads
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
python -m pytest test_rank.py test_v2.py test_v2_app.py -q   # 176 tests
python -m v2.report                  # compare pick methods on data/labels/
python -m v2.report --dir v2/sample_data   # same, on the bundled FAKE samples
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
| `v2/db.py` | SQLite at `DATA_DIR/podsen_v2.db`: users, shows, heard, runs, feedback, caches |
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

## How the app works

- **Accounts:** email + werkzeug-hashed password. Logins last 30 days (permanent
  session, `PERMANENT_SESSION_LIFETIME`). Session keys: only `v2_user`, `v2_csrf`.
- **CSRF:** every `/v2` POST needs `csrf_token` (checked in `web.py` `before_request`,
  400 otherwise). New POST forms must include `_csrf(csrf)`.
- **Login throttle (15 min):** 5 failures per email *from one IP*, 20 per IP overall.
  Per-IP keying stops one person locking out someone else.
- **Delete my account** (`/v2/account`, password + same throttle) removes the user and
  every row of theirs.
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

## Hosting

Run locally and share with `cloudflared tunnel --url http://127.0.0.1:3000`. Set
`SESSION_SECRET` and `ADMIN_KEY` first. `Dockerfile` / `fly.toml` / `render.yaml` are
kept but untested since the V2 move; all data is one SQLite file, so one instance only.
Gunicorn needs `--timeout 300`: a first run fetches every saved feed while the user waits.

## Known weak spots

- `rank.py`'s `claude` backend has never run against the live API, only stubs.
- The Amazon Music search link was only partly verified (the route exists; results
  rendering needs a real browser check).
- Apple's episode lookup returns a show's newest 200 episodes; older picks fall back to
  "Search on Apple Podcasts".
- There is no real labeled data yet. Feedback so far is the builder's own.
