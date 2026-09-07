import json
import os
import secrets
import uuid
from datetime import date, timedelta

from flask import Flask, redirect, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import spotify
import views
from rank import rank
from store import append, read_all

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
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


def authed() -> bool:
    return bool((session.get("tokens") or {}).get("access_token"))


@app.get("/")
def index():
    if authed():
        return redirect("/app")
    return views.page("Podsen", views.landing())


@app.get("/login")
def login():
    state = secrets.token_hex(16)
    session["state"] = state
    return redirect(spotify.auth_url(state))


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


@app.get("/app")
def app_home():
    if not authed():
        return redirect("/")
    return views.page("Podsen", views.time_form(session.get("user")))


@app.post("/triage")
def triage():
    if not authed():
        return redirect("/")
    try:
        minutes = int(request.form.get("minutes", 30))
    except ValueError:
        minutes = 30
    minutes = max(1, min(600, minutes))

    user = session.get("user") or {}
    result = spotify.get_backlog(session, market=user.get("country") or "US")
    episodes, shows_scanned = result["episodes"], result["showsScanned"]
    dropped = result.get("dropped") or []

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
        views.results(sid, minutes, picks, skips, len(episodes), shows_scanned, dropped),
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
