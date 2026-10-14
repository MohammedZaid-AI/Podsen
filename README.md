# Podsen

**Listen to what's worth it.**

Triage for your Spotify podcast backlog. Connects to your account, finds unheard
episodes across shows you already follow, and — given how much time you have right
now — surfaces the 1–2 worth playing today with a one-line reason each, plus what's
safe to skip. Nothing is deleted or skipped without you confirming. On confirm it
can drop the picks into a private Spotify playlist.

Not a discovery tool. Metadata-only ranking (title + description + show + duration),
no transcription.

## ⚠️ This runs on your machine

Ranking calls the **hosted Groq API by default** (`GROQ_API_KEY` required). The app
itself still runs on your machine, so it only works while your machine is on. The
Dockerfile / fly.toml / render.yaml files are kept, but they predate the switch to
Groq and **have not been re-tested since** — treat them as unverified.

**Recommended setup for testers: run locally, share via a tunnel.**

```powershell
# terminal 1 — the app
flask --app app run --port 3000

# terminal 2 — a public https URL pointing at it
cloudflared tunnel --url http://127.0.0.1:3000
```

Cloudflared prints an `https://<random>.trycloudflare.com` URL. Put that in `BASE_URL`,
add `<that-url>/callback` to your Spotify app's redirect URIs, and send it to testers.
Two minutes, no accounts, no cost. Tell testers the link is only live when you say it
is — closing your laptop kills every in-flight session.

Other options, in increasing order of effort, if the laptop dependency stops being
acceptable:

| Option | Testers get | Cost |
|---|---|---|
| **Local + cloudflared tunnel** (recommended) | Works while your machine is on | Free, 2 min |
| Deploy the container (Fly / Render) | Always-on — once the deploy files are re-verified | host's pricing |
| `RANK_BACKEND=claude` + `ANTHROPIC_API_KEY` | Same as above, Claude instead of Groq | ~$0.02/run |

## Stack

Python + **Flask**, server-rendered HTML, no build step and no client JS. Spotify Web
API (Authorization Code OAuth) for auth/data/playlist — plain code, no AI. One LLM
call for ranking + reasons, behind a swappable backend. State is an append-only JSONL
event log you can `cat`.

Flask because `flask.session` is a signed cookie — a direct match for how tokens are
carried — and because form-driven server-rendered pages are exactly what it's for.

| File | Role |
|---|---|
| `app.py` | Flask routes, the 6-step flow, `/admin` dump |
| `spotify.py` | OAuth, token refresh, backlog fetch, playlist create — plain code |
| `rank.py` | the one LLM call; both backends behind a single `rank()` |
| `store.py` | append-only `events.jsonl` |
| `views.py` | HTML builders |
| `test_podsen.py` | pytest suite |

## Ranking backends

`RANK_BACKEND` picks one. **Default is `groq`** — leave it unset and set `GROQ_API_KEY`.

| | `groq` (default) | `claude` |
|---|---|---|
| Needs | `GROQ_API_KEY` | `ANTHROPIC_API_KEY` |
| Model | `GROQ_MODEL`, default `llama-3.3-70b-versatile` | `ANTHROPIC_MODEL` |
| Backlog ranked | 40 episodes | 50 episodes |

Both share prompt building, JSON repair and parsing — only the transport differs.
Models occasionally emit malformed JSON (an unescaped quote inside a reason);
`rank()` retries once with a corrective instruction before failing. The `claude`
backend has only been verified against stubs, never the live API.

The project used a local Ollama backend until commit `918b147`; its notes are in git
history if a local backend is ever wanted again.

## Setup

### 1. Groq key (~2 min)

Create a key at https://console.groq.com/keys and put it in `.env` as `GROQ_API_KEY`.

### 2. Spotify app (~3 min)

1. https://developer.spotify.com/dashboard → **Create app**.
2. Redirect URI: add `http://127.0.0.1:3000/callback` (and your tunnel URL + `/callback`).
3. APIs used: **Web API**.
4. Copy the **Client ID** and **Client secret** into `.env`.
5. The app starts in **Development Mode** — under **User Management**, add the
   Spotify account email of every tester (you first). Max 25. No review needed.

### 3. Run it

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env              # fill in the Spotify values and GROQ_API_KEY
flask --app app run --port 3000     # `flask run` is what reads .env
```

Open http://127.0.0.1:3000.

### 4. Read the validation signal

`/admin?key=YOUR-ADMIN-KEY` — JSON dump of every event (triage runs, what was
confirmed, feedback answers). Or `cat data/events.jsonl`.

## Tests

```
pip install pytest
python -m pytest test_podsen.py test_v2.py -q
```

`test_podsen.py` (V1) covers unheard detection, model-output parsing and the
JSON-repair retry, the pick/skip overlap guard, backend selection, Groq request and
error handling, fan-out failure reporting, the JSONL event format, and HTML escaping.
`test_v2.py` covers the V2 evaluation setup (see CLAUDE.md). Transport stubs are test doubles — the app itself still
shows nothing that didn't come from real Spotify data.

## Known corners (fine for a prototype)

- Heard/unheard = Spotify `resume_point` + last 50 recently-played. If Spotify
  returns no `resume_point` (scope/market quirk) an episode counts as unheard.
- Scans newest 20 episodes of up to 50 followed shows; ranks the 40 most recent unheard.
- A show that fails mid-fan-out (rate limit, region lock, expired token) is reported
  on the results page, not silently dropped — a partial triage never looks complete.
- Triage is one blocking ~18s request with no progress UI.
- Session (incl. Spotify tokens) lives in a signed cookie.

## legacy-js/

The original Node implementation, kept because this repo started without git history.
Delete it once you're happy: `git rm -r legacy-js`.
