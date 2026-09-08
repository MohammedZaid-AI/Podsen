"""Server-rendered HTML. No templates directory, no client JS, no build step."""

from html import escape


def esc(s) -> str:
    return escape("" if s is None else str(s), quote=True)


HERO_LEDE = "Listen to"
HERO_REST = "what's worth it."
_HERO_SPANS = f'<span class="lede">{HERO_LEDE}</span> <span class="rest">{HERO_REST}</span>'


def page(title: str, body: str, hero: bool = False) -> str:
    # Large display type is for the landing hero only - every other screen keeps the
    # same two-part treatment at brand size so results/feedback pages stay calm.
    if hero:
        header = f'<p class="wordmark">Podsen</p><h1 class="hero">{_HERO_SPANS}</h1>'
    else:
        header = f'<h1 class="wordmark">Podsen</h1><p class="tagline">{_HERO_SPANS}</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;1,500;1,600&display=swap" rel="stylesheet">
<style>
  :root {{
    color-scheme: dark;
    --accent-coral: #E8637A;
    /* The brief asked for near-black #1A1A1A, but this app is dark-themed and that
       would be invisible on #101114. Display ink is a warm off-white instead; swap
       --ink-display to var(--ink-on-light) if the page ever goes light. */
    --ink-display: #F4F1EC;
    --ink-on-light: #1A1A1A;
    --display-serif: "Playfair Display", "Iowan Old Style", Palatino, Georgia, serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #101114; color: #e9e9ec;
    font: 16px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; }}
  main {{ max-width: 640px; margin: 0 auto; padding: 32px 20px 80px; }}
  /* Editorial display treatment: one sentence, two emphases. Same family and
     baseline throughout - only style, weight and colour change. */
  .wordmark {{ font-family: var(--display-serif); font-size: 19px; font-weight: 700;
    letter-spacing: .01em; margin: 0 0 10px; color: #e9e9ec; }}
  .hero {{ font-family: var(--display-serif); font-weight: 700;
    font-size: clamp(36px, 7.5vw, 60px); line-height: 1.04; letter-spacing: -.015em;
    margin: 0 0 30px; }}
  .tagline {{ font-family: var(--display-serif); font-size: 16px; line-height: 1.3;
    margin: 0 0 28px; }}
  .lede {{ font-style: italic; font-weight: 600; color: var(--accent-coral); }}
  .rest {{ font-style: normal; font-weight: 700; color: var(--ink-display); }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: #9aa0a6; margin: 32px 0 12px; }}
  .card {{ border: 1px solid #2a2c31; border-radius: 12px; padding: 16px; margin: 10px 0; background: #16171b; }}
  .pick {{ border-color: #1db954; }}
  .meta {{ color: #9aa0a6; font-size: 14px; }}
  .reason {{ margin: 8px 0 0; }}
  .skip {{ padding: 10px 0; border-bottom: 1px solid #23252a; }}
  .skip:last-child {{ border-bottom: 0; }}
  button, .btn {{ font: inherit; background: #1db954; color: #06240f; border: 0;
    border-radius: 999px; padding: 10px 18px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; }}
  button.secondary {{ background: #2a2c31; color: #e9e9ec; }}
  input[type=number] {{ font: inherit; background: #16171b; color: #e9e9ec; border: 1px solid #2a2c31;
    border-radius: 8px; padding: 9px 12px; width: 90px; }}
  label {{ display: block; margin: 6px 0; }}
  .row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  textarea {{ width: 100%; font: inherit; background: #16171b; color: #e9e9ec; border: 1px solid #2a2c31; border-radius: 8px; padding: 10px; }}
  a {{ color: #6fd08c; }}
  .warn {{ border: 1px solid #5a4a1e; background: #241f12; color: #e8d9a8;
    border-radius: 10px; padding: 12px 14px; margin: 12px 0 0; font-size: 14px; }}
  pre {{ white-space: pre-wrap; word-break: break-word; }}
</style></head><body><main>
{header}
{body}
</main></body></html>"""


def landing() -> str:
    return """<p>Podsen looks at the unheard episodes piling up across shows you already follow,
and &mdash; given how much time you have right now &mdash; tells you the 1&ndash;2 worth playing today,
plus what's safe to skip. It never deletes or skips anything without you confirming.</p>
<p style="margin-top:24px"><a class="btn" href="/login">Connect Spotify</a></p>"""


def time_form(user: dict | None) -> str:
    name = esc((user or {}).get("name"))
    return f"""<p>Connected as <strong>{name}</strong>. <a href="/logout">Log out</a></p>
<h2>How much time do you have right now?</h2>
<form method="post" action="/triage" class="row">
  <button name="minutes" value="15">15 min</button>
  <button name="minutes" value="30">30 min</button>
  <button name="minutes" value="60">60 min</button>
</form>
<form method="post" action="/triage" class="row" style="margin-top:12px">
  <input type="number" name="minutes" min="1" max="600" value="45">
  <button class="secondary">Go</button>
</form>
<p class="meta" style="margin-top:20px">Scanning your backlog + ranking takes ~10&ndash;20 seconds.</p>"""


def dropped_note(dropped) -> str:
    """Say plainly that this triage did not see the whole backlog.

    Silence here is the actual bug: picks chosen from a partial backlog look exactly
    like picks chosen from a complete one.
    """
    if not dropped:
        return ""
    names = ", ".join(sorted({d["show"] for d in dropped}))
    reasons = ", ".join(sorted({d["reason"] for d in dropped}))
    n = len({d["id"] for d in dropped})
    return (
        f'<p class="warn">Couldn\'t check {n} of your shows this time '
        f"({esc(reasons)}): {esc(names)}. These picks were chosen from the rest of "
        "your backlog &mdash; try again in a moment for the full picture.</p>"
    )


def results(sid, minutes, picks, skips, backlog, shows_scanned, dropped=None) -> str:
    def pick(p):
        return f"""<div class="card pick">
    <label class="row" style="align-items:flex-start">
      <input type="checkbox" name="pick" value="{esc(p['id'])}" checked style="margin-top:5px">
      <span><strong>{esc(p['title'])}</strong><br>
      <span class="meta">{esc(p['show'])} &middot; {esc(p['duration_min'])} min</span>
      <span class="reason">{esc(p['reason'])}</span></span>
    </label></div>"""

    def skip(s):
        return f"""<div class="skip"><strong>{esc(s['title'])}</strong>
    <span class="meta">&mdash; {esc(s['show'])}</span><br><span class="reason">{esc(s['reason'])}</span></div>"""

    picks_html = "".join(pick(p) for p in picks) or (
        "<p>No strong pick &mdash; everything in the backlog is low-priority right now.</p>"
    )
    return f"""<p class="meta">{esc(backlog)} unheard episodes across {esc(shows_scanned)} shows &middot; {esc(minutes)} min budget</p>
{dropped_note(dropped)}
<form method="post" action="/confirm">
  <input type="hidden" name="sid" value="{esc(sid)}">
  <h2>Worth it today</h2>
  {picks_html}
  <h2>Safe to skip</h2>
  {"".join(skip(s) for s in skips)}
  <p style="margin-top:24px"><label><input type="checkbox" name="make_playlist" checked>
    Put the checked picks in a private Spotify playlist</label></p>
  <button>Confirm</button>
  <a class="btn secondary" href="/app" style="margin-left:8px">Back</a>
</form>"""


def done(sid, picks, playlist) -> str:
    items = "".join(
        f"<li><strong>{esc(p['title'])}</strong> &mdash; {esc(p['show'])}</li>" for p in picks
    )
    body = f"<ul>{items}</ul>" if picks else "<p>Nothing selected.</p>"
    pl = ""
    if playlist and playlist.get("url"):
        pl = (
            f'<p style="margin:16px 0"><a class="btn" href="{esc(playlist["url"])}" '
            'target="_blank" rel="noopener">Open playlist in Spotify</a></p>'
        )
    return f"""<h2>Confirmed</h2>
{body}
{pl}
<h2>One question</h2>
<form method="post" action="/feedback">
  <input type="hidden" name="sid" value="{esc(sid)}">
  <p>Was this useful?</p>
  <label><input type="radio" name="useful" value="yes" required> Yes, I'd use it again</label>
  <label><input type="radio" name="useful" value="somewhat"> Somewhat</label>
  <label><input type="radio" name="useful" value="no"> No</label>
  <p style="margin-top:16px">Would you pay for this?</p>
  <label><input type="radio" name="pay" value="yes" required> Yes</label>
  <label><input type="radio" name="pay" value="maybe"> Maybe</label>
  <label><input type="radio" name="pay" value="no"> No</label>
  <p style="margin-top:16px"><textarea name="note" rows="3" placeholder="Anything else? (optional)"></textarea></p>
  <button>Send</button>
</form>"""


def thanks() -> str:
    return '<p>Thanks &mdash; noted.</p><p><a href="/app">Run it again</a></p>'
