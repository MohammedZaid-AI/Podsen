"""The only AI in the app: score fit against the time budget + write the reasons.

Two interchangeable backends behind one `rank()`, selected by RANK_BACKEND:
  ollama  (default) -- local model, free, no API key
  claude            -- Anthropic API, better output, ~$0.02/run, needs ANTHROPIC_API_KEY

Prompt building, JSON repair and response parsing are shared, so the backends differ
only in transport.

Why the Ollama backend talks to native /api/chat and not the OpenAI-compatible /v1:
only the native endpoint honours `options.num_ctx`. Measured on this codebase, /v1
silently caps a request at 4096 tokens total no matter what you pass, which truncates
a ~3.8k-token backlog prompt before the model can emit its JSON. Setting num_ctx
per request removes a whole class of failure without anyone having to remember a
machine-wide OLLAMA_CONTEXT_LENGTH.
"""

import json
import os

import requests

# --- backend selection -------------------------------------------------------
DEFAULT_BACKEND = "ollama"


def _backend_name() -> str:
    name = (os.environ.get("RANK_BACKEND") or DEFAULT_BACKEND).strip().lower()
    if name not in _BACKENDS:
        raise RuntimeError(
            f"RANK_BACKEND={name!r} is not supported; use one of {sorted(_BACKENDS)}"
        )
    return name


# How much backlog each backend can chew on. See the note by OLLAMA_NUM_CTX: with
# num_ctx set per request, ollama's ceiling is now RAM, not Ollama's 4096 default.
LIMITS = {"claude": (50, 600), "ollama": (40, 450)}

# A 62-minute episode is a fine answer to "I have an hour"; a 95-minute one is not.
BUDGET_TOLERANCE = 1.10

# --- Anthropic (kept working, but off the default path) ----------------------
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
_client = None


def _anthropic():
    global _client
    if _client is None:
        from anthropic import Anthropic  # imported lazily: no key needed on the ollama path

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError(
                "RANK_BACKEND=claude needs ANTHROPIC_API_KEY. Unset RANK_BACKEND to use "
                "the local ollama backend instead, which needs no key."
            )
        _client = Anthropic()
    return _client


# --- Ollama ------------------------------------------------------------------
def _normalize_base(url: str) -> str:
    """Accept either the native root or a leftover OpenAI-compat /v1 URL."""
    url = url.rstrip("/")
    if url.endswith("/v1"):  # old configs pointed here; native is what honours num_ctx
        url = url[: -len("/v1")]
    return url


OLLAMA_BASE_URL = _normalize_base(os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
# Measured: a full 40-episode backlog prompt is ~6.8k tokens, and the answer ~0.3k.
# 8192 left only 14% headroom; 12288 lands at ~57% used with no measured slowdown.
# Raising it further costs KV-cache RAM, the binding constraint on a laptop.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", 12288))
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT_MS", 300_000)) / 1000
# Reasoning is off by default: measured 2.8x faster (16s vs 45s) on gemma4:e4b with no
# quality loss on this task, and it stops reasoning tokens from crowding out the JSON.
OLLAMA_THINK = (os.environ.get("OLLAMA_THINK") or "").strip().lower() in {"1", "true", "yes"}

_preflight_done = False


SYSTEM = """You are Podsen, a podcast triage assistant. Tagline: "Listen to what's worth it."
The user already follows every show below and has a backlog of unheard episodes. Given how much time they have RIGHT NOW, pick the 1-2 episodes genuinely worth hearing today and list 3-6 that are safe to skip.
Judge on: topical substance, timeliness (news/interview-of-the-moment decays fast; evergreen deep-dives don't), and whether it earns the slot. Every episode listed ALREADY fits the time budget, so never judge on length and never mention running time.
This is triage of shows they chose to follow - not discovery, and never suggest unfollowing.
Prefer picks from different shows: two episodes of the same podcast is a worse listening hour than two good episodes from different ones, unless one is clearly outstanding.
Every reason is ONE sentence that names something concrete from that episode's own title or description - a person, a claim, an event, a question it takes up. A reason that would still make sense pasted under a different episode is wrong. Never write filler like "sounds interesting", "a good listen", or "worth your time".

A pick's reason must say why it earns the listening time they have - what they get out of it - not merely restate the blurb.

A skip's reason must say why it KEEPS, and each one must give a different kind of reason. Draw on: it is evergreen and will be just as good next month; it is a re-run or "Best Of"; its news hook has already been overtaken; it is a niche or narrow-interest instalment of a show they otherwise like; the same ground is covered by a pick. Running time is NEVER a reason - every episode listed already fits, so "it is N minutes long" is always wrong."""

REPAIR_HINT = (
    "\n\nYour previous reply could not be parsed as JSON ({err}). "
    "Reply with ONLY the JSON object and nothing else. Keep every reason on one line "
    "and use single quotes inside reason text - never an unescaped double quote."
)

_DECODER = json.JSONDecoder()


def extract_json(text: str) -> dict:
    """Find the first well-formed JSON object holding picks/skips.

    Models wrap the answer in prose, and reasoning models put braces in that prose,
    so the obvious first-'{' .. last-'}' slice regularly grabs something unparseable.
    Scanning candidate '{' offsets with raw_decode is stdlib and immune to both.
    """
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = _DECODER.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("picks" in obj or "skips" in obj):
            return obj
    raise ValueError("no JSON object with picks/skips in model output")


def fit_to_budget(episodes: list[dict], minutes: int) -> tuple[list[dict], int | None]:
    """Keep only episodes that actually fit the time budget.

    Asking an LLM to respect a hard numeric constraint does not work: a local model
    handed a 15-minute budget cheerfully picked a 61-minute episode. Duration is
    arithmetic, so it is enforced here and the model only ever chooses among
    episodes that already fit.

    Returns (fitting, shortest_minutes_if_nothing_fits).
    """
    ceiling = minutes * BUDGET_TOLERANCE
    fitting = [e for e in episodes if 0 < (e.get("duration_min") or 0) <= ceiling]
    if fitting:
        return fitting, None
    durations = [e["duration_min"] for e in episodes if (e.get("duration_min") or 0) > 0]
    return [], (min(durations) if durations else None)


def build_prompt(episodes: list[dict], minutes: int, max_episodes: int, desc_chars: int):
    candidates = episodes[:max_episodes]
    # Models copy a short integer far more reliably than a 22-char Spotify id.
    listing = [
        {
            "n": i + 1,
            "show": e.get("show"),
            "title": e.get("title"),
            "minutes": e.get("duration_min"),
            "released": e.get("released"),
            "description": (e.get("description") or "")[:desc_chars],
        }
        for i, e in enumerate(candidates)
    ]
    user = (
        f"Time available: {minutes} minutes.\n\n"
        f"Backlog ({len(listing)} unheard episodes):\n"
        f"{json.dumps(listing, ensure_ascii=False)}\n\n"
        'Respond with ONLY this JSON, using the "n" numbers from the list:\n'
        '{"picks":[{"n":0,"reason":""}],"skips":[{"n":0,"reason":""}]}\n'
        "1-2 picks, 3-6 skips. A number must not appear in both lists."
    )
    return candidates, user


def _call_claude(user: str) -> str:
    res = _anthropic().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in res.content if getattr(b, "type", None) == "text")


def _preflight() -> None:
    """Fail with something actionable if Ollama isn't running or the model isn't pulled.

    Cheaper than letting the real failure surface as a connection error mid-triage.
    """
    global _preflight_done
    if _preflight_done:
        return
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        r.raise_for_status()
        tags = r.json()
    except Exception as e:
        raise RuntimeError(
            f"Can't reach Ollama at {OLLAMA_BASE_URL} ({type(e).__name__}). "
            "Start it (`ollama serve`, or launch the Ollama app) — Podsen's ranking "
            "runs locally by default."
        ) from e

    names = {m.get("name", "") for m in tags.get("models", [])}
    if OLLAMA_MODEL not in names and f"{OLLAMA_MODEL}:latest" not in names:
        raise RuntimeError(
            f"Ollama is running but the model {OLLAMA_MODEL!r} isn't pulled. "
            f"Run: ollama pull {OLLAMA_MODEL}"
            + (f"  (available: {', '.join(sorted(names))})" if names else "")
        )
    _preflight_done = True


def _call_ollama(user: str) -> str:
    _preflight()
    body = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "think": OLLAMA_THINK,
        # num_ctx per request is the whole reason this uses native /api/chat.
        "options": {"num_ctx": OLLAMA_NUM_CTX, "temperature": 0.3},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    r = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=body, timeout=OLLAMA_TIMEOUT_S)
    if not r.ok:
        raise RuntimeError(f"ollama {r.status_code} at {OLLAMA_BASE_URL}: {r.text[:300]}")

    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"ollama error: {str(j['error'])[:300]}")

    # Backstop: num_ctx should prevent this, but a much larger backlog could still
    # overflow, and the failure must name the knob rather than surface as bad JSON.
    if j.get("done_reason") == "length":
        used = (j.get("prompt_eval_count") or 0) + (j.get("eval_count") or 0)
        raise RuntimeError(
            f"{OLLAMA_MODEL} ran out of context (used ~{used} of {OLLAMA_NUM_CTX} tokens) "
            "before finishing the JSON. Raise OLLAMA_NUM_CTX (costs RAM), or lower "
            "LIMITS['ollama'] in rank.py."
        )

    msg = j.get("message") or {}
    # With think=False `content` carries the answer; with thinking on, an early stop
    # can leave content empty and the JSON sitting in `thinking`.
    return (msg.get("content") or "").strip() or (msg.get("thinking") or "").strip()


_BACKENDS = {"claude": _call_claude, "ollama": _call_ollama}


def parse(text: str, candidates: list[dict]) -> dict:
    out = extract_json(text)

    def hydrate(arr):
        result = []
        for x in arr or []:
            try:
                idx = int(x.get("n", 0)) - 1
            except (TypeError, ValueError):
                continue
            reason = (x.get("reason") or "").strip()
            if 0 <= idx < len(candidates) and reason:
                result.append({**candidates[idx], "reason": reason})
        return result

    picks = hydrate(out.get("picks"))[:2]
    picked_ids = {p["id"] for p in picks}
    skips = [s for s in hydrate(out.get("skips")) if s["id"] not in picked_ids][:6]
    return {"picks": picks, "skips": skips}


def rank(episodes: list[dict], minutes: int) -> dict:
    backend = _backend_name()
    call = _BACKENDS[backend]
    max_episodes, desc_chars = LIMITS[backend]

    fitting, shortest = fit_to_budget(episodes, minutes)
    if not fitting:
        # Better to say so than to hand back something twice the length they asked for.
        return {"picks": [], "skips": [], "shortest_unfit": shortest}

    candidates, user = build_prompt(fitting, minutes, max_episodes, desc_chars)

    def _checked(raw):
        out = parse(raw, candidates)
        # Belt and braces: candidates are pre-filtered, so this should never bite.
        ceiling = minutes * BUDGET_TOLERANCE
        out["picks"] = [p for p in out["picks"] if (p.get("duration_min") or 0) <= ceiling]
        out["shortest_unfit"] = None
        return out

    text = call(user)
    if not text:
        text = ""
    try:
        return _checked(text)
    except ValueError as first_err:
        # Local models fairly regularly break their own JSON (an unescaped quote inside
        # a reason is the one seen in practice). One corrective retry is far cheaper
        # than failing a whole triage the user waited a minute for.
        retry_user = user + REPAIR_HINT.format(err=str(first_err)[:120])
        try:
            return _checked(call(retry_user))
        except ValueError as second_err:
            raise RuntimeError(
                f"{backend} returned unparseable JSON twice "
                f"(first: {str(first_err)[:100]}; retry: {str(second_err)[:100]}). "
                "Try again, or switch model via OLLAMA_MODEL."
            ) from second_err
