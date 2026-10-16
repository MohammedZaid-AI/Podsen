"""The shared HTML shell: esc() and page(). No templates directory, no client JS, no build step.

Page bodies are built in v2/app_views.py (the app) and v2/label_views.py (/label).
"""

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
  .standfirst {{ font-family: var(--display-serif); font-size: 21px; line-height: 1.35;
    color: var(--ink-display); margin: 0 0 14px; }}
  .lede {{ font-style: italic; font-weight: 600; color: var(--accent-coral); }}
  .rest {{ font-style: normal; font-weight: 700; color: var(--ink-display); }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: #9aa0a6; margin: 32px 0 12px; }}
  .card {{ border: 1px solid #2a2c31; border-radius: 12px; padding: 16px; margin: 10px 0; background: #16171b; }}
  .pick {{ border-color: #1db954; }}
  .meta {{ color: #9aa0a6; font-size: 14px; }}
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
