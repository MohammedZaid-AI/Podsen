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

from v2 import app_views, auth, backlog, db, engine, itunes, links, refresh, trending

bp = Blueprint("v2", __name__, url_prefix="/v2")

VERDICTS = ("yes", "somewhat", "no")
RUN_FIELDS = (
    "id", "title", "show", "duration_min", "released", "audio_url", "apple_url", "apple_episode_url",
)


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
    return app_views.page(title, body, logged_in=logged_in, csrf=_csrf_token()), status


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

    db.upsert_user(user["id"], user["email"])
    _log_in(user["id"])
    return redirect("/v2/today")


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
    return _page("Podsen · account", app_views.account_page(user["email"], csrf=_csrf_token()), logged_in=True)


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
    trend = None
    if n < app_views.MIN_SHOWS:  # only a new user needs suggestions here
        trend = trending.get(request.args.get("country") or request.form.get("country"))
    body = app_views.today_form(n, error, _csrf_token(), trending=trend,
                                have={s["itunes_id"] for s in shows_list}, note=note)
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

    bl = backlog.build(user["id"])
    picks, skips = engine.pick(bl["episodes"], minutes)
    longer = engine.a_little_longer(picks, skips, minutes)
    # Picks and "a little longer" options get play links, so only they need Apple's lookup.
    picks = links.add_apple_episode_urls(picks)
    longer = links.add_apple_episode_urls(longer)
    slim = lambda eps: [{k: e.get(k) for k in RUN_FIELDS} for e in eps]  # noqa: E731
    run_id = db.save_run(
        user["id"],
        minutes,
        engine.ENGINE_NAME,
        {
            "picks": slim(picks),
            "skips": slim(skips),
            "longer": [{**slim([e])[0], "total_min": e["total_min"], "extra_min": e["extra_min"]}
                       for e in longer],
            "failed": bl["failed"],
            "backlog_size": len(bl["episodes"]),
            "shows": bl["shows"],
        },
    )
    return redirect(f"/v2/run/{run_id}")


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
    body = app_views.results(r, db.heard_ids(user["id"]), db.latest_feedback(r["id"]), _csrf_token())
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
    return redirect(f"/v2/run/{r['id']}")


@bp.post("/feedback")
@login_required
def feedback(user):
    r = _run_or_404(user, request.form.get("run"))
    verdict = request.form.get("verdict")
    if r is None:
        return redirect("/v2/today")
    if verdict in VERDICTS:
        db.save_feedback(r, verdict)
    return redirect(f"/v2/run/{r['id']}")
