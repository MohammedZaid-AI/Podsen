"""HTML for the V2 app under /v2. Strings only, no client JS, reusing views.page().

Every interpolated value goes through esc(): titles, descriptions, show names and
URLs all come from public feeds or Apple's API and are untrusted.
"""

from urllib.parse import quote, quote_plus, urlparse

from v2 import engine, preferences
from v2.db import NOTE_MAX
from views import esc
from views import page as base_page

# Link-outs only: a search URL built from public RSS text. No data comes back from it.
SPOTIFY_SEARCH = "https://open.spotify.com/search/"
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="
AMAZON_MUSIC_SEARCH = "https://music.amazon.com/search/"
MIN_SHOWS = 1  # one show is enough: someone behind on a single show still gets picks
SKIPS_SHOWN = 10

_STYLE = """<style>
  /* Phone-first: every control is at least 44px tall, nothing is wider than 375px. */
  .nav { display: flex; gap: 4px 16px; align-items: center; flex-wrap: wrap; margin: -12px 0 18px; font-size: 15px; }
  .nav a, .foot a, .countries a, .countries strong { display: inline-flex; align-items: center; min-height: 44px; }
  .nav a, .nav .linkbtn { color: #9aa0a6; text-decoration: none; }
  .nav a[aria-current=page] { color: #e9e9ec; font-weight: 600; box-shadow: inset 0 -2px 0 #1db954; }
  .ico { width: 18px; height: 18px; flex: none; margin-right: 8px; }
  .btn.google { display: inline-flex; align-items: center; background: #fff; color: #1f1f1f; }
  .btn.google .ico { width: 20px; height: 20px; margin-right: 12px; }
  .custom { display: flex; align-items: center; gap: 8px; margin: 0; color: #9aa0a6; }
  .next { margin: 4px 0 14px; }
  .nav form { margin: 0; }
  .foot { margin-top: 48px; padding-top: 14px; border-top: 1px solid #23252a; font-size: 13px; color: #9aa0a6; }
  .show { display: flex; gap: 12px; align-items: center; padding: 10px 0; border-bottom: 1px solid #23252a; }
  .show:last-child { border-bottom: 0; }
  .show img { width: 56px; height: 56px; border-radius: 8px; flex: none; background: #23252a; object-fit: cover; }
  .show .grow { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .show form { margin: 0; flex: none; }
  .links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; font-size: 14px; }
  .links form { margin: 0; display: inline; }
  .links a, .links button { display: inline-flex; align-items: center; min-height: 44px; padding: 0 14px;
    border: 1px solid #2a2c31; border-radius: 999px; background: #16171b; color: #e9e9ec;
    text-decoration: none; font-size: 14px; font-weight: 500; line-height: 1.2; }
  details > summary { cursor: pointer; padding: 10px 0; min-height: 44px; line-height: 24px; }
  audio { display: block; width: 100%; margin-top: 6px; }
  input[type=text], input[type=email], input[type=password], input[type=search] { font: inherit;
    background: #16171b; color: #e9e9ec; border: 1px solid #2a2c31; border-radius: 8px;
    padding: 10px 12px; width: 100%; min-height: 44px; }
  .row input[type=search] { flex: 1 1 auto; min-width: 0; width: auto; }
  .row button { flex: none; }
  .field { margin: 0 0 14px; }
  .field > span { display: block; color: #9aa0a6; font-size: 14px; margin-bottom: 4px; }
  .err { border: 1px solid #6b2a33; background: #2a1418; color: #f1c4cb;
    border-radius: 10px; padding: 12px 14px; margin: 12px 0; font-size: 14px; }
  .ok { color: #6fd08c; }
  .note-form { margin: 12px 0 0; }
  .note-form textarea { margin-top: 2px; }
  .sent-note { white-space: pre-wrap; margin: 8px 0 0; }
  .prefs fieldset { border: 0; padding: 0; margin: 0 0 18px; }
  .prefs legend { font-weight: 600; margin-bottom: 4px; }
  .prefs label.choice { display: flex; align-items: center; gap: 10px; min-height: 44px; margin: 0; }
  .prefs input[type=radio], .prefs input[type=checkbox] { width: 22px; height: 22px; flex: none;
    margin: 0; accent-color: #1db954; }
  .links a { transition: filter .12s, transform .06s; }
  .links a:hover { filter: brightness(1.25); border-color: #3a3d44; }
  .links a:active { filter: brightness(.85); transform: translateY(1px) scale(.98); }
  button.linkbtn:active, button.linkbtn:hover { transform: none; box-shadow: none; }
  button.linkbtn:active { opacity: .7; }
  button.linkbtn { background: none; color: #6fd08c; padding: 10px 0; border-radius: 0;
    font-weight: 400; text-decoration: underline; }
  .danger { background: #b3394a; color: #fff; }
  label.show { cursor: pointer; margin: 0; min-height: 68px; }
  label.show input[type=checkbox] { width: 24px; height: 24px; flex: none; margin: 0 2px; accent-color: #1db954; }
  .countries { display: flex; gap: 0 18px; font-size: 15px; margin: -6px 0 4px; }
  .countries strong { color: #e9e9ec; }
  .note { color: #6fd08c; margin: 8px 0 12px; }
  .nobox { width: 28px; flex: none; }
  .nofeed { font-style: italic; }
  .unused { color: #c4c7cc; margin: 14px 0 0; padding: 12px 14px; border-left: 3px solid #2a2c31; }
  .needs { color: #e8d9a8; font-size: 14px; }
  .landing-cta { margin: 26px 0 14px; }  .landing-cta .btn { padding: 12px 26px; font-size: 17px; }
  /* First-run guide: three numbered steps, the current one lit. */
  .steps { display: flex; gap: 8px; margin: 0 0 20px; padding: 0; list-style: none; font-size: 14px; }
  .steps li { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px;
    padding: 10px 6px; text-align: center;
    border: 1px solid #23252a; border-radius: 10px; color: #9aa0a6; line-height: 1.25; }
  .steps .n { display: inline-flex; align-items: center; justify-content: center; flex: none;
    width: 24px; height: 24px; border-radius: 50%; background: #23252a; font-weight: 600; }
  .steps .now { border-color: #1db954; color: #e9e9ec; }
  .steps .now .n { background: #1db954; color: #101114; }
  .steps .done .n { background: #1f3b2a; color: #6fd08c; }
  .welcome h2 { margin-top: 0; }
  .welcome p { margin: 6px 0; }
  .lead { color: #c4c7cc; margin: -4px 0 12px; }
  /* Account button, top right, beside the logo. */
  main { position: relative; }
  .me { position: absolute; top: 28px; right: 16px; display: flex; align-items: center; text-decoration: none;
    justify-content: center; width: 48px; height: 48px; border-radius: 50%; }
  .me img, .me .initial { width: 40px; height: 40px; border-radius: 50%; object-fit: cover;
    box-shadow: 0 0 0 2px #2a2c31; background: #23252a; }
  .me .initial { display: flex; align-items: center; justify-content: center;
    font-weight: 600; color: #e9e9ec; }
  .me:hover img, .me:hover .initial, .me[aria-current=page] img,
  .me[aria-current=page] .initial { box-shadow: 0 0 0 2px #1db954; }
  /* A play triangle looks off-center when its box is centered: nudge it right a touch. */
  .me .initial img.mark { width: 26px; height: 26px; border-radius: 0; box-shadow: none;
    background: none; object-fit: contain; transform: translateX(1px); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  details.more { margin-top: 4px; }  details.more .links { margin-top: 0; }
  @media (max-width: 480px) {
    .show img { width: 48px; height: 48px; }
    .standfirst { font-size: 19px; }
  }
</style>"""


def _count(n, word: str) -> str:
    """"1 show", "2 shows": counts on these pages are often 1 now that one show is enough."""
    return f"{esc(n)} {word}{'' if n == 1 else 's'}"


def _csrf(token: str) -> str:
    """Hidden CSRF field. Every V2 POST form includes it; web.py rejects POSTs without it."""
    return f'<input type="hidden" name="csrf_token" value="{esc(token)}">'


NAV = (("/v2/today", "Today"), ("/v2/shows", "My shows"))


def account_button(me: dict | None, active: str = "") -> str:
    """Top right on every signed-in page: your Google picture (or the Podsen mark), linking to
    Account, where Log out and settings live. The picture comes only from Google's image
    host (auth.avatar_url checks it) and is loaded with no referrer."""
    me = me or {}
    email = me.get("email") or ""
    url = me.get("avatar_url")
    if url and urlparse(url).scheme == "https":
        face = (f'<img src="{esc(url)}" alt="" width="40" height="40" referrerpolicy="no-referrer">')
    else:
        # No Google picture: the Podsen mark, centered in the circle.
        face = ('<span class="initial"><img class="mark" src="/static/podsen-logo.png" alt="" '
                'width="26" height="26"></span>')
    current = ' aria-current="page"' if active == "/v2/account" else ""
    return (f'<a class="me" href="/v2/account"{current} title="Account ({esc(email)})">'
            f'{face}<span class="sr">Account</span></a>')


def page(title: str, body: str, logged_in: bool = False, hero: bool = False, csrf: str = "",
         active: str = "", me: dict | None = None) -> str:
    """`active`: the current path, so the nav shows where you are. `me`: the signed-in
    user, for the account button."""
    if logged_in:
        current = ' aria-current="page"'
        items = "".join(f'<a href="{href}"{current if active == href else ""}>{label}</a>'
                        for href, label in NAV)
        nav = f'{account_button(me, active)}<nav class="nav">{items}</nav>'
    else:
        nav = ""
    foot = '<footer class="foot"><a href="/v2/privacy">Privacy</a> &middot; Podsen</footer>'
    return base_page(title, f"{_STYLE}{nav}{body}{foot}", hero=hero)


def _error(msg):
    return f'<p class="err">{esc(msg)}</p>' if msg else ""


STEPS = ("Add shows", "Pick time", "Get picks")


def steps(current: int) -> str:
    """The three-step guide, so a new user always knows where they are and what's next.
    `current` is 1, 2 or 3."""
    items = []
    for i, label in enumerate(STEPS, 1):
        cls = "now" if i == current else ("done" if i < current else "")
        mark = "&#10003;" if i < current else str(i)
        here = ' aria-current="step"' if i == current else ""
        items.append(f'<li class="{cls}"{here}><span class="n">{mark}</span>{esc(label)}</li>')
    return f'<ol class="steps" aria-label="How Podsen works">{"".join(items)}</ol>'


# --- landing + auth --------------------------------------------------------------------

GOOGLE_BUTTON = ('<a class="btn google" href="/v2/login"><img class="ico" src="/static/icons/google.svg" '
                 'alt="" width="20" height="20">Continue with Google</a>')


def _icon(name: str) -> str:
    """A brand mark beside a link label. Decorative: the label says where it goes."""
    return f'<img class="ico" src="/static/icons/{name}.svg" alt="" width="18" height="18">'


def landing() -> str:
    # Three things only: what it does, the one button, where you can listen.
    return f"""<p class="standfirst">Tell Podsen how much time you have, and it picks which
episodes from your podcasts are worth it.</p>
<p class="landing-cta">{GOOGLE_BUTTON}</p>
<p class="meta">Works with Spotify, YouTube, Apple Podcasts and Amazon Music.</p>"""


def login_error(message: str) -> str:
    """A sign-in step failed: say what happened and offer a clean retry."""
    return f"""<h2>Sign in</h2>
{_error(message)}
<p style="margin-top:20px">{GOOGLE_BUTTON}</p>
<p class="meta" style="margin-top:16px"><a href="/v2/">Back to Podsen</a></p>"""


# --- shows -------------------------------------------------------------------------------

def _cover(url):
    return f'<img src="{esc(url)}" alt="" loading="lazy">' if url else '<img alt="">'


NO_FEED = '<br><span class="meta nofeed">No public feed, can not be analyzed</span>'


def _search_result(s: dict, have: set, csrf: str) -> str:
    # The no-feed message goes under the name, not beside it: on a phone a side-by-side
    # message squeezes the name into a column a few letters wide.
    note, action = "", ""
    if not s.get("feed_url"):
        note = NO_FEED
    elif s["itunes_id"] in have:
        action = '<span class="meta ok">Added</span>'
    else:
        action = f"""<form method="post" action="/v2/shows/add">{_csrf(csrf)}
      <input type="hidden" name="itunes_id" value="{esc(s['itunes_id'])}"><button>Add</button></form>"""
    return f"""<div class="show">{_cover(s.get('artwork_url'))}
  <div class="grow"><strong>{esc(s['name'])}</strong><br><span class="meta">{esc(s.get('artist'))}</span>{note}</div>
  {action}</div>"""


def _note(msg):
    return f'<p class="note">{esc(msg)}</p>' if msg else ""


added_line = _note  # the same line, used by web.py above the preference questions


def added_note(added: int, skipped: int = 0) -> str | None:
    parts = []
    if added:
        parts.append(f"Added {added} show{'s' if added != 1 else ''}.")
    if skipped:
        parts.append(f"{skipped} had no public feed, so {'it was' if skipped == 1 else 'they were'} not added.")
    return " ".join(parts) or None


def trending_section(trending: dict, have: set, csrf: str, back: str, heading: str) -> str:
    """Trending shows as one checkbox form. Shows already added are left out; a show
    with no public feed is listed without a checkbox."""
    code = trending["country"]
    switch = " ".join(
        f"<strong>{esc(label)}</strong>" if c == code
        else f'<a href="/v2/{back}?country={esc(c)}">{esc(label)}</a>'
        for c, label in (("in", "India"), ("us", "US"), ("gb", "UK"))
    )
    head = f'<h2>{esc(heading)}</h2><div class="countries">{switch}</div>'
    if trending.get("error"):
        return f'{head}<p class="warn">{esc(trending["error"])} Search still works.</p>'

    rows, addable = [], 0
    for s in trending["shows"]:
        if s["itunes_id"] in have:
            continue
        meta = " &middot; ".join(esc(x) for x in (s.get("artist"), s.get("genre")) if x)
        name = f'<strong>{esc(s["name"])}</strong><br><span class="meta">{meta}</span>'
        if s.get("feed_url"):
            addable += 1
            rows.append(f"""<label class="show">
  <input type="checkbox" name="itunes_id" value="{esc(s['itunes_id'])}">{_cover(s.get('artwork_url'))}<div class="grow">{name}</div></label>""")
        else:
            # A spacer where the checkbox would be keeps the artwork in line.
            rows.append(f"""<div class="show"><span class="nobox"></span>{_cover(s.get('artwork_url'))}
  <div class="grow">{name}{NO_FEED}</div></div>""")
    if not rows:
        return f'{head}<p class="meta">You have added every trending show here.</p>'
    button = ('<p style="margin-top:14px"><button>Add selected</button></p>' if addable else "")
    return f"""{head}
<form method="post" action="/v2/shows/add-many">{_csrf(csrf)}
  <input type="hidden" name="back" value="{esc(back)}">
  <input type="hidden" name="country" value="{esc(code)}">
  {"".join(rows)}
  {button}
</form>"""


def shows_page(my_shows: list[dict], query: str = "", results=None, error: str | None = None,
               csrf: str = "", trending: dict | None = None, note: str | None = None) -> str:
    have = {s["itunes_id"] for s in my_shows}
    n = len(my_shows)
    if n < MIN_SHOWS:
        need = ('<p class="warn">Add a show you follow to get picks. '
                "Search or tick trending shows below.</p>")
    else:
        need = '<p class="next"><a class="btn" href="/v2/today">Pick episodes for today &rarr;</a></p>'

    found = ""
    if results is not None:
        found = "".join(_search_result(s, have, csrf) for s in results) or (
            f'<p class="meta">No podcasts found for &ldquo;{esc(query)}&rdquo;.</p>'
        )
        found = f"<h2>Results</h2>{found}"

    mine = "".join(
        f"""<div class="show">{_cover(s.get('artwork_url'))}
  <div class="grow"><strong>{esc(s['name'])}</strong><br><span class="meta">{esc(s.get('artist'))}</span></div>
  <form method="post" action="/v2/shows/remove">{_csrf(csrf)}
    <input type="hidden" name="itunes_id" value="{esc(s['itunes_id'])}">
    <button class="secondary">Remove</button></form></div>"""
        for s in reversed(my_shows)  # newest first: a show you just added is on top
    ) or '<p class="meta">No shows yet. Search below to add some.</p>'

    trend = trending_section(trending, have, csrf, "shows", "Trending") if trending else ""
    if n == 0:
        # The first screen after sign-in: say what Podsen is and the one thing to do now,
        # instead of an empty list and a warning.
        top = f"""{steps(1)}{_note(note)}
<div class="welcome">
<h2>Welcome to Podsen</h2>
<p>Too many episodes, not enough time? Podsen picks the ones worth playing right now,
and tells you what's safe to skip.</p>
<p><strong>Add a show you follow to get picks.</strong> Search for it below, or tick
some from the trending list.</p>
</div>"""
    else:
        # The shows you chose come first; finding more comes after.
        top = f"""{_note(note)}
<h2>My shows ({n})</h2>
{need}
{mine}"""
    return f"""{top}
<h2>Find your shows</h2>
<form method="get" action="/v2/shows" class="row" style="flex-wrap:nowrap">
  <input type="search" name="q" value="{esc(query)}" placeholder="Search podcasts" required maxlength="100">
  <button>Search</button>
</form>
{_error(error)}
{found}
{trend}"""


# --- listening preferences -----------------------------------------------------------------

def _choices(kind: str, name: str, options, chosen) -> str:
    return "".join(
        f'<label class="choice"><input type="{kind}" name="{name}" value="{esc(o)}"'
        f'{" checked" if o in chosen else ""}>{esc(label)}</label>'
        for o, label in options
    )


def preferences_form(prefs: dict, csrf: str, first_time: bool) -> str:
    """The three optional questions. first_time: before the first triage, with
    "Skip for now" (saves the defaults so it is not asked again). Otherwise reached from
    Account's "Set my preferences", filled in with the saved answers."""
    speeds = [(f"{v:g}", preferences.speed_label(v)) for v in preferences.SPEEDS]
    chosen_speed = {f"{prefs['listening_speed']:g}"}
    intro = ("<p>Three quick questions so picks fit how you listen. All optional; you can "
             "change them any time from Account.</p>" if first_time else "")
    later = ('<button class="secondary" name="action" value="skip">Skip for now</button>' if first_time
             else '<a class="btn secondary" href="/v2/account" style="background:#2a2c31;color:#e9e9ec">Cancel</a>')
    return f"""{steps(2) if first_time else ""}<h2>Your listening preferences</h2>
{intro}
<form method="post" action="/v2/preferences" class="prefs">{_csrf(csrf)}
  <fieldset><legend>Listening speed</legend>
    {_choices("radio", "speed", speeds, chosen_speed)}
  </fieldset>
  <fieldset><legend>What you want from podcasts <span class="meta">(pick any)</span></legend>
    {_choices("checkbox", "wants", [(w, w.capitalize()) for w in preferences.WANTS], prefs["wants"])}
  </fieldset>
  <fieldset><legend>What you usually skip <span class="meta">(pick any)</span></legend>
    {_choices("checkbox", "skips", [(k, k.capitalize()) for k in preferences.SKIPS], prefs["skips"])}
  </fieldset>
  <div class="row">
    <button name="action" value="save">Save</button>
    {later}
  </div>
</form>"""


def _prefs_summary(prefs: dict | None) -> str:
    if prefs is None:
        return "Not set yet: Podsen uses 1x speed and no topic preferences."
    wants = ", ".join(prefs["wants"]) or "nothing chosen"
    skips = ", ".join(prefs["skips"]) or "nothing chosen"
    return (f"Speed {esc(preferences.speed_label(prefs['listening_speed']))}. "
            f"Wants: {esc(wants)}. Skips: {esc(skips)}.")


# --- today -------------------------------------------------------------------------------

def today_form(n_shows: int, error: str | None = None, csrf: str = "",
               trending: dict | None = None, have: set | None = None, note: str | None = None) -> str:
    if n_shows < MIN_SHOWS:
        trend = (trending_section(trending, have or set(), csrf, "today",
                                  f"Trending in {trending['label']}") if trending else "")
        return f"""{steps(1)}{_note(note)}{_error(error)}
<p>Add a show you follow to get started. Tick one below, or search for yours.</p>
{trend}
<p style="margin-top:20px"><a class="btn secondary" href="/v2/shows"
  style="background:#2a2c31;color:#e9e9ec">Add shows</a></p>"""
    return f"""{steps(2)}{_note(note)}{_error(error)}
<form method="post" action="/v2/today" data-busy-note="Finding your picks...">{_csrf(csrf)}
  <h2>How much time do you have right now?</h2>
  <p class="lead">Tap one. Podsen will pick what to play in that time.</p>
  <div class="row">
    <button name="minutes" value="15">15 min</button>
    <button name="minutes" value="30">30 min</button>
    <button name="minutes" value="60">60 min</button>
  </div>
  <div class="row" style="margin-top:12px">
    <label class="custom">Other:
      <input type="number" name="custom_minutes" min="1" max="600" value="45"> min</label>
    <button class="secondary" name="minutes" value="custom">Pick</button>
  </div>
  <p class="busy-note" hidden aria-live="polite"></p>
</form>
<p class="meta" style="margin-top:20px">Picks come from the newest episodes from your
{_count(n_shows, "show")} (last 30 days). The first time can take a few seconds.</p>"""


# --- results -----------------------------------------------------------------------------

def spotify_link(ep: dict) -> str:
    return SPOTIFY_SEARCH + quote(f"{ep.get('show', '')} {ep.get('title', '')}", safe="")


def youtube_link(ep: dict) -> str:
    # Quoted title keeps YouTube to that phrase; the show name disambiguates.
    return YOUTUBE_SEARCH + quote_plus(f'"{ep.get("title", "")}" {ep.get("show", "")}')


def amazon_music_link(ep: dict) -> str:
    return AMAZON_MUSIC_SEARCH + quote(f"{ep.get('show', '')} {ep.get('title', '')}", safe="")


def _apple_link(ep: dict) -> str:
    """Exact episode when Apple's guid/title match found one; otherwise say it's a search."""
    exact, show = ep.get("apple_episode_url"), ep.get("apple_url")
    if exact and urlparse(exact).scheme == "https":
        return f'<a href="{esc(exact)}" target="_blank" rel="noopener">{_icon("applepodcasts")}Open in Apple Podcasts</a>'
    if show and urlparse(show).scheme == "https":
        return f'<a href="{esc(show)}" target="_blank" rel="noopener">{_icon("applepodcasts")}Search on Apple Podcasts</a>'
    return ""


def failed_note(failed, total_shows: int | None = None) -> str:
    """Say which shows failed. When every show failed (common with a single show) there
    are no "other shows" to pick from, so say that instead."""
    if not failed:
        return ""
    n = len(failed)
    items = "; ".join(f"{f['show']} ({f['error']})" for f in failed)
    if total_shows is not None and n >= total_shows:
        whose = "your show" if n == 1 else f"any of your {n} shows"
        return (f'<p class="warn">Could not load {whose}: {esc(items)}. Nothing could be '
                "picked this time. Try again in a moment.</p>")
    return (
        f'<p class="warn">Could not load {n} show{"s" if n != 1 else ""}: {esc(items)}. '
        "These picks come from your other shows only.</p>"
    )


def about(minutes) -> str:
    """Feed durations usually leave out ads inserted at playback (a "28 min" episode
    can play as 30:56), so the label says "about". Budget math uses the raw number."""
    return f"about {esc(minutes)} min"


def unused_note(picks, skips, budget) -> str:
    """Say why time is left over: never leave the user guessing at a gap."""
    info = engine.unused_time(picks, skips, budget)
    if not info:
        return ""
    left = f"{esc(info['left'])} min left."
    if info["reason"] == "nothing_fits":
        why = (f"Nothing else fits. Your shortest unheard episode is "
               f"{about(info['shortest'])}.")
    elif info["reason"] == "backlog_empty":
        why = "That's every unheard episode from the last 30 days."
    else:
        why = "Some episodes that would fit were kept as safe to skip."
    return f'<p class="unused">{left} {why}</p>'


def _pick(run_id: str, ep: dict, heard: bool, csrf: str, longer: bool = False) -> str:
    audio = ep.get("audio_url")
    player = (
        f'<audio controls preload="none" src="{esc(audio)}"></audio>'
        if audio and urlparse(audio).scheme in ("http", "https")
        else '<p class="meta">No playable audio in this feed.</p>'
    )
    heard_ctl = (
        '<span class="meta ok">Marked as heard</span>'
        if heard
        else f"""<form method="post" action="/v2/heard">{_csrf(csrf)}
      <input type="hidden" name="run" value="{esc(run_id)}">
      <input type="hidden" name="episode" value="{esc(ep['id'])}">
      <button class="secondary">Already heard</button></form>"""
    )
    needs = (f'<br><span class="needs">Needs {about(ep["total_min"])} in total '
             f'({esc(ep["extra_min"])} more than you have)</span>' if longer else "")
    return f"""<div class="card{'' if longer else ' pick'}">
  <strong>{esc(ep['title'])}</strong><br>
  <span class="meta">{esc(ep['show'])} &middot; {about(ep['duration_min'])} &middot; {esc(ep.get('released'))}</span>{needs}
  <details><summary class="meta">Play here</summary>{player}</details>
  <div class="links">
    <a href="{esc(spotify_link(ep))}" target="_blank" rel="noopener">{_icon("spotify")}Open in Spotify</a>
    {heard_ctl}
  </div>
  <details class="more"><summary class="meta">Other apps</summary><div class="links">
    {_apple_link(ep)}
    <a href="{esc(youtube_link(ep))}" target="_blank" rel="noopener">{_icon("youtube")}Search on YouTube</a>
    <a href="{esc(amazon_music_link(ep))}" target="_blank" rel="noopener">{_icon("amazonmusic")}Search on Amazon Music</a>
  </div></details>
</div>"""


def _skip(ep: dict) -> str:
    return (
        f'<div class="skip"><strong>{esc(ep["title"])}</strong> '
        f'<span class="meta">&mdash; {esc(ep["show"])} &middot; {about(ep["duration_min"])}</span></div>'
    )


def _note_box(run_id: str, note: str | None, csrf: str) -> str:
    """Under the "Thanks" line: the optional note, or the note already sent (escaped:
    it is untrusted user text)."""
    if note:
        return f'<p class="meta sent-note">Your note: &ldquo;{esc(note)}&rdquo;</p>'
    return f"""<form method="post" action="/v2/feedback/note" class="note-form">{_csrf(csrf)}
  <input type="hidden" name="run" value="{esc(run_id)}">
  <label class="field"><span>Anything you'd change? (optional)</span>
    <textarea name="note" rows="3" maxlength="{NOTE_MAX}"></textarea></label>
  <button class="secondary">Send</button>
</form>"""


def _at_speed(payload: dict) -> str:
    """Times on a run made at a faster speed are listening times: say so once."""
    speed = payload.get("speed") or 1.0
    return "" if speed == 1.0 else f" &middot; times at {esc(preferences.speed_label(speed))} speed"


def results(run: dict, heard: set, verdict: str | None, csrf: str = "", note: str | None = None) -> str:
    p = run["payload"]
    picks, skips, budget = p["picks"], p["skips"], run["budget"]
    used = sum(x["duration_min"] for x in picks)

    if picks:
        picks_html = "".join(_pick(run["id"], x, x["id"] in heard, csrf) for x in picks)
    elif not skips:
        failed = p.get("failed") or []
        if failed and len(failed) >= p["shows"]:
            picks_html = ""  # nothing loaded: failed_note above says so; never "inbox zero"
        elif failed:
            picks_html = "<p>No unheard episodes from the last 30 days in the shows that loaded.</p>"
        else:
            picks_html = (f"<p>No unheard episodes from the last 30 days in your shows. "
                          "Inbox zero &mdash; nice.</p>")
    else:
        shortest = min(x["duration_min"] for x in skips)
        picks_html = (f'<p class="warn">Nothing fits {esc(budget)} minutes. The shortest '
                      f"recent episode is <strong>{about(shortest)}</strong>.</p>")

    head, tail = skips[:SKIPS_SHOWN], skips[SKIPS_SHOWN:]
    skips_html = "".join(_skip(x) for x in head)
    if tail:
        skips_html += (f"<details><summary class=\"meta\">{len(tail)} more</summary>"
                       + "".join(_skip(x) for x in tail) + "</details>")

    if verdict:
        ask = (f'<p class="ok">Thanks &mdash; you said &ldquo;{esc(verdict)}&rdquo;.</p>'
               + _note_box(run["id"], note, csrf))
    else:
        ask = f"""<form method="post" action="/v2/feedback" class="row">{_csrf(csrf)}
  <input type="hidden" name="run" value="{esc(run['id'])}">
  <button name="verdict" value="yes">Yes</button>
  <button class="secondary" name="verdict" value="somewhat">Somewhat</button>
  <button class="secondary" name="verdict" value="no">No</button>
</form>"""

    # Only under real picks: with no picks the warning above already says why.
    unused = unused_note(picks, skips, budget) if picks else ""
    longer = p.get("longer") or []
    longer_html = ""
    if longer:
        longer_html = ('<h2>If you have a little longer</h2>'
                       '<p class="meta">Not picks: these fit if you have up to '
                       f'{engine.LONGER_EXTRA_MIN} more minutes.</p>'
                       + "".join(_pick(run["id"], x, x["id"] in heard, csrf, longer=True) for x in longer))

    return f"""<p class="meta">{_count(p['backlog_size'], "recent unheard episode")} across
{_count(p['shows'], "show")} &middot; about {esc(used)} of {esc(budget)} min used{_at_speed(p)}</p>
{failed_note(p.get('failed'), p['shows'])}
<h2>Worth it now</h2>
{'<p class="lead">Play these. Together they fit your time.</p>' if picks else ""}
{picks_html}
{unused}
<h2 id="feedback">Was this worth your time?</h2>
{"" if verdict else '<p class="lead">Would you actually play these? Your answer helps make the picks better.</p>'}
{ask}
{longer_html}
<h2>Safe to skip</h2>
{'<p class="lead">You can leave these for another day. Nothing is deleted.</p>' if skips else ""}
{skips_html or '<p class="meta">Nothing else in your recent backlog.</p>'}
<p style="margin-top:24px"><a class="btn" href="/v2/today">Pick again</a></p>"""


# --- privacy -----------------------------------------------------------------------------

def privacy() -> str:
    return """<h2>Privacy</h2>
<p>What Podsen stores about you, and nothing else:</p>
<ul>
  <li><strong>Your email address and profile picture link, from Google.</strong> You sign
      in with Google; we receive your email, an account id and the link to your Google
      profile picture (shown top right), never your Google password. The picture itself
      stays on Google and is loaded from there.</li>
  <li><strong>The shows you chose</strong> (name and public feed address).</li>
  <li><strong>Episodes you marked as heard</strong>, so they stop being suggested.</li>
  <li><strong>Your listening preferences</strong>, if you set them: listening speed, what
      you want from podcasts and what you usually skip.</li>
  <li><strong>Each set of picks we showed you and your feedback on it</strong>
      (yes / somewhat / no, any note you choose to add, the time you had, and which
      episodes were picked),
      to measure whether the picks are any good.</li>
</ul>
<p><strong>Where it is stored:</strong> with <a href="https://supabase.com" target="_blank"
rel="noopener">Supabase</a>, which runs our sign-in and database. Supabase keeps the
sign-in record (your email and Google account id); everything else is in our
database there.</p>
<p>Episode information comes from each show's public feed and is cached for an hour.
We do not connect to your streaming accounts.</p>
<p><strong>You can delete everything.</strong> On your <a href="/v2/account">Account</a>
page, &ldquo;Delete my account&rdquo; asks you to type DELETE and then immediately and
permanently removes everything above, including your sign-in record at Supabase.</p>
<p><strong>We never sell your data.</strong> It is not shared with advertisers or
anyone else.</p>
<p><a href="/v2/">Back</a></p>"""


# --- account -----------------------------------------------------------------------------

def account_page(email: str, error: str | None = None, csrf: str = "", prefs: dict | None = None) -> str:
    return f"""<h2>Account</h2>
<p>Signed in with Google as <strong>{esc(email)}</strong>. You stay signed in on this
browser for 30 days unless you log out.</p>
<form method="post" action="/v2/logout">{_csrf(csrf)}<button class="secondary">Log out</button></form>
<h2>Listening preferences</h2>
<p class="meta">{_prefs_summary(prefs)}</p>
<p><a class="btn secondary" href="/v2/preferences" style="background:#2a2c31;color:#e9e9ec">Set my preferences</a></p>
<h2>Delete my account</h2>
<p>This permanently deletes your account and all its data: your shows, heard episodes,
preferences, picks, feedback and your sign-in record. It cannot be undone.</p>
{_error(error)}
<form method="post" action="/v2/account/delete">{_csrf(csrf)}
  <label class="field"><span>Type DELETE to confirm</span>
    <input type="text" name="confirm" required autocomplete="off" spellcheck="false"></label>
  <button class="danger">Delete my account</button>
</form>"""


def account_deleted(login_error: str | None = None) -> str:
    """`login_error`: our data is gone but the sign-in record could not be removed."""
    if login_error:
        return f"""<h2>Account data deleted</h2>
<p>Your shows, heard episodes, picks and feedback have been deleted.</p>
<p class="warn">{esc(login_error)} Only your email and Google account id are left
there. Sign in again and delete once more to finish, or ask us to remove it.</p>
<p style="margin-top:20px"><a class="btn" href="/v2/">Back to Podsen</a></p>"""
    return """<h2>Account deleted</h2>
<p>Your account and all its data have been deleted, including your sign-in record.</p>
<p style="margin-top:20px"><a class="btn" href="/v2/">Back to Podsen</a></p>"""
