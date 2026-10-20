"""The shared HTML shell: esc() and page(). No templates directory, no client JS, no build step.

Page bodies are built in v2/app_views.py (the app) and v2/label_views.py (/label).
"""

from html import escape


def esc(s) -> str:
    return escape("" if s is None else str(s), quote=True)


HERO_LEDE = "Listen to"
HERO_REST = "what's worth it."
_HERO_SPANS = f'<span class="lede">{HERO_LEDE}</span> <span class="rest">{HERO_REST}</span>'

# The ONE script: progressive enhancement, under 30 lines, no libraries. Every form
# works without it. On submit it marks the form busy (a second submit is ignored),
# shows "Working..." on the pressed button (its width kept) or the form's
# data-busy-note, and disables the form's buttons one tick later, after the browser
# has read the pressed button's name and value. pageshow (e.g. Back) restores it all.
PROGRESSIVE_JS = """<script>
document.addEventListener("submit", function (e) {
  var f = e.target, b = e.submitter, note = f.querySelector(".busy-note");
  if (f.dataset.busy) { e.preventDefault(); return; }
  f.dataset.busy = "1";
  if (note && f.dataset.busyNote) { note.textContent = f.dataset.busyNote; note.hidden = false; }
  if (b && !(note && f.dataset.busyNote)) {
    b.style.width = b.offsetWidth + "px"; b.dataset.label = b.textContent; b.textContent = "Working...";
    if (b.scrollWidth > b.clientWidth) b.textContent = b.dataset.label;  // too narrow: keep the label
  }
  if (b) b.classList.add("busy");
  setTimeout(function () { f.querySelectorAll("button").forEach(function (x) { x.disabled = true; }); }, 0);
});
window.addEventListener("pageshow", function () {
  document.querySelectorAll("form[data-busy]").forEach(function (f) {
    delete f.dataset.busy;
    f.querySelectorAll("button").forEach(function (x) {
      x.disabled = false; x.classList.remove("busy");
      if (x.dataset.label) { x.textContent = x.dataset.label; delete x.dataset.label; x.style.width = ""; }
    });
    var note = f.querySelector(".busy-note");
    if (note) note.hidden = true;
  });
});
</script>"""


def page(title: str, body: str, hero: bool = False) -> str:
    # Large display type is for the landing hero only - every other screen keeps the
    # same two-part treatment at brand size so results/feedback pages stay calm.
    # The logo sits top left on every page and goes home (the app, once signed in).
    logo = '<img class="logo" src="/static/podsen-logo.png" alt="" width="56" height="56">'
    if hero:
        header = (f'<a class="brand" href="/v2/">{logo}<p class="wordmark">Podsen</p></a>'
                  f'<h1 class="hero">{_HERO_SPANS}</h1>')
    else:
        header = (f'<a class="brand" href="/v2/">{logo}<h1 class="wordmark">Podsen</h1></a>'
                  f'<p class="tagline">{_HERO_SPANS}</p>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
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
    font: 16px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
    -webkit-text-size-adjust: 100%; overflow-wrap: break-word; }}
  /* Phones first: a 16px gutter, and nothing may be wider than the screen. */
  main {{ max-width: 640px; margin: 0 auto; padding: 28px 16px 72px; }}
  img, audio, video {{ max-width: 100%; }}
  /* Editorial display treatment: one sentence, two emphases. Same family and
     baseline throughout - only style, weight and colour change. */
  /* The mark is a transparent PNG in the display ink, so it sits on the page itself. */
  .brand {{ display: inline-flex; align-items: center; gap: 12px; margin: 0 0 12px;
    min-height: 44px; text-decoration: none; }}
  .logo {{ width: 56px; height: 56px; flex: none; }}
  .wordmark {{ font-family: var(--display-serif); font-size: 26px; font-weight: 700;
    letter-spacing: .01em; margin: 0; color: #e9e9ec; }}
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
  /* 44px minimum height: Apple's smallest comfortable touch target. */
  button, .btn {{ font: inherit; background: #1db954; color: #06240f; border: 0;
    border-radius: 999px; padding: 10px 20px; min-height: 44px; line-height: 24px;
    font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block;
    text-align: center; }}
  button.secondary {{ background: #2a2c31; color: #e9e9ec; }}
  /* Every button answers a tap: hover lightens, pressed darkens and sinks, keyboard
     focus gets a clear outline, and a busy form's button pulses. */
  button, .btn {{ transition: filter .12s, transform .06s, box-shadow .12s; }}
  button:hover, .btn:hover {{ filter: brightness(1.1); }}
  button:active, .btn:active {{ filter: brightness(.85); transform: translateY(1px) scale(.98);
    box-shadow: inset 0 2px 5px rgba(0, 0, 0, .35); }}
  button:focus-visible, .btn:focus-visible, a:focus-visible, summary:focus-visible,
  input:focus-visible, textarea:focus-visible {{ outline: 3px solid #6fd08c; outline-offset: 2px; }}
  button[disabled] {{ cursor: progress; }}
  button.busy {{ animation: podsen-busy 1s ease-in-out infinite; }}
  @keyframes podsen-busy {{ 50% {{ opacity: .6; }} }}
  @media (prefers-reduced-motion: reduce) {{ button.busy {{ animation: none; opacity: .7; }}
    button, .btn {{ transition: none; }} }}
  .busy-note {{ margin: 10px 0 0; color: #6fd08c; }}
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
</main>{PROGRESSIVE_JS}</body></html>"""
