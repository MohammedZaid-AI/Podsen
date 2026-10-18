# Podsen

**Listen to what's worth it.**

Triage for your podcast backlog. Sign in with Google, add the shows you follow, and tell
Podsen how much time you have right now. It reads each show's public feed and picks
the recent episodes that fit that time, lists what's safe to skip, and gives you a
way to play each pick: right in the page, or via Spotify, Apple Podcasts, YouTube or
Amazon Music.

Not a discovery tool — only the shows you chose. Works with any show that has a
public RSS feed; no streaming account is needed.

## Run it

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env              # fill in the values (see Supabase setup below)
flask --app app run --port 3000     # `flask run` is what reads .env
```

Open http://127.0.0.1:3000 — it goes straight to the app at `/v2/`. Continue with
Google, add 3 or more shows, pick a time.

## Supabase setup (once)

Podsen uses [Supabase](https://supabase.com) for Google sign-in and its Postgres
database.

1. **Create the tables.** Dashboard → SQL Editor → paste `v2/supabase_setup.sql` →
   Run. It also turns on Row Level Security for every table, with no policies, so the
   public anon key can't read anything through Supabase's API. Our server connects to
   the database directly, so it isn't affected.
2. **Turn on Google.** Authentication → Providers → Google: paste a Google OAuth client
   id and secret (Google Cloud console → APIs & Services → Credentials). Give that
   Google client the redirect URI Supabase shows on the same page.
3. **Allow our callback.** Authentication → URL Configuration → Redirect URLs: add
   `http://127.0.0.1:3000/v2/auth/callback`, plus `https://<your-tunnel-or-host>/v2/auth/callback`
   for anywhere else you run it.
4. **Fill in `.env`:** `SUPABASE_URL` and `SUPABASE_ANON_KEY` (Project Settings → API),
   `SUPABASE_SERVICE_ROLE_KEY` (same page; secret, server only) and `DATABASE_URL`
   (Connect → connection string, with your database password). Without `DATABASE_URL`
   the app uses a local SQLite file instead.
5. **Bringing old local data across** (from the email+password version): sign in with
   Google once, then run `python -m v2.migrate_sqlite --dry-run` to see what would be
   copied, then `python -m v2.migrate_sqlite`. Accounts are matched by email. Afterwards
   rename `data/podsen_v2.db` (for example to `podsen_v2.old.db`).

To share with testers, run it on your machine and put a tunnel in front:

```powershell
cloudflared tunnel --url http://127.0.0.1:3000
```

Cloudflared prints an `https://<random>.trycloudflare.com` URL to send out. Add
`https://<that-host>/v2/auth/callback` to Supabase's Redirect URLs (step 3 above) or
Google sign-in will refuse it. The link only works while your machine is on and the
app is running.

## How it works

| Step | How |
|---|---|
| Accounts | "Continue with Google" through Supabase Auth (PKCE, server side, no client JS); we get your email |
| Storage | Supabase Postgres (`DATABASE_URL`), or a local SQLite file when that isn't set |
| Find shows | Apple's free iTunes Search API, plus a "Trending" list (Apple's top-25 chart for India, US or UK) where you tick several shows and add them in one step. Shows with no public feed are marked "No public feed, can not be analyzed" |
| Backlog | episodes from the last 30 days of each saved feed, minus ones you marked heard |
| Picks | `v2/engine.py` `pick()`, currently newest-first, filling the time budget. The budget is a hard cap, enforced in code |
| Play | an in-page audio player from the feed, plus Spotify search, Apple Podcasts (the exact episode when it can be matched, otherwise a search), YouTube search, Amazon Music search |
| Feedback | "Was this worth your time?" yes / somewhat / no, saved with the picks |

A show whose feed fails to load is named on the results page — picks from a partial
backlog never look complete. Logins last 30 days; every form is CSRF-protected.
Users can delete their account and all its data from the Account page by typing
DELETE; that also removes their sign-in record at Supabase. See `/v2/privacy` for
what is stored.

Python + **Flask**, server-rendered HTML, no build step and no client JS.

## Evaluating pick methods

Podsen is still validating whether its picks are any good. A new pick method only
replaces newest-first if it clearly beats it on hand-labeled backlogs.

1. Open http://127.0.0.1:3000/label (this machine only, or `?key=ADMIN_KEY`).
2. Paste RSS feed URLs, a time budget and what you want from the slot; label each
   episode *want* / *don't want* / *would regret skipping*.
3. Run `python -m v2.report` for a table of every method with 95% confidence intervals.

`python -m v2.report --dir v2/sample_data` runs it on three FAKE sample backlogs.
The `v1_llm` method ranks with an LLM through `rank.py` and needs `GROQ_API_KEY`
(or `RANK_BACKEND=claude` + `ANTHROPIC_API_KEY`); without a key its row is skipped.

## Operator pages

- `/admin?key=YOUR-ADMIN-KEY` — every run and feedback answer as JSON (user ids
  only, no emails).
- `/label` — see above.

## Tests

```
pip install pytest
python -m pytest test_rank.py test_v2.py test_v2_app.py test_engine.py test_production.py test_speed.py -q
```

`test_rank.py` covers the LLM ranking layer (budget filter, JSON repair and retry,
Groq and Claude call handling). `test_v2.py` covers the RSS loader, label files,
pick methods, metrics, the report, `/label`, and the data rule. `test_v2_app.py`
covers the app: Google sign-in (PKCE, error cases), CSRF, shows, picks, links,
feedback, deletion, the database layer, the data copy script and `/admin`. Supabase,
Apple and every feed are stubbed, and `conftest.py` makes sure no test can reach the
real Supabase project or load the real `.env` — no network needed.

## Data rule

Spotify's developer policy forbids feeding Spotify data into AI/ML models, so Podsen
uses only public RSS data and labels people type in. The "Spotify" link on a pick is a
plain search link; Podsen never calls a Spotify API. `test_v2.py` fails if anything
imports a `spotify` module or a Spotify API URL appears anywhere in the repo.

## Deploy on Render

`render.yaml` is a Render Blueprint for one Docker web service built from the
`Dockerfile`: gunicorn, 2 workers, 300-second timeout, health check on `/health`
(which never touches the database). There is no disk: all data is in Supabase.

### Environment variables

| Name | Where it comes from | Notes |
|---|---|---|
| `FLASK_ENV` | set to `production` by `render.yaml` | turns on the startup checks and HTTPS-only cookies |
| `SESSION_SECRET` | generated by Render | signs the login cookie; at least 32 characters |
| `ADMIN_KEY` | generated by Render | unlocks `/admin?key=` and `/label?key=` |
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL | |
| `SUPABASE_ANON_KEY` | Supabase → Project Settings → API → `anon` `public` | |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API → `service_role` | secret; server only |
| `DATABASE_URL` | Supabase → **Connect** → **Session pooler** URI, with your DB password | see below |
| `DATA_DIR` | set to `/tmp/podsen-data` by `render.yaml` | only `/label` drafts are written here |
| `GROQ_API_KEY` | optional, console.groq.com | only for the `v1_llm` evaluation baseline |

In production the app **refuses to start** if `SESSION_SECRET` is missing, still a
placeholder or shorter than 32 characters, or if `DATABASE_URL` or any of the three
Supabase values is missing. The deploy log lists what's wrong by name, never by value.

**`DATABASE_URL` must be the Session pooler string**, which looks like
`postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
The direct host (`db.<project-ref>.supabase.co`) does **not** work from Render: it is
IPv6-only and Render's outbound network is IPv4. A password with `@`, `#`, `/` or `:`
in it is fine; the app encodes it.

Behind Render's proxy, `ProxyFix` (one trusted hop) turns `X-Forwarded-Proto` and the
`Host` header into `https://<your-host>/`, so the Google callback is
`https://<your-host>/v2/auth/callback`. Add exactly that URL to Supabase's Redirect URLs.

`fly.toml` is kept but untested.
