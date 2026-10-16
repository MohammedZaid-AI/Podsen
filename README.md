# Podsen

**Listen to what's worth it.**

Triage for your podcast backlog. Make an account, add the shows you follow, and tell
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
copy .env.example .env              # set SESSION_SECRET and ADMIN_KEY
flask --app app run --port 3000     # `flask run` is what reads .env
```

Open http://127.0.0.1:3000 — it goes straight to the app at `/v2/`. Sign up, add 3 or
more shows, pick a time.

To share with testers, run it on your machine and put a tunnel in front:

```powershell
cloudflared tunnel --url http://127.0.0.1:3000
```

Cloudflared prints an `https://<random>.trycloudflare.com` URL to send out. The link
only works while your machine is on and the app is running.

## How it works

| Step | How |
|---|---|
| Accounts | email + password (hashed with werkzeug), stored in SQLite at `data/podsen_v2.db` |
| Find shows | Apple's free iTunes Search API. Shows with no public feed are marked "No public feed, can not be analyzed" |
| Backlog | episodes from the last 30 days of each saved feed, minus ones you marked heard |
| Picks | `v2/engine.py` `pick()`, currently newest-first, filling the time budget. The budget is a hard cap, enforced in code |
| Play | an in-page audio player from the feed, plus Spotify search, Apple Podcasts (the exact episode when it can be matched, otherwise a search), YouTube search, Amazon Music search |
| Feedback | "Was this worth your time?" yes / somewhat / no, saved with the picks |

A show whose feed fails to load is named on the results page — picks from a partial
backlog never look complete. Logins last 30 days; every form is CSRF-protected; failed
logins are throttled per email and IP. Users can delete their account and all its data
from the Account page. See `/v2/privacy` for what is stored.

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

- `/admin?key=YOUR-ADMIN-KEY` — every run and feedback answer as JSON (no emails or
  password hashes).
- `/label` — see above.

## Tests

```
pip install pytest
python -m pytest test_rank.py test_v2.py test_v2_app.py -q
```

`test_rank.py` covers the LLM ranking layer (budget filter, JSON repair and retry,
Groq and Claude call handling). `test_v2.py` covers the RSS loader, label files,
pick methods, metrics, the report, `/label`, and the data rule. `test_v2_app.py`
covers the app: accounts, CSRF, throttling, shows, picks, links, feedback, deletion
and `/admin`. Apple and every feed are stubbed — no network needed.

## Data rule

Spotify's developer policy forbids feeding Spotify data into AI/ML models, so Podsen
uses only public RSS data and labels people type in. The "Spotify" link on a pick is a
plain search link; Podsen never calls a Spotify API. `test_v2.py` fails if anything
imports a `spotify` module or a Spotify API URL appears anywhere in the repo.

## Deploying

`Dockerfile`, `fly.toml` and `render.yaml` are kept but untested since the move to the
current app. All data is one SQLite file, so run a single instance with a persistent
disk, and give gunicorn `--timeout 300` (a first run fetches every feed while the user
waits).
