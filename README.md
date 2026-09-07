# Podsen

**Listen to what's worth it.**

Triage for your Spotify podcast backlog. Connects to your account, finds unheard
episodes across shows you already follow, and — given how much time you have right
now — surfaces the 1–2 worth playing today with a one-line reason each, plus what's
safe to skip. Nothing is deleted or skipped without you confirming. On confirm it
can drop the picks into a private Spotify playlist.

Not a discovery tool. Metadata-only ranking (title + description + show + duration),
no transcription.

## Stack

Python + **Flask**, server-rendered HTML, no build step and no client JS. Spotify Web
API (Authorization Code OAuth) for auth/data/playlist — plain code, no AI. One LLM
call for ranking + reasons, behind a swappable backend. State is an append-only JSONL
event log you can `cat`.

Flask because `flask.session` is a signed cookie — a direct match for how the Node
version kept Spotify tokens — and because form-driven, server-rendered pages with no
client JS are exactly what it's for. FastAPI would have added async plumbing and a
separate session middleware for no gain in an app whose slowest step is one blocking
API call.

| File | Role |
|---|---|
| `app.py` | Flask routes, the 6-step flow, `/admin` dump |
| `spotify.py` | OAuth, token refresh, backlog fetch, playlist create — plain code |
| `rank.py` | the one LLM call; both backends behind a single `rank()` |
| `store.py` | append-only `events.jsonl` |
| `views.py` | HTML builders |
| `test_podsen.py` | pytest suite |

## Ranking backends

`RANK_BACKEND` picks one. **Default is `claude`.**

| | `claude` (default) | `ollama` |
|---|---|---|
| Cost | ~$0.02/run | free |
| Quality | good | weaker — see caveats |
| Speed | ~10–20s | 45s–5min on a laptop |
| Needs | `ANTHROPIC_API_KEY` | a reachable Ollama |

Both go through the same prompt-building and response-parsing code; only the
transport differs. Switching is a config flip, not a rewrite.

### Ollama caveats (learned the hard way — don't rediscover these)

1. **Context length must be raised to 16384+ or every run fails.** Ollama defaults to
   a 4096-token context regardless of what the model supports, one backlog prompt is
   ~3.8k tokens, and the `/v1` endpoint **silently ignores** a per-request `num_ctx`.
   Set it on the server: `setx OLLAMA_CONTEXT_LENGTH 16384` (Windows, then restart
   Ollama from the tray) or `OLLAMA_CONTEXT_LENGTH=16384 ollama serve`. `rank.py`
   raises a message naming this when it happens, rather than a cryptic JSON error.
2. **Hosting.** `localhost:11434` means the machine *running Podsen*. On Fly/Render
   that's the container, which has no Ollama — so a deployed Podsen on this backend
   only works if you point `OLLAMA_BASE_URL` at a reachable Ollama (a tunnel to your
   machine, or a hosted GPU box). With a tunnel, testers can only use the app while
   your machine is on, awake, and serving.
3. **Quality gap, measured.** On real episode data, `gemma4:e4b` clustered both picks
   from a single show and wrote "it is N minutes long" as the skip reason 4 times out
   of 4. A prompt fix (now shared by both backends) cut that to 2 in 5 and produced
   genuinely differentiated reasons for the rest — but it still ignores that explicit
   instruction ~40% of the time. Bigger local models should narrow this; untested.
4. `gemma4:26b` is an 18.6 GB download with a 17 GB model layer — it needs ~20 GB of
   VRAM or RAM. It will not run on a 12 GB / 6 GB-VRAM laptop.
5. **Occasional malformed JSON.** In live runs `gemma4:e4b` sometimes emitted an
   unescaped `"` inside a reason string, which no parser can recover. `extract_json`
   handles prose and stray braces around the answer, but not a broken string literal —
   that run fails with a 500. Claude does not do this in practice. If you make ollama
   the primary backend, add one retry around `rank()`.

Because the ollama backend must send a smaller prompt, it ranks the **30** most recent
unheard episodes at 350-char descriptions; claude ranks **50** at 600. See
`LIMITS` in `rank.py`.

## What you have to do (I can't — needs your accounts)

### 1. Spotify app (~3 min)

1. https://developer.spotify.com/dashboard → **Create app**.
2. Redirect URI: add `https://YOUR-DEPLOYED-URL/callback` (and `http://127.0.0.1:3000/callback` for local).
3. APIs used: **Web API**.
4. Copy the **Client ID** and **Client secret**.
5. The app starts in **Development Mode** — under **User Management**, add the
   Spotify account email of every tester (you first). Max 25. No review needed.

### 2. Anthropic API key

https://console.anthropic.com → API keys. Set `ANTHROPIC_API_KEY`.

(Only if you switch to `RANK_BACKEND=ollama` do you instead need Ollama installed,
`ollama pull <model>`, and the context-length fix above. No API key in that case.)

### 3. Deploy

**Fly.io** (free persistent volume for the feedback log):

```
fly launch --no-deploy          # accept the existing fly.toml; creates the app
fly volumes create podsen_data --size 1
fly secrets set \
  SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... ANTHROPIC_API_KEY=... \
  SESSION_SECRET=$(openssl rand -hex 32) ADMIN_KEY=$(openssl rand -hex 16) \
  BASE_URL=https://YOUR-APP.fly.dev
fly deploy
```

**Render**: push this repo to GitHub → New → Blueprint → pick the repo (`render.yaml`
is read automatically) → fill in `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
`ANTHROPIC_API_KEY`, `BASE_URL`. Free plan works but the feedback log resets on each
deploy; Starter keeps the disk.

### 4. Point Spotify at the real URL

Add `https://YOUR-DEPLOYED-URL/callback` to the app's redirect URIs. Send testers
the base URL.

### 5. Read the validation signal

`https://YOUR-DEPLOYED-URL/admin?key=YOUR-ADMIN-KEY` — JSON dump of every event
(triage runs, what was confirmed, feedback answers). Or on the box:
`cat $DATA_DIR/events.jsonl`.

## Local dev

```
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate  elsewhere
pip install -r requirements.txt
cp .env.example .env              # fill in Spotify creds + ANTHROPIC_API_KEY
flask --app app run --port 3000   # loads .env via python-dotenv
# open http://127.0.0.1:3000
```

`python app.py` also works but does **not** read `.env` — export the vars yourself.

## Tests

```
pip install pytest
python -m pytest test_podsen.py -q
```

Covers unheard-detection, model-output parsing, the pick/skip overlap guard, backend
selection, both backends end-to-end against stubbed transports, the Ollama
context-truncation error, and the JSONL event format. Transport stubs are test
doubles — the app itself still shows nothing that didn't come from real Spotify data.

## Known corners (fine for a prototype)

- Heard/unheard = Spotify `resume_point` + last 50 recently-played. If Spotify
  returns no `resume_point` (scope/market quirk) an episode counts as unheard.
- Scans newest 20 episodes of up to 50 followed shows.
- Triage is one blocking request with no progress UI. Gunicorn is configured with
  `--timeout 300` so the worker isn't killed mid-run.
- The parallel per-show episode fetch refreshes the Spotify token *before* fanning
  out and doesn't refresh inside worker threads; a show whose request 401s is dropped
  from that run rather than retried.
- Session (incl. Spotify tokens) lives in a signed cookie, so multiple gunicorn
  workers are fine. Event-log appends are single sub-4KB `O_APPEND` writes.

## legacy-js/

The original Node implementation, kept only because this project has no git history
and the port would otherwise be irreversible. Delete it once you're happy:
`rm -rf legacy-js`.
