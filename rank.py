"""The only AI in the app: score fit against the time budget + write the reasons.

Two interchangeable backends behind one `rank()`, selected by RANK_BACKEND:
  groq    (default) -- Groq API, needs GROQ_API_KEY
  claude            -- Anthropic API, needs ANTHROPIC_API_KEY

Prompt building, JSON repair and response parsing are shared, so the backends differ
only in transport.
"""

import json
import os

import requests

# Load .env from this file's directory if python-dotenv is installed.
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# --- backend selection -------------------------------------------------------
DEFAULT_BACKEND = "groq"


def _backend_name() -> str:
    name = (os.environ.get("RANK_BACKEND") or DEFAULT_BACKEND).strip().lower()
    if name not in _BACKENDS:
        raise RuntimeError(
            f"RANK_BACKEND={name!r} is not supported; use one of {sorted(_BACKENDS)}"
        )
    return name


# Maximum candidate episodes and description characters sent to each backend.
LIMITS = {"claude": (50, 600), "groq": (40, 450)}

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


# --- Groq --------------------------------------------------------------------
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_BASE_URL = (
    os.environ.get("GROQ_BASE_URL") or "https://api.groq.com/openai/v1"
).rstrip("/")
GROQ_TIMEOUT_S = float(os.environ.get("GROQ_TIMEOUT_S", "300"))


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




def _call_groq(user: str) -> str:
    """Call Groq and detect reasoning/token-limit failures clearly."""

    api_key = (
        os.environ.get("GROQ_API_KEY") or GROQ_API_KEY
    ).strip()

    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing.")

    model = (
        os.environ.get("GROQ_MODEL") or GROQ_MODEL
    ).strip()

    base_url = (
        os.environ.get("GROQ_BASE_URL") or GROQ_BASE_URL
    ).rstrip("/")

    timeout_s = float(
        os.environ.get("GROQ_TIMEOUT_S", str(GROQ_TIMEOUT_S))
    )

    body = {
        "model": model,
        "temperature": 0.2,
        "max_completion_tokens": 3000,
        "reasoning_effort": "low",
        "messages": [
            {
                "role": "system",
                "content": (
                    SYSTEM
                    + "\n\nReturn ONLY a valid JSON object. "
                    "Do not include markdown or text outside JSON. "
                    "Keep the response concise and follow the "
                    "requested output schema exactly."
                ),
            },
            {
                "role": "user",
                "content": user,
            },
        ],
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout_s,
        )
    except requests.Timeout as exc:
        raise RuntimeError(
            f"Groq timed out after {timeout_s} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not connect to Groq: {exc}"
        ) from exc

    if not response.ok:
        raise RuntimeError(
            f"Groq HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:
        data = response.json()
        choices = data.get("choices") or []

        if not choices:
            raise RuntimeError("Groq returned no choices.")

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        finish_reason = choice.get("finish_reason")

        if isinstance(content, str) and content.strip():
            return content.strip()

        if finish_reason == "length":
            raise RuntimeError(
                "Groq stopped at the completion-token limit "
                "without producing final content. Reduce "
                "reasoning_effort or increase the token limit."
            )

        raise RuntimeError(
            f"Groq returned empty content "
            f"(finish_reason={finish_reason!r}, "
            f"model={data.get('model')!r})."
        )

    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeError(
            "Unexpected response format from Groq: "
            f"{response.text[:1000]}"
        ) from exc

_BACKENDS = {
    "claude": _call_claude,
    "groq": _call_groq,
}

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
        # Models can occasionally break their own JSON (an unescaped quote inside
        # a reason is the one seen in practice). One corrective retry is far cheaper
        # than failing a whole triage the user waited a minute for.
        retry_user = user + REPAIR_HINT.format(err=str(first_err)[:120])
        try:
            return _checked(call(retry_user))
        except ValueError as second_err:
            raise RuntimeError(
                f"{backend} returned unparseable JSON twice "
                f"(first: {str(first_err)[:100]}; retry: {str(second_err)[:100]}). "
                "Try again, or check GROQ_MODEL / the selected backend."
            ) from second_err
