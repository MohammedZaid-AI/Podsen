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

Ranking uses a **local Ollama model by default**. No API key, nothing to pay for —
but it only works while your machine is on, awake, and running Ollama. A hosted
container cannot reach `localhost:11434` on your laptop, so **the Dockerfile /
fly.toml / render.yaml path does not work as-is** (those files are kept, with notes,
for when you want it).

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
| Deploy the app, tunnel back to your Ollama | Same laptop dependency, and your Ollama is exposed — put auth in front | Free, ~15 min |
| Host Ollama on a GPU box, point `OLLAMA_BASE_URL` at it | Always-on, no laptop dependency | ~$0.50–2/hr |
| `RANK_BACKEND=claude` + `ANTHROPIC_API_KEY` | Always-on, no Ollama at all | ~$0.02/run |

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

`RANK_BACKEND` picks one. **Default is `ollama`** — leave it unset and everything
works with no API key.

| | `ollama` (default) | `claude` |
|---|---|---|
| Cost | free | ~$0.02/run |
| Speed | ~18s | ~10–20s |
| Needs | Ollama + `gemma4:e4b` | `ANTHROPIC_API_KEY` |
| Backlog ranked | 40 episodes | 50 episodes |
| Quality | good enough to test the idea; see below | better |

Both share prompt building, JSON repair and parsing — only the transport differs.

### Why the Ollama backend uses `/api/chat`, not `/v1`

Only Ollama's **native** endpoint honours `options.num_ctx`. Measured here: the
OpenAI-compatible `/v1` endpoint silently caps a request at 4096 tokens total no
matter what you pass, which truncated the ~6.8k-token backlog prompt and left the
model cut off before it could emit JSON — surfacing as an unrelated-looking parse
error. Setting `num_ctx` per request removes that whole class of failure, so
**you do not need to set `OLLAMA_CONTEXT_LENGTH` on the server any more**. If your
`OLLAMA_BASE_URL` still ends in `/v1` it's stripped automatically.

### Known corners of the local model

- Reasons are usually specific, but `gemma4:e4b` still occasionally falls back to
  "the 79-minute runtime is too long" despite being told not to — roughly 1 skip in 4,
  down from 4 in 4 before prompt tuning.
- It sometimes truncates a single reason mid-sentence. Cosmetic, doesn't break parsing.
- It occasionally emits malformed JSON (an unescaped quote inside a reason).
  `rank()` retries once with a corrective instruction before failing.
- `gemma4:26b` is a much better model but needs ~20 GB — it will not load on a 12 GB /
  6 GB-VRAM laptop. Set `OLLAMA_MODEL=gemma4:26b` on better hardware.
- Reasoning mode is **off** (`think: false`): measured 2.8× faster (16s vs 45s) with
  no quality loss on this task. `OLLAMA_THINK=true` re-enables it.

## Setup

### 1. Ollama (~5 min)

```
# install from https://ollama.com/download  (or: winget install Ollama.Ollama)
ollama pull gemma4:e4b        # ~9.6 GB
```

That's it — no context-length env var, no config. Podsen checks on its first ranking
call that Ollama is reachable and the model is pulled, and tells you exactly what to
run if not.

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
copy .env.example .env              # fill in the two Spotify values
flask --app app run --port 3000     # `flask run` is what reads .env
```

Open http://127.0.0.1:3000.

### 4. Read the validation signal

`/admin?key=YOUR-ADMIN-KEY` — JSON dump of every event (triage runs, what was
confirmed, feedback answers). Or `cat data/events.jsonl`.

## Tests

```
pip install pytest
python -m pytest test_podsen.py -q
```

Covers unheard detection, model-output parsing and the JSON-repair retry, the
pick/skip overlap guard, backend selection and the no-API-key default path, the
`num_ctx` safeguard and preflight errors, fan-out failure reporting, the JSONL event
format, and HTML escaping. Transport stubs are test doubles — the app itself still
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
