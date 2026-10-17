"""HTML for the V2 app under /v2. Strings only, no client JS, reusing views.page().

Every interpolated value goes through esc(): titles, descriptions, show names and
URLs all come from public feeds or Apple's API and are untrusted.
"""

from urllib.parse import quote, quote_plus, urlparse

from v2 import engine
from views import esc
from views import page as base_page

# Link-outs only: a search URL built from public RSS text. No data comes back from it.
SPOTIFY_SEARCH = "https://open.spotify.com/search/"
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="
AMAZON_MUSIC_SEARCH = "https://music.amazon.com/search/"
MIN_SHOWS = 3
SKIPS_SHOWN = 10

_STYLE = """<style>
  /* Phone-first: every control is at least 44px tall, nothing is wider than 375px. */
  .nav { display: flex; gap: 4px 16px; align-items: center; flex-wrap: wrap; margin: -12px 0 18px; font-size: 15px; }
  .nav a, .foot a, .countries a, .countries strong { display: inline-flex; align-items: center; min-height: 44px; }
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
  .landing-cta { margin: 26px 0 14px; }
  .landing-cta .btn { padding: 12px 26px; font-size: 17px; }
  @media (max-width: 480px) {
    .show img { width: 48px; height: 48px; }
    .standfirst { font-size: 19px; }
  }
</style>"""


def _csrf(token: str) -> str:
    """Hidden CSRF field. Every V2 POST form includes it; web.py rejects POSTs without it."""
    return f'<input type="hidden" name="csrf_token" value="{esc(token)}">'


def page(title: str, body: str, logged_in: bool = False, hero: bool = False, csrf: str = "") -> str:
    if logged_in:
        nav = f"""<nav class="nav"><a href="/v2/today">Today</a><a href="/v2/shows">My shows</a>
  <a href="/v2/account">Account</a>
  <form method="post" action="/v2/logout">{_csrf(csrf)}<button class="linkbtn">Log out</button></form></nav>"""
    else:
        nav = ""
    foot = '<footer class="foot"><a href="/v2/privacy">Privacy</a> &middot; Podsen</footer>'
    return base_page(title, f"{_STYLE}{nav}{body}{foot}", hero=hero)


def _error(msg):
    return f'<p class="err">{esc(msg)}</p>' if msg else ""


# --- landing + auth --------------------------------------------------------------------

GOOGLE_BUTTON = '<a class="btn" href="/v2/login">Continue with Google</a>'


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
        need = (f'<p class="warn">Add at least {MIN_SHOWS} shows to get picks '
                f"&mdash; you have {n}.</p>")
    else:
        need = '<p class="meta">Ready. <a href="/v2/today">Pick for today &rarr;</a></p>'

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
        for s in my_shows
    ) or '<p class="meta">No shows yet. Search above to add some.</p>'

    trend = trending_section(trending, have, csrf, "shows", "Trending") if trending else ""
    return f"""{_note(note)}
<h2>Find your shows</h2>
<form method="get" action="/v2/shows" class="row" style="flex-wrap:nowrap">
  <input type="search" name="q" value="{esc(query)}" placeholder="Search podcasts" required maxlength="100">
  <button>Search</button>
</form>
{_error(error)}
{found}
{trend}
<h2>My shows ({n})</h2>
{need}
{mine}"""


# --- today -------------------------------------------------------------------------------

def today_form(n_shows: int, error: str | None = None, csrf: str = "",
               trending: dict | None = None, have: set | None = None, note: str | None = None) -> str:
    if n_shows < MIN_SHOWS:
        trend = (trending_section(trending, have or set(), csrf, "today",
                                  f"Trending in {trending['label']}") if trending else "")
        return f"""{_note(note)}{_error(error)}
<p class="meta">Pick at least {MIN_SHOWS} shows you follow to get started &mdash; you have
{esc(n_shows)}. Tick them below, or search for yours.</p>
{trend}
<p style="margin-top:20px"><a class="btn secondary" href="/v2/shows"
  style="background:#2a2c31;color:#e9e9ec">Add shows</a></p>"""
    return f"""{_note(note)}{_error(error)}
<form method="post" action="/v2/today">{_csrf(csrf)}
  <h2>How much time do you have right now?</h2>
  <div class="row">
    <button name="minutes" value="15">15 min</button>
    <button name="minutes" value="30">30 min</button>
    <button name="minutes" value="60">60 min</button>
  </div>
  <div class="row" style="margin-top:12px">
    <input type="number" name="custom_minutes" min="1" max="600" value="45">
    <button class="secondary" name="minutes" value="custom">Go</button>
  </div>
</form>
<p class="meta" style="margin-top:20px">Looks at episodes from the last 30 days of your
{esc(n_shows)} shows. First run can take a few seconds while feeds load.</p>"""


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
        return f'<a href="{esc(exact)}" target="_blank" rel="noopener">Open in Apple Podcasts</a>'
    if show and urlparse(show).scheme == "https":
        return f'<a href="{esc(show)}" target="_blank" rel="noopener">Search on Apple Podcasts</a>'
    return ""


def failed_note(failed) -> str:
    if not failed:
        return ""
    n = len(failed)
    items = "; ".join(f"{f['show']} ({f['error']})" for f in failed)
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
    <a href="{esc(spotify_link(ep))}" target="_blank" rel="noopener">Open in Spotify</a>
    {_apple_link(ep)}
    <a href="{esc(youtube_link(ep))}" target="_blank" rel="noopener">Search on YouTube</a>
    <a href="{esc(amazon_music_link(ep))}" target="_blank" rel="noopener">Search on Amazon Music</a>
    {heard_ctl}
  </div>
</div>"""


def _skip(ep: dict) -> str:
    return (
        f'<div class="skip"><strong>{esc(ep["title"])}</strong> '
        f'<span class="meta">&mdash; {esc(ep["show"])} &middot; {about(ep["duration_min"])}</span></div>'
    )


def results(run: dict, heard: set, verdict: str | None, csrf: str = "") -> str:
    p = run["payload"]
    picks, skips, budget = p["picks"], p["skips"], run["budget"]
    used = sum(x["duration_min"] for x in picks)

    if picks:
        picks_html = "".join(_pick(run["id"], x, x["id"] in heard, csrf) for x in picks)
    elif not skips:
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
        ask = f'<p class="ok">Thanks &mdash; you said &ldquo;{esc(verdict)}&rdquo;.</p>'
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

    return f"""<p class="meta">{esc(p['backlog_size'])} recent unheard episodes across
{esc(p['shows'])} shows &middot; about {esc(used)} of {esc(budget)} min used</p>
{failed_note(p.get('failed'))}
<h2>Worth it now</h2>
{picks_html}
{unused}
{longer_html}
<h2>Safe to skip</h2>
{skips_html or '<p class="meta">Nothing else in your recent backlog.</p>'}
<h2>Was this worth your time?</h2>
{ask}
<p style="margin-top:24px"><a class="btn" href="/v2/today">Pick again</a></p>"""


# --- privacy -----------------------------------------------------------------------------

def privacy() -> str:
    return """<h2>Privacy</h2>
<p>What Podsen stores about you, and nothing else:</p>
<ul>
  <li><strong>Your email address, from Google.</strong> You sign in with Google; we
      receive your email and an account id, never your Google password.</li>
  <li><strong>The shows you chose</strong> (name and public feed address).</li>
  <li><strong>Episodes you marked as heard</strong>, so they stop being suggested.</li>
  <li><strong>Each set of picks we showed you and your feedback on it</strong>
      (yes / somewhat / no, the time you had, and which episodes were picked),
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

def account_page(email: str, error: str | None = None, csrf: str = "") -> str:
    return f"""<h2>Account</h2>
<p>Signed in with Google as <strong>{esc(email)}</strong>. You stay signed in on this
browser for 30 days unless you log out.</p>
<form method="post" action="/v2/logout">{_csrf(csrf)}<button class="secondary">Log out</button></form>
<h2>Delete my account</h2>
<p>This permanently deletes your account and all its data: your shows, heard episodes,
picks, feedback and your sign-in record. It cannot be undone.</p>
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
