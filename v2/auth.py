"""Google sign-in through Supabase Auth, PKCE flow, entirely server side.

1. /v2/login: make a code_verifier, keep it in the session, send the browser to
   Supabase's /authorize with its S256 code_challenge.
2. Supabase signs the user in with Google and redirects back with ?code=.
3. /v2/auth/callback: trade code + verifier at /token?grant_type=pkce (anon key).

The service-role key is used for one thing only, deleting an auth user, and only in a
request header from this server. It never goes into a page, a URL, an error or a log.
"""

import base64
import hashlib
import os
import secrets
from urllib.parse import quote, urlencode

import requests

TIMEOUT_S = 10


class AuthError(Exception):
    """A sign-in step failed. `str(e)` is safe to show the user; `kind` is for tests/logs."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _base() -> str:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        raise AuthError("not_configured", "Sign-in isn't set up on this server yet (SUPABASE_URL is missing).")
    return url


def _key(name: str) -> str:
    key = (os.environ.get(name) or "").strip()
    if not key:
        raise AuthError("not_configured", f"Sign-in isn't set up on this server yet ({name} is missing).")
    return key


# --- PKCE ------------------------------------------------------------------------------

def new_verifier() -> str:
    # 64 random bytes -> 86 URL-safe chars: inside RFC 7636's 43-128 range and alphabet.
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(redirect_to: str, verifier: str) -> str:
    params = {
        "provider": "google",
        "redirect_to": redirect_to,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "s256",
    }
    return f"{_base()}/auth/v1/authorize?{urlencode(params)}"


# --- Supabase calls ----------------------------------------------------------------------

def exchange_code(code: str, verifier: str) -> dict:
    """Return {"id", "email", "avatar_url"} for the signed-in user, or raise AuthError.
    avatar_url is None when Google sent no usable picture."""
    anon = _key("SUPABASE_ANON_KEY")
    try:
        r = requests.post(
            f"{_base()}/auth/v1/token",
            params={"grant_type": "pkce"},
            json={"auth_code": code, "code_verifier": verifier},
            headers={"apikey": anon, "Content-Type": "application/json"},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise AuthError("unreachable", "Couldn't reach the sign-in service. Please try again in a minute.") from e

    if r.status_code >= 500:
        raise AuthError("unreachable", "The sign-in service is having trouble. Please try again in a minute.")
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not r.ok:
        code_ = str(data.get("error_code") or data.get("error") or "")
        if code_ == "bad_code_verifier":
            raise AuthError("bad_verifier", "That sign-in didn't match this browser. Please start again.")
        raise AuthError("bad_code", "That sign-in link was already used or has expired. Please start again.")

    user = data.get("user") or {}
    uid, email = str(user.get("id") or ""), str(user.get("email") or "").strip().lower()
    if not uid or not email:
        raise AuthError("no_email", "Google didn't share an email address, so we can't sign you in.")
    return {"id": uid, "email": email, "avatar_url": avatar_url(user.get("user_metadata"))}


AVATAR_HOST = ".googleusercontent.com"


def avatar_url(meta) -> str | None:
    """Google's profile picture URL from Supabase's user_metadata, or None.

    Only an https URL on Google's own image host is kept: it is rendered as an <img>,
    so it must never point anywhere a signed-in page could leak to."""
    if not isinstance(meta, dict):
        return None
    url = meta.get("avatar_url") or meta.get("picture")
    if not isinstance(url, str) or len(url) > 1000:
        return None
    from urllib.parse import urlparse
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host.endswith(AVATAR_HOST):
        return None
    return url


def delete_auth_user(user_id: str) -> None:
    """Delete the Supabase auth user (admin API, service-role key, server side only).

    A user that is already gone counts as deleted.
    """
    service = _key("SUPABASE_SERVICE_ROLE_KEY")
    try:
        r = requests.delete(
            f"{_base()}/auth/v1/admin/users/{quote(user_id, safe='')}",
            headers={"apikey": service, "Authorization": f"Bearer {service}"},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise AuthError("unreachable", "Couldn't reach the sign-in service to remove your login.") from e
    if r.status_code == 404 or r.ok:
        return
    raise AuthError("admin_failed", f"The sign-in service refused to remove your login (HTTP {r.status_code}).")
