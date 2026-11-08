"""The V2 product flow under /v2: Google sign-in (Supabase Auth), shows from Apple
search, picks from public RSS. No Spotify data anywhere in this flow.

Our Flask session keys: "v2_user" (the Supabase user id), "v2_csrf" (the form token)
and "v2_pkce" (the PKCE code_verifier, only between /login and /auth/callback).
"""

import hmac
import re
import secrets
from functools import wraps

from flask import Blueprint, current_app, redirect, request, session

from v2 import app_views, auth, backlog, db, engine, importer, itunes, links, preferences, refresh, timing, trending

bp = Blueprint("v2", __name__, url_prefix="/v2")

VERDICTS = ("yes", "somewhat", "no")
RUN_FIELDS = (
    "id", "title", "show", "duration_min", "released", "audio_url", "apple_url", "apple_episode_url",
    "raw_min",  # the episode's own length; duration_min is the time to hear it at the chosen speed
    "itunes_id",  # lets a replacement pick get its exact Apple link and keeps one episode per show
)


def _slim(eps: list[dict]) -> list[dict]:
    return [{k: e.get(k) for k in RUN_FIELDS} for e in eps]


def _longer_options(picks: list[dict], skips: list[dict], budget: int) -> list[dict]:
    """"If you have a little longer": skipped episodes that fit with a few more minutes."""
    longer = links.add_apple_episode_urls(engine.a_little_longer(picks, skips, budget))
    return [{**_slim([e])[0], "total_min": e["total_min"], "extra_min": e["extra_min"]} for e in longer]


# --- session, CSRF ---------------------------------------------------------------------

def _csrf_token() -> str:
    token = session.get("v2_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["v2_csrf"] = token
    return token


@bp.before_request
def _check_csrf():
    """Every POST under /v2 must carry the token from this session's forms."""
    if request.method != "POST":
        return None
    sent = request.form.get("csrf_token") or ""
    expected = session.get("v2_csrf") or ""
    if expected and hmac.compare_digest(sent.encode(), expected.encode()):
        return None
    body = ('<p class="warn">This form expired or did not come from Podsen. '
            'Go back, reload the page and try again.</p>'
            '<p style="margin-top:20px"><a class="btn" href="/v2/">Back to Podsen</a></p>')
    return app_views.page("Podsen", body), 400


def _log_in(user_id: str) -> None:
    session["v2_user"] = user_id
    session.permanent = True  # 30 days: PERMANENT_SESSION_LIFETIME in app.py
    session["v2_csrf"] = secrets.token_urlsafe(32)  # fresh token for the new login


def _log_out() -> None:
    session.pop("v2_user", None)
    session.pop("v2_csrf", None)
    session.pop("v2_pkce", None)
    session.permanent = False


def _current_user(run_id=None, trending_country=None, with_caches=False):
    uid = session.get("v2_user")
    return db.get_user(uid, run_id, trending_country, with_caches) if uid else None


# Pages that show a trending chart: their first query brings the cached chart along.
_TRENDING_PAGES = {"v2.shows", "v2.today", "v2.shows_add_many"}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        # Hints so the one user query also fetches what this page needs next.
        run_hint = kwargs.get("run_id") or request.form.get("run")
        if not re.fullmatch(r"[0-9a-f]{32}", run_hint or ""):
            run_hint = None
        trend_hint = (trending.country(request.args.get("country") or request.form.get("country"))
                      if request.endpoint in _TRENDING_PAGES else None)
        # "Find picks" needs every show's cached feed and Apple list: same query too.
        user = _current_user(run_hint, trend_hint, request.endpoint == "v2.today_run")
        if user is None:
            session.pop("v2_user", None)
            return redirect("/v2/")
        return view(user, *args, **kwargs)

    return wrapper


def _page(title, body, status=200, logged_in=False):
    # Memoised per request: the user came with the page's one query, so this is free.
    me = _current_user() if logged_in else None
    return app_views.page(title, body, logged_in=logged_in, csrf=_csrf_token(),
                          active=request.path, me=me), status


def _callback_url() -> str:
    # From the request host (ProxyFix applies X-Forwarded-Host behind a tunnel), so the
    # same code works on 127.0.0.1 and when hosted. Supabase only redirects to URLs on
    # its allow-list, so a forged Host header can't send the code anywhere else.
    return f"{request.url_root.rstrip('/')}/v2/auth/callback"


# --- landing, Google sign-in -------------------------------------------------------------

@bp.get("")
@bp.get("/")
def landing():
    if _current_user():
        return redirect("/v2/today")
    return app_views.page("Podsen", app_views.landing(), hero=True)


@bp.get("/login")
def login():
    if _current_user():
        return redirect("/v2/today")
    verifier = auth.new_verifier()
    try:
        url = auth.authorize_url(_callback_url(), verifier)
    except auth.AuthError as e:
        return _page("Podsen · sign in", app_views.login_error(str(e)), 503)
    session["v2_pkce"] = verifier
    return redirect(url)


@bp.get("/auth/callback")
def auth_callback():
    # The verifier is single use, whatever happens next.
    verifier = session.pop("v2_pkce", None)

    error = request.args.get("error")
    if error:
        if error == "access_denied":
            msg = "You cancelled signing in with Google. Nothing was changed."
        else:
            detail = request.args.get("error_description") or error
            msg = f"Google sign-in failed: {detail[:200]}"
        return _page("Podsen · sign in", app_views.login_error(msg), 400)

    code = request.args.get("code")
    if not code:
        return _page("Podsen · sign in",
                     app_views.login_error("No sign-in code came back. Please start again."), 400)
    if not verifier:
        return _page("Podsen · sign in", app_views.login_error(
            "This sign-in was started in another browser or took too long. Please start again."), 400)

    try:
        user = auth.exchange_code(code, verifier)
    except auth.AuthError as e:
        status = 503 if e.kind in ("unreachable", "not_configured") else 400
        current_app.logger.warning("sign-in failed: %s", e.kind)  # the kind only, never a key
        return _page("Podsen · sign in", app_views.login_error(str(e)), status)

    db.upsert_user(user["id"], user["email"], user.get("avatar_url"))
    _log_in(user["id"])
    # Land on My shows: the shows you chose are listed first, then finding more.
    return redirect("/v2/shows")


@bp.post("/logout")
def logout():
    _log_out()
    return redirect("/v2/")


@bp.get("/privacy")
def privacy():
    return _page("Podsen · privacy", app_views.privacy(), logged_in=_current_user() is not None)


# --- account -------------------------------------------------------------------------------

@bp.get("/account")
@login_required
def account(user):
    body = app_views.account_page(user["email"], csrf=_csrf_token(), prefs=db.get_preferences(user["id"]))
    return _page("Podsen · account", body, logged_in=True)


@bp.post("/account/delete")
@login_required
def account_delete(user):
    if (request.form.get("confirm") or "").strip() != "DELETE":
        body = app_views.account_page(
            user["email"], "Type DELETE in capitals to confirm. Nothing was deleted.", _csrf_token()
        )
        return _page("Podsen · account", body, 400, logged_in=True)

    db.delete_user(user["id"])
    _log_out()
    try:
        auth.delete_auth_user(user["id"])
    except auth.AuthError as e:
        # Our data is already gone; say plainly that the sign-in record is not.
        current_app.logger.warning("auth user delete failed: %s", e.kind)
        return _page("Podsen", app_views.account_deleted(str(e)), 502)
    return _page("Podsen", app_views.account_deleted())


# --- shows -------------------------------------------------------------------------------

MAX_ADD = 25  # one trending chart's worth per request


def _note_from_args():
    def num(k):
        v = request.args.get(k, "")
        return min(int(v), 99) if v.isdigit() else 0
    return app_views.added_note(num("added"), num("skipped"))


def _shows_page(user, query="", results=None, error=None, status=200, note=None):
    body = app_views.shows_page(
        db.list_shows(user["id"]), query, results, error, _csrf_token(),
        trending=trending.get(request.args.get("country") or request.form.get("country")),
        note=note,
    )
    return _page("Podsen · shows", body, status, logged_in=True)


def _today_page(user, error=None, status=200, note=None):
    shows_list = db.list_shows(user["id"])
    n = len(shows_list)
    if n >= app_views.MIN_SHOWS and db.get_preferences(user["id"]) is None:
        # Once, before the first triage: three optional questions, with "Skip for now".
        body = app_views.added_line(note) + app_views.preferences_form(
            preferences.DEFAULTS, _csrf_token(), first_time=True)
        return _page("Podsen · preferences", body, status, logged_in=True)
    trend = None
    if n < app_views.MIN_SHOWS:  # only a new user needs suggestions here
        trend = trending.get(request.args.get("country") or request.form.get("country"))
    focus = preferences.focus_of(db.get_preferences(user["id"]))
    body = app_views.today_form(n, error, _csrf_token(), trending=trend,
                                have={s["itunes_id"] for s in shows_list}, note=note, focus=focus)
    return _page("Podsen · today", body, status, logged_in=True)


def _add_shows(user, ids) -> tuple[int, int]:
    """The one add path: the form sends only iTunes ids; every feed URL comes from
    Apple's lookup, never from the form. Returns (added, skipped_no_feed)."""
    found = itunes.lookup_many(ids)
    added, skipped, new = 0, 0, []
    for itunes_id in dict.fromkeys(ids):
        show = found.get(itunes_id)
        if show and show["feed_url"]:
            db.add_show(user["id"], show)
            new.append(show)
            added += 1
        else:
            skipped += 1
    _warm_caches(new)
    return added, skipped


def _warm_caches(new_shows: list[dict]) -> None:
    """Fetch a new show's feed and Apple episode list in the background, so the first
    "Find picks" usually finds them cached instead of waiting on the network."""
    if not new_shows:
        return
    db.prefetch_caches([s["feed_url"] for s in new_shows], [s["itunes_id"] for s in new_shows])
    for s in new_shows:
        if db.cache_entry("feed", s["feed_url"]) is None:
            refresh.schedule(("feed", s["feed_url"]), lambda u=s["feed_url"]: backlog.refresh_feed(u))
        if db.cache_entry("apple", s["itunes_id"]) is None:
            refresh.schedule(("apple", s["itunes_id"]), lambda i=s["itunes_id"]: links.refresh_apple(i))


@bp.get("/shows")
@login_required
def shows(user):
    query = (request.args.get("q") or "").strip()[:100]
    results, error = None, None
    if query:
        try:
            results = itunes.search(query)
        except Exception as e:  # say the search failed rather than showing "no results"
            error = f"Search failed ({type(e).__name__}). Try again in a moment."
    return _shows_page(user, query, results, error, note=_note_from_args())


@bp.post("/shows/add")
@login_required
def shows_add(user):
    itunes_id = (request.form.get("itunes_id") or "").strip()
    try:
        added, _ = _add_shows(user, [itunes_id])
    except Exception as e:
        return _shows_page(user, error=f"Could not add that show ({type(e).__name__}).", status=502)
    if not added:
        return _shows_page(user, error="No public feed, can not be analyzed.", status=400)
    return redirect("/v2/shows")


@bp.post("/shows/add-many")
@login_required
def shows_add_many(user):
    """"Add selected" from a trending list: several shows in one normal form POST."""
    back = "today" if request.form.get("back") == "today" else "shows"
    code = trending.country(request.form.get("country"))
    ids = [i.strip() for i in request.form.getlist("itunes_id") if i.strip().isdigit()][:MAX_ADD]
    render = _today_page if back == "today" else _shows_page
    if not ids:
        return render(user, error="Tick at least one show to add.", status=400)
    try:
        added, skipped = _add_shows(user, ids)
    except Exception as e:
        return render(user, error=f"Could not add those shows ({type(e).__name__}). Try again.", status=502)

    if len(db.list_shows(user["id"])) >= app_views.MIN_SHOWS:
        return redirect(f"/v2/today?added={added}&skipped={skipped}")
    return redirect(f"/v2/{back}?added={added}&skipped={skipped}&country={code}")


@bp.post("/shows/import")
@login_required
def shows_import(user):
    """Bring shows over in one step: an OPML file from another podcast app, or pasted names.
    Each is looked up by name on Apple and shown with a checkbox; "Add selected" then posts
    only iTunes ids to the usual add path. A URL in the file is never fetched."""
    upload = request.files.get("opml")
    try:
        if upload and upload.filename:
            items = importer.parse_opml(upload.read(importer.MAX_FILE_BYTES + 1))
        else:
            items = importer.parse_names((request.form.get("names") or "")[:20000])
    except importer.ImportError_ as e:
        return _shows_page(user, error=str(e), status=400)
    if not items:
        return _shows_page(user, error="Choose an OPML file or paste at least one show name.", status=400)
    with timing.measure("apple", n=min(len(items), importer.MAX_IMPORT)):
        found, left = importer.match(items)
    have = {s["itunes_id"] for s in db.list_shows(user["id"])}
    body = app_views.import_results(found, left, have, _csrf_token())
    return _page("Podsen · import shows", body, logged_in=True)


@bp.post("/shows/remove")
@login_required
def shows_remove(user):
    db.remove_show(user["id"], (request.form.get("itunes_id") or "").strip())
    return redirect("/v2/shows")


# --- today -> run --------------------------------------------------------------------------

@bp.get("/today")
@login_required
def today(user):
    return _today_page(user, note=_note_from_args())


def _minutes() -> int | None:
    raw = request.form.get("minutes", "")
    if raw == "custom":
        raw = request.form.get("custom_minutes", "")
    try:
        m = int(raw)
    except ValueError:
        return None
    return m if 1 <= m <= 600 else None


@bp.post("/today")
@login_required
def today_run(user):
    if len(db.list_shows(user["id"])) < app_views.MIN_SHOWS:
        return _today_page(user, status=400)
    minutes = _minutes()
    if minutes is None:
        return _today_page(user, error="Time must be a whole number of minutes from 1 to 600.", status=400)

    prefs = db.get_preferences(user["id"]) or preferences.DEFAULTS
    speed = prefs["listening_speed"]
    focus = preferences.focus_of(prefs)
    days = preferences.FOCUS_DAYS[focus]
    bl = backlog.build(user["id"], days=days)
    # Speed changes the time math only: the rule picker gets each episode's listening time
    # at that speed (rounded up), so its budget check stays a hard cap. "My backlog" changes
    # the window (180 days) and the fill order (oldest first), nothing else.
    oldest = focus == "backlog"
    picks, skips = engine.pick(preferences.at_speed(bl["episodes"], speed), minutes, oldest=oldest)
    run_id = _save_triage(user, minutes, picks, skips, engine.engine_name(oldest), prefs, {
        "failed": bl["failed"],
        "backlog_size": len(bl["episodes"]),
        "shows": bl["shows"],
        "window_days": days,  # how far back this run looked (the results page says so)
        "older_loading": bl["older_loading"],  # shows whose older episodes were still loading
    })
    return redirect(f"/v2/run/{run_id}")


def _save_triage(user, minutes, picks, skips, name: str, prefs: dict, extra: dict) -> str:
    """Store one set of picks as a run (the same for Today and for "Pick from my saved").
    Picks and "a little longer" options get play links, so only they need Apple's lookup."""
    longer = links.add_apple_episode_urls(engine.a_little_longer(picks, skips, minutes))
    picks = links.add_apple_episode_urls(picks)
    return db.save_run(user["id"], minutes, name, {
        "picks": _slim(picks),
        "skips": _slim(skips),
        "longer": [{**_slim([e])[0], "total_min": e["total_min"], "extra_min": e["extra_min"]}
                   for e in longer],
        "speed": prefs["listening_speed"],
        "preferences": prefs,  # kept with the run, for a future picker and evaluation
        **extra,
    })


@bp.get("/preferences")
@login_required
def preferences_page(user):
    """"Set my preferences" from Account: the same questions, filled in with the saved
    answers. Only ever the signed-in user's own row."""
    prefs = db.get_preferences(user["id"]) or preferences.DEFAULTS
    return _page("Podsen · preferences", app_views.preferences_form(prefs, _csrf_token(), first_time=False),
                 logged_in=True)


@bp.post("/preferences")
@login_required
def preferences_save(user):
    """Save (or "Skip for now": the defaults) for the signed-in user only. Saving any row
    means the questions are not asked again; Account has "Set my preferences" to change it.
    No user id is read from the form."""
    if request.form.get("action") == "skip":
        prefs = preferences.clean(None, [], [])  # the defaults, as fresh lists
    else:
        prefs = preferences.clean(request.form.get("speed"), request.form.getlist("wants"),
                                  request.form.getlist("skips"), request.form.get("focus"))
    db.save_preferences(user["id"], prefs)
    return redirect("/v2/today")


def _run_or_404(user, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
        return None
    return db.get_run(run_id, user["id"])


@bp.get("/run/<run_id>")
@login_required
def run(user, run_id):
    r = _run_or_404(user, run_id)
    if r is None:
        return _page("Podsen", '<p>That run was not found. <a href="/v2/today">Start over</a></p>', 404, True)
    body = app_views.results(r, db.heard_ids(user["id"]), db.latest_feedback(r["id"]), _csrf_token(),
                             note=db.latest_note(r["id"]), saved=db.saved_ids(user["id"]))
    return _page("Podsen · today", body, logged_in=True)


@bp.post("/heard")
@login_required
def heard(user):
    r = _run_or_404(user, request.form.get("run"))
    episode = request.form.get("episode") or ""
    if r is None or not episode:
        return redirect("/v2/today")
    # Only episodes we actually showed this user can be marked, not arbitrary ids.
    shown = {e["id"] for e in r["payload"]["picks"] + r["payload"]["skips"]}
    if episode in shown:
        db.mark_heard(user["id"], episode)
        _after_heard(user, r, episode)
    return redirect(f"/v2/run/{r['id']}")


def _after_heard(user, r, episode: str) -> None:
    """A pick marked heard is replaced: the other picks stay, and the time it freed is
    refilled from the safe-to-skip list with the same rules (one per show, never over the
    time). If nothing fits, the page says so and offers "a little longer" with a button."""
    p = r["payload"]
    heard_now = db.heard_ids(user["id"])
    gone = next((x for x in p["picks"] if x["id"] == episode), None)
    kept = [x for x in p["picks"] if x["id"] != episode]
    pool = [x for x in p["skips"] if x["id"] not in heard_now]  # heard ones leave the skips too
    added = []
    if gone is not None:
        oldest = engine.is_oldest_first(r["engine"])
        added = _slim(links.add_apple_episode_urls(engine.refill(kept, pool, r["budget"], oldest)))
        taken = {x["id"] for x in added}
        pool = [x for x in pool if x["id"] not in taken]
        p["swaps"] = (p.get("swaps") or [])[-4:] + [
            {"heard": gone["title"], "freed": gone["duration_min"], "added": [x["title"] for x in added]}]
    p["picks"], p["skips"] = kept + added, pool
    p["longer"] = _longer_options(p["picks"], p["skips"], r["budget"])
    db.update_run(user["id"], r["id"], r["budget"], p)


@bp.post("/run/extend")
@login_required
def run_extend(user):
    """"Add N min and hear this": raise this run's time to fit one of the "a little longer"
    episodes. Only on the listener's tap, and only an episode this run offered, so the time
    never grows by itself. The run keeps what was first asked in `asked_min`."""
    r = _run_or_404(user, request.form.get("run"))
    if r is None:
        return redirect("/v2/today")
    p, episode = r["payload"], request.form.get("episode") or ""
    offered = {e["id"] for e in p.get("longer") or []}
    ep = next((e for e in p["skips"] if e["id"] == episode and episode in offered), None)
    if ep is None:
        return redirect(f"/v2/run/{r['id']}")
    new_budget = sum(x["duration_min"] for x in p["picks"]) + ep["duration_min"]
    if not 0 < new_budget - r["budget"] <= engine.LONGER_EXTRA_MIN:  # recomputed, never trusted
        return redirect(f"/v2/run/{r['id']}")
    p["asked_min"] = p.get("asked_min") or r["budget"]
    p["picks"] = p["picks"] + [ep]
    p["skips"] = [x for x in p["skips"] if x["id"] != episode]
    p["longer"] = _longer_options(p["picks"], p["skips"], new_budget)
    db.update_run(user["id"], r["id"], new_budget, p)
    return redirect(f"/v2/run/{r['id']}")


# --- saved for later ---------------------------------------------------------------------

@bp.get("/saved")
@login_required
def saved_page(user):
    body = app_views.saved_page(db.list_saved(user["id"]), _csrf_token(), full=bool(request.args.get("full")))
    return _page("Podsen · saved", body, logged_in=True)


@bp.post("/saved/add")
@login_required
def saved_add(user):
    """"Save for later" on a card. Only an episode this user's own run showed, and the copy
    is taken from the run, never from the form, so a form can't save arbitrary text."""
    r = _run_or_404(user, request.form.get("run"))
    if r is None:
        return redirect("/v2/today")
    p, episode = r["payload"], request.form.get("episode") or ""
    ep = next((e for e in p["picks"] + p["skips"] + (p.get("longer") or []) if e["id"] == episode), None)
    if ep is not None and not db.save_episode(user["id"], ep):
        return redirect("/v2/saved?full=1")
    return redirect(f"/v2/run/{r['id']}")


@bp.post("/saved/remove")
@login_required
def saved_remove(user):
    db.unsave_episode(user["id"], request.form.get("episode") or "")  # owner's row only
    return redirect("/v2/saved")


@bp.post("/saved/heard")
@login_required
def saved_heard(user):
    """"Already heard" on the Saved page: only an episode that is on this user's list. Marking
    it heard also takes it off the list (db.mark_heard)."""
    episode = request.form.get("episode") or ""
    if episode in db.saved_ids(user["id"]):
        db.mark_heard(user["id"], episode)
    return redirect("/v2/saved")


@bp.post("/saved/pick")
@login_required
def saved_pick(user):
    """"Pick from my saved": the same time rules, but only from the saved list, oldest first.
    Speed counts as it does everywhere; the time stays a hard cap."""
    pool = db.list_saved(user["id"])
    if not pool:
        return redirect("/v2/saved")
    minutes = _minutes()
    if minutes is None:
        body = app_views.saved_page(pool, _csrf_token(),
                                    error="Time must be a whole number of minutes from 1 to 600.")
        return _page("Podsen · saved", body, 400, logged_in=True)
    prefs = db.get_preferences(user["id"]) or preferences.DEFAULTS
    picks, skips = engine.pick(preferences.at_speed(pool, prefs["listening_speed"]), minutes, oldest=True)
    run_id = _save_triage(user, minutes, picks, skips, engine.SAVED_ENGINE_NAME, prefs, {
        "source": "saved",  # the results page words itself for the saved list, not for a window
        "failed": [],
        "backlog_size": len(pool),
        "shows": len({e["itunes_id"] or e["show"] for e in pool}),
    })
    return redirect(f"/v2/run/{run_id}")


@bp.post("/feedback")
@login_required
def feedback(user):
    r = _run_or_404(user, request.form.get("run"))
    verdict = request.form.get("verdict")
    if r is None:
        return redirect("/v2/today")
    if verdict in VERDICTS:
        db.save_feedback(r, verdict)
    return redirect(f"/v2/run/{r['id']}#feedback")  # back to the question, not the top


@bp.post("/feedback/note")
@login_required
def feedback_note(user):
    """The optional "Anything you'd change?" text, sent after the verdict. Same checks as
    /feedback: signed in, CSRF (before_request), and only the user's own run. It attaches
    to that run's latest verdict; with no verdict yet, or an empty box, nothing is saved."""
    r = _run_or_404(user, request.form.get("run"))
    if r is None:
        return redirect("/v2/today")
    db.save_note(r, request.form.get("note") or "")
    return redirect(f"/v2/run/{r['id']}#feedback")
