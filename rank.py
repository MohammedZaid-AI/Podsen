"""The only AI in the app: score fit against the time budget + write the reasons.

Two interchangeable backends behind one `rank()` call, selected by RANK_BACKEND:
  claude  (default) -- Anthropic API, better output, costs ~$0.02/run
  ollama            -- local/self-hosted model, free, weaker output + a host dependency

Prompt building and response parsing are shared, so the backends differ only in
transport. Adding a third is one function plus a dict entry.
"""

import json
import os

import requests

# --- backend selection -------------------------------------------------------
DEFAULT_BACKEND = "claude"


def _backend_name() -> str:
    name = (os.environ.get("RANK_BACKEND") or DEFAULT_BACKEND).strip().lower()
    if name not in _BACKENDS:
        raise RuntimeError(
            f"RANK_BACKEND={name!r} is not supported; use one of {sorted(_BACKENDS)}"
        )
    return name


# Claude handles the full backlog comfortably. Ollama does not: its default context
# is 4096 tokens total and the /v1 endpoint ignores per-request num_ctx, so the
# prompt has to be smaller or the answer gets truncated before the JSON.
LIMITS = {"claude": (50, 600), "ollama": (30, 350)}

# --- Anthropic ---------------------------------------------------------------
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
_client = None


def _anthropic():
    global _client
    if _client is None:
        from anthropic import Anthropic  # imported lazily so RANK_BACKEND=ollama needs no key

        _client = Anthropic()
    return _client


# --- Ollama ------------------------------------------------------------------
OLLAMA_BASE_URL = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")  # Ollama ignores it; a proxy may not
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT_MS", 180_000)) / 1000


SYSTEM = """You are Podsen, a podcast triage assistant. Tagline: "Listen to what's worth it."
The user already follows every show below and has a backlog of unheard episodes. Given how much time they have RIGHT NOW, pick the 1-2 episodes genuinely worth hearing today and list 3-6 that are safe to skip.
Judge on: topical substance, timeliness (news/interview-of-the-moment decays fast; evergreen deep-dives don't), and fit to the time budget (an episode much longer than the budget is a poor fit unless exceptional; a couple of short ones can stack).
This is triage of shows they chose to follow - not discovery, and never suggest unfollowing.
Every reason is ONE sentence that names something concrete from that episode's own title or description - a person, a claim, an event, a question it takes up. A reason that would still make sense pasted under a different episode is wrong. Never write filler like "sounds interesting", "a good listen", or "worth your time".

A pick's reason must say why it earns the listening time they have - what they get out of it - not merely restate the blurb.

A skip's reason must say why it KEEPS, and each one must give a different kind of reason. Draw on: it is evergreen and will be just as good next month; it is a re-run or "Best Of"; its news hook has already been overtaken; it is a niche or narrow-interest instalment of a show they otherwise like; the same ground is covered by a pick. Running time alone is NEVER a sufficient reason - do not write "it is N minutes long"; if length is the issue, say what they would be giving up the rest of the day to fit it in."""


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


def _call_ollama(user: str) -> str:
    r = requests.post(
        f"{OLLAMA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": OLLAMA_MODEL,
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        },
        timeout=OLLAMA_TIMEOUT_S,
    )
    if not r.ok:
        raise RuntimeError(f"ollama {r.status_code} at {OLLAMA_BASE_URL}: {r.text[:300]}")

    j = r.json()
    choice = (j.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    # Ollama's default context is 4096 tokens TOTAL (prompt + output) no matter what
    # the model supports, and the /v1 endpoint ignores `options.num_ctx`. A backlog
    # prompt is ~4k on its own, so the answer gets guillotined before the JSON.
    # Without this check the symptom is a baffling "no JSON in model output".
    if choice.get("finish_reason") == "length":
        raise RuntimeError(
            f"{OLLAMA_MODEL} hit the context limit "
            f"(used {(j.get('usage') or {}).get('total_tokens')} tokens) before finishing the JSON. "
            "Restart Ollama with OLLAMA_CONTEXT_LENGTH=16384, or lower LIMITS['ollama'] in rank.py."
        )

    # Reasoning models (gemma4, deepseek-r1, gpt-oss) split their output: the answer
    # lands in `content`, the scratchpad in `reasoning`. If an early stop leaves
    # `content` empty, the JSON is often still in `reasoning`.
    text = (msg.get("content") or "").strip() or (msg.get("reasoning") or "").strip()
    if not text:
        raise RuntimeError(
            f"empty response from {OLLAMA_MODEL} (finish_reason={choice.get('finish_reason')})"
        )
    return text


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
    max_episodes, desc_chars = LIMITS[backend]
    candidates, user = build_prompt(episodes, minutes, max_episodes, desc_chars)
    return parse(_BACKENDS[backend](user), candidates)
