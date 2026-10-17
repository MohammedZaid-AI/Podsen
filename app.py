"""Podsen web app: the V2 product flow under /v2, the /label evaluation tool, /admin.

The app itself lives in the v2/ Blueprint; this file wires it up and holds the two
operator-only pages.
"""

import hmac
import json
import os
import re
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode

from flask import Flask, redirect, request

try:  # read .env here too, not only via rank.py: DATABASE_URL and SUPABASE_* live there
    from dotenv import load_dotenv

    if not os.environ.get("PODSEN_SKIP_DOTENV"):  # set by conftest.py for tests
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import views
from v2 import dataset as v2_dataset
from v2 import db as v2_db
from v2 import label_views
from v2 import rss as v2_rss
from v2.web import bp as v2_bp

# --- production settings ---------------------------------------------------------------

DEV_SECRET = "dev-insecure-secret"
# Values shipped in .env.example. Treated as "not set": copying the example into
# production must never leave a guessable secret or admin key in place.
PLACEHOLDERS = {"", DEV_SECRET, "change-this", "pick-a-long-random-string"}
MIN_SECRET_CHARS = 32


def is_production(env=os.environ) -> bool:
    return (env.get("FLASK_ENV") or "").strip().lower() == "production"


def production_problems(env=os.environ) -> list[str]:
    """Everything that must be fixed before this can serve real users. Names only:
    no value is ever included, so the message is safe to show in a deploy log."""
    if not is_production(env):
        return []
    problems = []
    secret = (env.get("SESSION_SECRET") or "").strip()
    if secret in PLACEHOLDERS:
        problems.append("SESSION_SECRET is missing or still the development default. "
                        "Set it to a long random value (Render: Generate).")
    elif len(secret) < MIN_SECRET_CHARS:
        problems.append(f"SESSION_SECRET is too short; use at least {MIN_SECRET_CHARS} random characters.")
    db_url = (env.get("DATABASE_URL") or "").strip()
    if not db_url:
        problems.append("DATABASE_URL is not set. Without it, users' data would go to a "
                        "temporary file that is wiped on every deploy. Use Supabase's Session pooler string.")
    elif not db_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        problems.append("DATABASE_URL must be a Postgres connection string (Supabase Session pooler).")
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        if not (env.get(name) or "").strip():
            problems.append(f"{name} is not set; Google sign-in can't work without it.")
    return problems


_problems = production_problems()
if _problems:
    raise RuntimeError("Podsen refuses to start in production:\n  - " + "\n  - ".join(_problems))

app = Flask(__name__)
# Render (and a cloudflared tunnel) terminate HTTPS and forward plain HTTP with
# X-Forwarded-Proto / X-Forwarded-For / Host. Trusting exactly one proxy hop makes
# request.url_root "https://<public host>/", so the Google callback URL is right.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SESSION_SECRET") or DEV_SECRET,
    SESSION_COOKIE_NAME="podsen",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # cross-site POSTs don't carry the cookie
    SESSION_COOKIE_SECURE=is_production(),  # HTTPS-only cookie in production
    PREFERRED_URL_SCHEME="https" if is_production() else "http",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # V2 logins last 30 days
)
app.register_blueprint(v2_bp)


def _admin_key_ok() -> bool:
    key = (os.environ.get("ADMIN_KEY") or "").strip()
    given = request.args.get("key") or ""
    if key in PLACEHOLDERS:  # unset, or the example value: the key path is off
        return False
    return hmac.compare_digest(given.encode(), key.encode())


@app.get("/health")
def health():
    """For Render's health check. Touches nothing (no database, no session), so a
    database outage can't make Render restart a healthy app in a loop."""
    return app.response_class("ok", mimetype="text/plain")


@app.get("/")
def index():
    return redirect("/v2/")


@app.get("/admin")
def admin():
    """Every run and every feedback answer, as JSON. No emails or password hashes."""
    if not _admin_key_ok():
        return "nope", 403
    data = {"runs": v2_db.all_runs(), "feedback": v2_db.all_feedback()}
    return app.response_class(
        json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json"
    )


# --- /label: hand-labeling public RSS backlogs for evaluation ---------------------
# Independent of any login and of the session: evaluation data is public RSS plus the
# labels typed in here. Fetched episodes wait in a draft file between steps instead
# of round-tripping 60 episodes through hidden form fields.

MAX_FEEDS = 30
LOOPBACK = {"127.0.0.1", "::1"}
# Headers a proxy or tunnel adds. cloudflared connects from 127.0.0.1, so loopback
# alone would let every tunnel visitor in; a request carrying any of these came from
# somewhere else and needs the key.
FORWARDING_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP")


def _label_is_local() -> bool:
    # Read the socket peer before ProxyFix rewrote REMOTE_ADDR from X-Forwarded-For.
    orig = request.environ.get("werkzeug.proxy_fix.orig", {})
    peer = orig.get("REMOTE_ADDR", request.environ.get("REMOTE_ADDR"))
    return peer in LOOPBACK and not any(h in request.headers for h in FORWARDING_HEADERS)


@app.before_request
def _lock_label_routes():
    """/label writes files and fetches URLs: this machine, or the admin key, only."""
    if request.path != "/label" and not request.path.startswith("/label/"):
        return None
    if _label_is_local() or _admin_key_ok():
        return None
    return "nope", 403


def _label_qs() -> str:
    """Carry ?key= through the form actions so a keyed session can still save."""
    key = request.args.get("key")
    return f"?{urlencode({'key': key})}" if key else ""


def _draft_path(draft_id: str):
    # The id comes back from the browser: hex-only, so it can never name another path.
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id or ""):
        return None
    return v2_dataset.labels_dir() / "drafts" / f"{draft_id}.json"


@app.get("/label")
def label_home():
    return views.page("Podsen · label", label_views.step1(qs=_label_qs()))


@app.post("/label/fetch")
def label_fetch():
    values = {k: request.form.get(k, "") for k in ("name", "feeds", "budget", "per_feed", "intent")}
    urls = [u.strip() for u in values["feeds"].splitlines() if u.strip()]
    try:
        budget = int(values["budget"])
    except ValueError:
        budget = 0
    try:
        per_feed = max(1, min(50, int(values["per_feed"] or 10)))
    except ValueError:
        per_feed = 10

    error = None
    if not urls:
        error = "Paste at least one RSS feed URL."
    elif len(urls) > MAX_FEEDS:
        error = f"At most {MAX_FEEDS} feeds per backlog."
    elif not 1 <= budget <= 600:
        error = "Budget must be a whole number of minutes from 1 to 600."
    if error:
        return views.page("Podsen · label", label_views.step1(values, error, _label_qs())), 400

    loaded = v2_rss.load_feeds(urls, per_feed=per_feed)
    draft = {
        "name": values["name"].strip()[:80] or f"backlog {date.today().isoformat()}",
        "budget_minutes": budget,
        "intent": values["intent"].strip()[:1000],
        **loaded,
    }
    if not draft["episodes"]:
        return views.page("Podsen · label", label_views.no_episodes(draft, _label_qs()))

    draft_id = uuid.uuid4().hex
    path = _draft_path(draft_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return views.page("Podsen · label", label_views.step2(draft_id, draft, qs=_label_qs()))


@app.post("/label/save")
def label_save():
    draft_id = request.form.get("draft", "")
    path = _draft_path(draft_id)
    if path is None or not path.exists():
        return views.page(
            "Podsen · label",
            '<p class="warn">That labeling session has expired or was already saved.</p>'
            f'<p style="margin-top:20px"><a class="btn" href="/label{views.esc(_label_qs())}">Start again</a></p>',
        ), 400
    draft = json.loads(path.read_text(encoding="utf-8"))
    eps = draft["episodes"]

    labels, groups, by_letter = {}, {}, {}
    for i, ep in enumerate(eps):
        lab = request.form.get(f"label_{i}")
        if lab in v2_dataset.LABELS:
            labels[i] = lab
        letter = (request.form.get(f"group_{i}") or "").strip().upper()[:3]
        if letter:
            groups[i] = letter
            by_letter.setdefault(letter, []).append(ep["id"])

    error = None
    missing = len(eps) - len(labels)
    lonely = sorted(k for k, ids in by_letter.items() if len(ids) < 2)
    if missing:
        error = f"{missing} episode(s) still need a label."
    elif lonely:
        error = f"Duplicate group(s) {', '.join(lonely)} have only one episode; a group needs at least two."
    if error:
        # Re-render with every choice kept: losing 60 labels to one missed radio is the
        # fastest way to make sure nobody labels a second backlog.
        return views.page(
            "Podsen · label", label_views.step2(draft_id, draft, labels, groups, error, _label_qs())
        ), 400

    backlog = {
        "name": draft["name"],
        "budget_minutes": draft["budget_minutes"],
        "intent": draft["intent"],
        "episodes": [{k: ep[k] for k in v2_rss.EPISODE_FIELDS if k in ep} for ep in eps],
        "labels": {eps[i]["id"]: lab for i, lab in labels.items()},
        "duplicate_groups": [ids for _, ids in sorted(by_letter.items())],
        "feeds": [f["url"] for f in draft.get("feeds", [])],
        "created": date.today().isoformat(),
    }
    saved = v2_dataset.save(backlog)
    path.unlink(missing_ok=True)
    return views.page(
        "Podsen · label",
        label_views.saved(str(saved), len(eps), len(backlog["duplicate_groups"]), _label_qs()),
    )


@app.errorhandler(Exception)
def on_error(err):
    # Let 404/405/etc. keep their own status; only real faults become the 500 page.
    if isinstance(err, HTTPException):
        return err
    # Full details go to the server log only. The page names the error type and no
    # more: a database or HTTP error message can carry hostnames or connection details.
    app.logger.exception(err)
    return (
        views.page(
            "Podsen",
            f"<p>Something broke ({views.esc(type(err).__name__)}). It has been logged.</p>"
            "<p><a href='/v2/'>Back</a></p>",
        ),
        500,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 3000)))
