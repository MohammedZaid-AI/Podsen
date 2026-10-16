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

app = Flask(__name__)
# Behind a tunnel or TLS-terminating host, trust one proxy hop: real client IP (used
# by the login throttle) and https scheme.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SESSION_SECRET", "dev-insecure-secret"),
    SESSION_COOKIE_NAME="podsen",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # cross-site POSTs don't carry the cookie
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # V2 logins last 30 days
)
app.register_blueprint(v2_bp)


def _admin_key_ok() -> bool:
    key = os.environ.get("ADMIN_KEY")
    given = request.args.get("key") or ""
    return bool(key) and hmac.compare_digest(given.encode(), key.encode())


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
