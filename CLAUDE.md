# Podsen

Triage for a Spotify podcast backlog. Given how much time the user has right now,
surface the 1–2 unheard episodes worth playing today with a one-line reason each,
plus what's safe to skip. Not a discovery tool — only shows they already follow.

Target user: follows 10+ shows, 100+ unheard episodes, refuses to unsubscribe.
Wants help deciding, not permission to prune.

**Stage: prototype for ~5 test users. Validating "are the picks any good", nothing else.**

## Commands

```bash
.venv\Scripts\Activate.ps1
flask --app app run --port 3000       # `flask run` reads .env; `python app.py` does NOT
python -m pytest test_podsen.py -q    # 59 tests
```

There is **no auto-reload** — after editing, kill the process on port 3000 and
restart, or you will test stale code. Verifying a change by curling the running
server without restarting has produced false "the fix didn't work" conclusions.

## Architecture

| File | Role |
|---|---|
| `app.py` | Flask routes, the 6-step flow, `/admin` dump |
| `spotify.py` | OAuth, token refresh, backlog fetch, playlist create |
| `rank.py` | the only LLM call; ollama + claude behind one `rank()` |
| `store.py` | append-only `events.jsonl` |
| `views.py` | HTML string builders |

**The AI boundary is deliberate.** Only relevance judgement and reason-writing go to
the model. Auth, data fetching, playlist creation, and anything numeric stay as plain
code. When something is arithmetic, enforce it in code — see the time-budget note below.

Flow: connect Spotify → time budget → scan backlog → rank → confirm → playlist +
feedback. Nothing is hidden, skipped or deleted without an explicit confirm.

## Gotchas that cost real time — do not rediscover these

**Ollama `/v1` silently ignores `options.num_ctx`.** It caps every request at exactly
4096 tokens total regardless of what you pass, which truncates the ~6.8k backlog
prompt mid-answer and surfaces as an unrelated-looking JSON parse error. Only the
native `/api/chat` endpoint honours it. That is why `rank.py` does not use the
OpenAI-compatible endpoint despite it being more portable.

**`think: false` is 2.8× faster** on gemma4:e4b (16s vs 45s) with no quality loss on
this task, and stops reasoning tokens crowding out the JSON.

**Never ask the model to respect a hard number.** A 15-minute budget once returned a
61-minute episode and a 60-minute budget returned 119 minutes of audio. `fit_to_budget()`
now filters candidates before ranking so the model only sees episodes that fit. If a
future constraint is numeric, filter in code — do not add it to the prompt.

**Spotify does not honour `public: false` on playlist creation.** A playlist created
that way was confirmed world-readable with an app-only token. `create_playlist` forces
it with a follow-up `PUT` and *reads the value back*; the UI warns if it's still public.
Never report privacy you haven't verified.

**Spotify requires the app owner to have Premium** (Feb 2026 change), enforced at
request time on existing apps too, not just at app creation. Dev Mode is capped at
**5 test users**. Without Premium, `GET /me` returns 403 and nothing works.

**Redirect URI must be `127.0.0.1`, not `localhost`** — Spotify rejects the latter.

**Flask's session is request-context-bound and not thread-safe.** The per-show fan-out
refreshes the token once *before* spawning threads and workers never touch the session.
A show that fails anyway is reported via `dropped`, never silently omitted — a partial
triage must never look complete.

**Gunicorn needs `--timeout 300`.** Triage blocks on the ranking call; the 30s default
kills the worker mid-run.

## Constraints

- No client JS, no build step, no templates directory. Server-rendered strings only.
- **No mock data in the product flow.** Test doubles in `test_podsen.py` are fine;
  the app must never show a pick that didn't come from real user data.
- Escape every interpolated value in `views.py` (`esc()`); episode titles are untrusted.
- `data/` and `.env` are gitignored — real listening history must never be committed.

## Hosting reality

Ranking runs on **local Ollama by default**, so the app only works while this machine
is on. `Dockerfile` / `fly.toml` / `render.yaml` are kept but **do not work as written**
— a container can't reach `localhost:11434`. Recommended: run locally, share via
`cloudflared tunnel`. See README for the alternatives.

## Current state / known weak spots

- `RANK_BACKEND` defaults to `ollama` (`gemma4:e4b`). `claude` works but **has never
  been executed against the live API** — no key has ever been available in this project.
  It is verified only against stubs and the SDK signature.
- `gemma4:26b` needs ~20GB; will not load on a 12GB / 6GB-VRAM laptop.
- The local model occasionally emits malformed JSON (unescaped quote in a reason);
  `rank()` retries once with a corrective hint, then fails with a clear message.
- `legacy-js/` is the superseded Node implementation, kept only because the repo began
  without git history. Safe to delete.
- Feedback in `events.jsonl` so far is the builder's own — not yet a validation signal.
