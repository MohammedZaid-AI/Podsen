import hmac
import json
import os
import re
import secrets
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode

from flask import Flask, redirect, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import spotify
import views
from rank import rank
from store import append, read_all, read_prefs, write_pref
from v2 import dataset as v2_dataset
from v2 import label_views
from v2 import rss as v2_rss
from v2.web import bp as v2_bp

app = Flask(__name__)
# Behind Fly/Render TLS termination, trust one proxy hop so redirect_uri stays https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SESSION_SECRET", "dev-insecure-secret"),
    SESSION_COOKIE_NAME="podsen",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # still sent on the top-level GET back from Spotify
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production"
    or os.environ.get("NODE_ENV") == "production",
    # Only V2 marks its session permanent (V2 users stay logged in 30 days); V1 never
    # does, so its cookie still ends with the browser session as before.
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
# V2 product flow under /v2: own accounts + public RSS, independent of the V1 routes.
app.register_blueprint(v2_bp)


def authed() -> bool:
    return bool((session.get("tokens") or {}).get("access_token"))


@app.get("/")
def index():
    if authed():
        return redirect("/app")
    return views.page("Podsen", views.landing(), hero=True)

@app.get("/login")
def login():
    state = secrets.token_hex(16)
    session["state"] = state

    url = spotify.auth_url(state)
    print("Spotify auth URL:", url)

    return redirect(url)

@app.get("/callback")
def callback():
    if request.args.get("error"):
        return views.page(
            "Podsen",
            f'<p>Spotify: {views.esc(request.args["error"])}</p><p><a href="/">Try again</a></p>',
        )
    state = request.args.get("state")
    if not state or state != session.get("state"):
        return views.page("Podsen", '<p>Auth state mismatch. <a href="/login">Retry</a></p>'), 400

    session["tokens"] = spotify.exchange_code(request.args.get("code"))
    session["user"] = spotify.get_me(session)
    session.pop("state", None)
    return redirect("/app")


def _selected_shows(user_id: str) -> set | None:
    """Stored show selection, or None meaning "all" (incl. shows followed later)."""
    saved = read_prefs(user_id).get("shows")
    return set(saved) if saved else None


@app.get("/app")
def app_home():
    if not authed():
        return redirect("/")
    user = session.get("user") or {}
    shows = spotify.get_followed_shows(session)
    return views.page(
        "Podsen",
        views.time_form(user, shows, _selected_shows(user.get("id"))),
    )


@app.get("/shows/all")
def shows_all():
    """Reset to scanning everything. Clearing the pref (rather than storing every id)
    means shows followed later are included automatically."""
    if not authed():
        return redirect("/")
    write_pref((session.get("user") or {}).get("id"), "shows", None)
    return redirect("/app")


@app.post("/triage")
def triage():
    if not authed():
        return redirect("/")
    raw = request.form.get("minutes", "30")
    if raw == "custom":  # the number box, not one of the preset buttons
        raw = request.form.get("custom_minutes", "30")
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 30
    minutes = max(1, min(600, minutes))

    user = session.get("user") or {}
    # An empty list means the user unticked everything, which is not the same as
    # "no preference" - say so rather than returning a confusingly empty triage.
    picked = [sid for sid in request.form.getlist("show") if sid]
    if request.form.get("show_picker") and not picked:
        return views.page(
            "Podsen",
            '<p class="warn">Select at least one show to scan.</p>'
            '<p style="margin-top:20px"><a class="btn" href="/app">Back</a></p>',
        )
    selected = set(picked) if picked else _selected_shows(user.get("id"))

    result = spotify.get_backlog(
        session, market=user.get("country") or "US", selected_ids=selected
    )
    episodes, shows_scanned = result["episodes"], result["showsScanned"]
    dropped = result.get("dropped") or []

    # Remember the scope for next time. All-selected is stored as "no preference" so a
    # newly followed show is picked up automatically instead of being silently excluded.
    if picked:
        write_pref(
            user.get("id"),
            "shows",
            None if result["showsSelected"] >= result["showsFollowed"] else picked,
        )

    if not episodes:
        return views.page(
            "Podsen",
            f"<p>No unheard episodes found across {shows_scanned} shows. Inbox zero &mdash; nice.</p>"
            # An empty backlog is only trustworthy if we actually read every show.
            f"{views.dropped_note(dropped)}"
            '<p><a href="/app">Back</a></p>',
        )

    ranked = rank(episodes, minutes)
    picks, skips = ranked["picks"], ranked["skips"]

    if ranked.get("shortest_unfit"):
        return views.page(
            "Podsen",
            views.nothing_fits(minutes, ranked["shortest_unfit"], len(episodes), dropped),
        )
    sid = str(uuid.uuid4())
    append(
        "events.jsonl",
        {
            "sid": sid,
            "type": "triage",
            "user": user.get("id"),
            "minutes": minutes,
            "backlog": len(episodes),
            "showsScanned": shows_scanned,
            "showsSelected": result.get("showsSelected"),
            "showsFollowed": result.get("showsFollowed"),
            "dropped": dropped,
            "picks": [
                {"id": p["id"], "uri": p["uri"], "title": p["title"], "show": p["show"], "reason": p["reason"]}
                for p in picks
            ],
            "skips": [
                {"id": s["id"], "title": s["title"], "show": s["show"], "reason": s["reason"]}
                for s in skips
            ],
        },
    )
    return views.page(
        "Podsen",
        views.results(
            sid, minutes, picks, skips, len(episodes), shows_scanned, dropped,
            shows_followed=result.get("showsFollowed"),
        ),
    )


@app.post("/confirm")
def confirm():
    if not authed():
        return redirect("/")
    sid = request.form.get("sid")
    evs = [e for e in read_all("events.jsonl") if e.get("sid") == sid and e.get("type") == "triage"]
    if not evs:
        return views.page("Podsen", '<p>Unknown session. <a href="/app">Start over</a></p>'), 400
    ev = evs[-1]

    chosen = set(request.form.getlist("pick"))
    picks = [p for p in ev["picks"] if p["id"] in chosen]

    playlist = None
    if request.form.get("make_playlist") and picks:
        playlist = spotify.create_playlist(
            session,
            session["user"]["id"],
            f"Podsen — {date.today().isoformat()}",
            f"Picked by Podsen for a {ev['minutes']}-min listen.",
            [p["uri"] for p in picks],
        )

    append(
        "events.jsonl",
        {
            "sid": sid,
            "type": "confirm",
            "confirmed": [p["id"] for p in picks],
            "playlist": (playlist or {}).get("url"),
        },
    )
    return views.page("Podsen", views.done(sid, picks, playlist))


@app.post("/feedback")
def feedback():
    append(
        "events.jsonl",
        {
            "sid": request.form.get("sid"),
            "type": "feedback",
            "useful": request.form.get("useful"),
            "pay": request.form.get("pay"),
            "note": (request.form.get("note") or "")[:500],
        },
    )
    return views.page("Podsen", views.thanks())


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.get("/admin")
def admin():
    key = os.environ.get("ADMIN_KEY")
    if not key or request.args.get("key") != key:
        return "nope", 403
    return app.response_class(
        json.dumps(read_all("events.jsonl"), indent=2, ensure_ascii=False),
        mimetype="application/json",
    )


# --- V2 Phase 0: labeling public RSS backlogs for evaluation ----------------------
# Deliberately independent of the Spotify login and session: V2 may only use public
# RSS data and labels typed in here (Spotify policy bars its data from AI/ML use).
# Fetched episodes wait in a draft file between steps instead of round-tripping
# 60 episodes through hidden form fields.

MAX_FEEDS = 30
LOOPBACK = {"127.0.0.1", "::1"}
# Headers a proxy or tunnel adds. cloudflared connects from 127.0.0.1, so loopback
# alone would let every tunnel visitor in; a request carrying any of these came from
# somewhere else and needs the key.
FORWARDING_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP")


def _label_key_ok() -> bool:
    key = os.environ.get("ADMIN_KEY")
    given = request.args.get("key") or ""
    return bool(key) and hmac.compare_digest(given.encode(), key.encode())


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
    if _label_is_local() or _label_key_ok():
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
    app.logger.exception(err)
    return (
        views.page(
            "Podsen",
            f"<p>Something broke:</p><pre>{views.esc(err)}</pre><p><a href='/app'>Back</a></p>",
        ),
        500,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 3000)))
