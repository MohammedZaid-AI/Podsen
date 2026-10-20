"""Production readiness: startup checks, HTTPS-only cookies, /health, proxy URLs, deploy files.

The startup checks run at import time, so the "refuses to start" tests import the
app in a fresh process with production settings, exactly as gunicorn would. No
network: the fake DATABASE_URL points at a reserved .invalid host that never
resolves, and nothing here connects to it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app as app_mod  # noqa: E402
from v2 import db  # noqa: E402

GOOD_SECRET = "s" * 48
UNREACHABLE_DB = "postgresql://postgres.ref:pw@aws-0-x.pooler.supabase.invalid:5432/postgres"
PROD = {
    "FLASK_ENV": "production",
    "SESSION_SECRET": GOOD_SECRET,
    "DATABASE_URL": UNREACHABLE_DB,
    "SUPABASE_URL": "https://podsen-test.supabase.invalid",
    "SUPABASE_ANON_KEY": "anon-test",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-value",
}


def run_app(env_overrides: dict, code: str = "import app; print('STARTED')"):
    """Import the app in a new process with a production-like environment."""
    env = {k: v for k, v in os.environ.items()
           if k not in PROD and not k.startswith(("SUPABASE_", "DATABASE_"))}
    env.update({"PODSEN_SKIP_DOTENV": "1"})
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


# --- refusing to start ---------------------------------------------------------------------

@pytest.mark.parametrize("secret", [None, "", "dev-insecure-secret", "change-this", "short-secret"])
def test_production_refuses_to_start_without_a_real_session_secret(secret, tmp_path):
    r = run_app({**PROD, "SESSION_SECRET": secret, "DATA_DIR": str(tmp_path)})
    assert r.returncode != 0 and "STARTED" not in r.stdout
    assert "Podsen refuses to start in production" in r.stderr
    assert "SESSION_SECRET" in r.stderr


def test_production_starts_with_every_setting_and_without_touching_the_database(tmp_path):
    r = run_app({**PROD, "DATA_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "STARTED"


def test_refusal_names_the_problems_but_never_the_values(tmp_path):
    r = run_app({**PROD, "SESSION_SECRET": "change-this", "DATABASE_URL": None,
                 "SUPABASE_ANON_KEY": None, "DATA_DIR": str(tmp_path)})
    assert r.returncode != 0
    for name in ("SESSION_SECRET", "DATABASE_URL", "SUPABASE_ANON_KEY"):
        assert name in r.stderr, name
    for value in ("service-role-test-value", "podsen-test.supabase.invalid", "anon-test"):
        assert value not in r.stderr, value


def test_development_needs_none_of_it():
    assert app_mod.production_problems({"FLASK_ENV": "development"}) == []
    assert app_mod.production_problems({}) == []


@pytest.mark.parametrize("change,expected", [
    ({}, []),
    ({"DATABASE_URL": ""}, ["DATABASE_URL is not set"]),
    ({"DATABASE_URL": "sqlite:///data/podsen_v2.db"}, ["must be a Postgres connection string"]),
    ({"SUPABASE_URL": ""}, ["SUPABASE_URL is not set"]),
    ({"SUPABASE_SERVICE_ROLE_KEY": " "}, ["SUPABASE_SERVICE_ROLE_KEY is not set"]),
    ({"SESSION_SECRET": "x" * 31}, ["too short"]),
])
def test_production_problems_lists_each_missing_piece(change, expected):
    problems = app_mod.production_problems({**PROD, **change})
    assert len(problems) == len(expected)
    for p, e in zip(problems, expected):
        assert e in p


# --- cookies ------------------------------------------------------------------------------

COOKIE_PROBE = (
    "import app; c = app.app.test_client(); "
    "r = c.get('/v2/privacy', base_url='https://podsen.onrender.com'); print([h for h in r.headers.getlist('Set-Cookie') if h.startswith('podsen=')])"
)


def test_production_session_cookie_is_https_only(tmp_path):
    r = run_app({**PROD, "DATA_DIR": str(tmp_path)}, COOKIE_PROBE)
    assert r.returncode == 0, r.stderr
    cookie = r.stdout
    assert "podsen=" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_development_cookie_still_works_over_plain_http():
    c = app_mod.app.test_client()
    cookie = next(h for h in c.get("/v2/privacy").headers.getlist("Set-Cookie") if h.startswith("podsen="))
    assert "Secure" not in cookie and "HttpOnly" in cookie


# --- /health ------------------------------------------------------------------------------

def test_health_is_200_without_touching_the_database(monkeypatch):
    def no_db():
        raise AssertionError("/health must not touch the database")

    monkeypatch.setattr(db, "engine", no_db)
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    r = app_mod.app.test_client().get("/health")
    assert r.status_code == 200 and r.get_data(as_text=True) == "ok"
    assert not r.headers.getlist("Set-Cookie"), "no session either"


def test_health_works_in_production_with_the_database_unreachable(tmp_path):
    code = ("import app; r = app.app.test_client().get('/health'); "
            "print(r.status_code, r.get_data(as_text=True))")
    r = run_app({**PROD, "DATA_DIR": str(tmp_path)}, code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "200 ok"


# --- behind Render's proxy ------------------------------------------------------------------

def test_google_callback_is_https_behind_renders_proxy():
    # Render terminates TLS and forwards plain HTTP with the public Host header,
    # X-Forwarded-Proto and X-Forwarded-For.
    c = app_mod.app.test_client()
    r = c.get("/v2/login", base_url="http://podsen.onrender.com",
              headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})
    redirect_to = parse_qs(urlparse(r.headers["Location"]).query)["redirect_to"][0]
    assert redirect_to == "https://podsen.onrender.com/v2/auth/callback"


def test_label_is_locked_when_requests_come_through_a_proxy():
    c = app_mod.app.test_client()
    r = c.get("/label", environ_base={"REMOTE_ADDR": "10.0.0.7"},
              headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
    assert r.status_code == 403


@pytest.mark.parametrize("key", ["pick-a-long-random-string", "change-this", ""])
def test_admin_ignores_placeholder_keys(monkeypatch, key):
    monkeypatch.setenv("ADMIN_KEY", key)
    assert app_mod.app.test_client().get(f"/admin?key={key}").status_code == 403


# --- deploy files ----------------------------------------------------------------------------

def test_dockerfile_runs_gunicorn_with_2_workers_and_a_long_timeout():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in text.splitlines() if line.startswith("CMD"))
    assert "gunicorn app:app" in cmd and "--workers 2" in cmd
    assert int(re.search(r"--timeout (\d+)", cmd).group(1)) >= 120
    assert "${PORT}" in cmd and "--access-logfile" not in cmd
    assert "USER podsen" in text, "the app does not run as root"


def test_render_yaml_lists_every_env_var_and_holds_no_secret_values():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "runtime: docker" in text and "healthCheckPath: /health" in text
    env, current = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*- key: (\w+)", line)
        if m:
            current = m.group(1)
            env[current] = ""
        elif current and not line.strip().startswith("#"):
            env[current] += line.strip() + "\n"
    for name in ("FLASK_ENV", "SESSION_SECRET", "ADMIN_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY",
                 "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL"):
        assert name in env, name
    for secret in ("SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL", "SUPABASE_URL"):
        assert "sync: false" in env[secret] and "value:" not in env[secret], secret
    for generated in ("SESSION_SECRET", "ADMIN_KEY"):
        assert "generateValue: true" in env[generated], generated
    assert "value: production" in env["FLASK_ENV"]


def test_every_env_var_the_code_reads_is_in_render_yaml():
    """Catch a new os.environ setting that someone forgets to add to the deploy."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in
                     [ROOT / "app.py", ROOT / "rank.py", *sorted((ROOT / "v2").glob("*.py"))])
    # os.environ.get("X") / os.environ["X"] / env.get("X") (production_problems) / _key("X")
    read = set(re.findall(r"""(?:os\.environ(?:\.get\(|\[)|\benv\.get\(|_key\()\s*["']([A-Z_]+)["']""", code))
    optional = {"PORT", "PODSEN_SKIP_DOTENV", "RANK_BACKEND", "GROQ_MODEL", "GROQ_BASE_URL",
                "GROQ_TIMEOUT_S", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"}
    yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
    missing = sorted(n for n in read - optional if f"- key: {n}" not in yaml)
    assert missing == []
    assert {"SESSION_SECRET", "DATABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"} <= read, "the scan must work"


# --- mobile basics -------------------------------------------------------------------------

def test_pages_are_set_up_for_phones():
    import views
    from v2 import app_views

    html = app_views.page("Podsen", app_views.landing(), hero=True)
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html
    css = views.page("x", "") + app_views._STYLE
    assert "min-height: 44px" in css, "tap targets are at least 44px tall"
    assert "img, audio, video {{ max-width: 100%; }}".replace("{{", "{").replace("}}", "}") in css


def test_landing_is_one_line_one_button_one_line():
    from v2 import app_views

    body = app_views.landing()
    assert body.count("<p") == 3
    assert body.count('href="/v2/login"') == 1 and "Continue with Google" in body
    assert "Works with Spotify, YouTube, Apple Podcasts and Amazon Music." in body


def test_the_app_can_be_installed_on_a_home_screen():
    import views
    from v2 import app_views

    client = app_mod.app.test_client()
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    m = r.get_json(force=True)
    assert m["display"] == "standalone" and m["start_url"] == "/v2/" and m["name"] == "Podsen"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    head = views.page("Podsen", "<p>x</p>").split("</head>")[0]
    for tag in ('<link rel="manifest" href="/manifest.webmanifest">',
                '<meta name="theme-color" content="#101114">',
                '<meta name="apple-mobile-web-app-capable" content="yes">'):
        assert tag in head, tag
    # The install button is on the landing page only, never in the app.
    assert "install-btn" not in app_views.page("Podsen", "<p>x</p>", logged_in=True)


def test_landing_page_is_served_at_root_with_the_install_button():
    client = app_mod.app.test_client()
    r = client.get("/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    html = r.get_data(as_text=True)
    # Same address as the app, so its install button installs the app.
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'class="btn install-btn" href="#install">Install the Podsen app</a>' in html
    assert 'id="install"' in html and "beforeinstallprompt" in html
    # Links stay on this site; images come from /assets/.
    assert "onrender.com" not in html and 'href="/v2/"' in html
    for asset in ("assets/podsen-logo.png", "assets/favicon.png", "assets/icons/spotify.svg"):
        assert asset in html and client.get("/" + asset).status_code == 200, asset
    assert client.get("/assets/../app.py").status_code == 404


def test_root_goes_to_the_app_when_there_is_no_landing_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "LANDING_DIR", tmp_path)
    r = app_mod.app.test_client().get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/v2/")
