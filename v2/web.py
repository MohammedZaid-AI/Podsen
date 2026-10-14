"""The V2 product flow under /v2: own accounts, shows from Apple search, picks from
public RSS. No streaming-service login and no Spotify data anywhere in this flow.

Our Flask session keys: "v2_user" (the user's id) and "v2_csrf" (the form token).
V1's keys in the same cookie are never read or written here.
"""

import hmac
import re
import secrets
import time
from functools import wraps

from flask import Blueprint, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from v2 import app_views, backlog, db, engine, itunes, links

bp = Blueprint("v2", __name__, url_prefix="/v2")

# Failed logins, counted in a 15-minute window. Per email AND IP, so guessing at
# someone's account from one machine never locks them out on theirs; and per IP
# overall, so one machine can't spray guesses across many accounts.
MAX_FAILURES = 5
MAX_IP_FAILURES = 20
FAILURE_WINDOW_S = 15 * 60
VERDICTS = ("yes", "somewhat", "no")
RUN_FIELDS = (
    "id", "title", "show", "duration_min", "released", "audio_url", "apple_url", "apple_episode_url",
)
# Compared against when the email is unknown, so a miss costs the same as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


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


def _log_in(user_id: int) -> None:
    session["v2_user"] = user_id
    session.permanent = True  # 30 days: PERMANENT_SESSION_LIFETIME in app.py
    session["v2_csrf"] = secrets.token_urlsafe(32)  # fresh token for the new login


def _log_out() -> None:
    session.pop("v2_user", None)
    session.pop("v2_csrf", None)
    session.permanent = False


def _current_user():
    uid = session.get("v2_user")
    return db.get_user(uid) if uid else None


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if user is None:
            session.pop("v2_user", None)
            return redirect("/v2/login")
        return view(user, *args, **kwargs)

    return wrapper


def _page(title, body, status=200, logged_in=False):
    return app_views.page(title, body, logged_in=logged_in, csrf=_csrf_token()), status


def _norm_email(raw) -> str:
    return (raw or "").strip().lower()[:254]


def _ip() -> str:
    # ProxyFix in app.py has already replaced this with the client behind a tunnel.
    return request.remote_addr or ""


def _locked_out(email: str, ip: str) -> int | None:
    """Minutes until this email+IP may try again, or None if it may try now."""
    pair, by_ip = db.recent_failures(email, ip, FAILURE_WINDOW_S)
    # For each limit hit, the failure whose ageing-out brings the count under the cap.
    blocking = [lst[len(lst) - cap] for lst, cap in ((pair, MAX_FAILURES), (by_ip, MAX_IP_FAILURES))
                if len(lst) >= cap]
    if not blocking:
        return None
    return max(1, round((max(blocking) + FAILURE_WINDOW_S - time.time()) / 60))


def _password_ok(email: str, password: str):
    """(user or None, locked_minutes or None). Records a failure on a wrong password."""
    ip = _ip()
    wait = _locked_out(email, ip)
    if wait:
        return None, wait
    user = db.get_user_by_email(email)
    ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not (user and ok):
        db.record_failure(email, ip)
        return None, None
    db.clear_failures(email, ip)
    return user, None


# --- landing, auth -----------------------------------------------------------------------

@bp.get("")
@bp.get("/")
def landing():
    if _current_user():
        return redirect("/v2/today")
    return app_views.page("Podsen", app_views.landing(), hero=True)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return _page("Podsen · sign up", app_views.auth_form("signup", csrf=_csrf_token()))
    email = _norm_email(request.form.get("email"))
    password = request.form.get("password") or ""
    error = None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        error = "Enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif len(password) > 1000:
        error = "Password is too long."
    if error is None:
        uid = db.create_user(email, generate_password_hash(password))
        if uid is None:
            error = "An account with this email already exists. Log in instead."
    if error:
        return _page("Podsen · sign up", app_views.auth_form("signup", email, error, _csrf_token()), 400)
    _log_in(uid)
    return redirect("/v2/shows")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return _page("Podsen · log in", app_views.auth_form("login", csrf=_csrf_token()))
    email = _norm_email(request.form.get("email"))
    user, wait = _password_ok(email, request.form.get("password") or "")
    if wait:
        msg = f"Too many failed attempts. Try again in about {wait} minute(s)."
        return _page("Podsen · log in", app_views.auth_form("login", email, msg, _csrf_token()), 429)
    if user is None:
        msg = "Wrong email or password."
        return _page("Podsen · log in", app_views.auth_form("login", email, msg, _csrf_token()), 401)
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
    # Same throttle as login: a stolen session must not be a way to guess the password.
    checked, wait = _password_ok(user["email"], request.form.get("password") or "")
    if wait or checked is None:
        msg = (f"Too many failed attempts. Try again in about {wait} minute(s)." if wait
               else "That password is not right. Your account was not deleted.")
        body = app_views.account_page(user["email"], msg, _csrf_token())
        return _page("Podsen · account", body, 429 if wait else 401, logged_in=True)
    db.delete_user(user["id"])
    _log_out()
    return _page("Podsen", app_views.account_deleted())


# --- shows -------------------------------------------------------------------------------

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
    body = app_views.shows_page(db.list_shows(user["id"]), query, results, error, _csrf_token())
    return _page("Podsen · shows", body, logged_in=True)


@bp.post("/shows/add")
@login_required
def shows_add(user):
    itunes_id = (request.form.get("itunes_id") or "").strip()
    try:
        show = itunes.lookup(itunes_id)
    except Exception as e:
        body = app_views.shows_page(
            db.list_shows(user["id"]), error=f"Could not add that show ({type(e).__name__}).",
            csrf=_csrf_token(),
        )
        return _page("Podsen · shows", body, 502, logged_in=True)
    if show is None or not show["feed_url"]:
        body = app_views.shows_page(
            db.list_shows(user["id"]), error="No public feed, can not be analyzed.", csrf=_csrf_token()
        )
        return _page("Podsen · shows", body, 400, logged_in=True)
    db.add_show(user["id"], show)
    return redirect("/v2/shows")


@bp.post("/shows/remove")
@login_required
def shows_remove(user):
    db.remove_show(user["id"], (request.form.get("itunes_id") or "").strip())
    return redirect("/v2/shows")


# --- today -> run --------------------------------------------------------------------------

@bp.get("/today")
@login_required
def today(user):
    n = len(db.list_shows(user["id"]))
    return _page("Podsen · today", app_views.today_form(n, csrf=_csrf_token()), logged_in=True)


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
    n = len(db.list_shows(user["id"]))
    if n < app_views.MIN_SHOWS:
        return _page("Podsen · today", app_views.today_form(n, csrf=_csrf_token()), 400, logged_in=True)
    minutes = _minutes()
    if minutes is None:
        body = app_views.today_form(
            n, "Time must be a whole number of minutes from 1 to 600.", _csrf_token()
        )
        return _page("Podsen · today", body, 400, logged_in=True)

    bl = backlog.build(user["id"])
    picks, skips = engine.pick(bl["episodes"], minutes)
    picks = links.add_apple_episode_urls(picks)  # only picks show "Open in" links
    slim = lambda eps: [{k: e.get(k) for k in RUN_FIELDS} for e in eps]  # noqa: E731
    run_id = db.save_run(
        user["id"],
        minutes,
        engine.ENGINE_NAME,
        {
            "picks": slim(picks),
            "skips": slim(skips),
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
