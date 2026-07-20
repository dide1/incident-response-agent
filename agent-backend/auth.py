"""GitHub OAuth login — multi-user; any GitHub account can sign in."""
import json
import os
import urllib.parse
import urllib.request

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
# Optional: if set, only this GitHub username is allowed. Empty = anyone.
ALLOWED_GITHUB_USER = os.getenv("ALLOWED_GITHUB_USER", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

COOKIE_NAME = "ira_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _signer() -> URLSafeTimedSerializer:
    if not SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET env var is not set")
    return URLSafeTimedSerializer(SESSION_SECRET)


def make_session_cookie(user_id: int, username: str) -> str:
    return _signer().dumps({"uid": user_id, "u": username})


def verify_session_cookie(cookie: str) -> dict | None:
    """Return {"uid": int, "u": str} if the cookie is valid, else None."""
    try:
        data = _signer().loads(cookie, max_age=COOKIE_MAX_AGE)
        if "uid" in data and "u" in data:
            return data
        return None
    except (BadSignature, SignatureExpired, Exception):
        return None


def is_auth_enabled() -> bool:
    return bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET and SESSION_SECRET)


def github_auth_url(state: str) -> str:
    params = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "state": state,
        "scope": "read:user repo",
    })
    return f"https://github.com/login/oauth/authorize?{params}"


def exchange_code(code: str) -> str | None:
    data = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=data,
        headers={"Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return result.get("access_token")


def get_github_user_info(token: str) -> dict | None:
    """Return {"id": int, "login": str} or None on failure."""
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if "id" not in data or "login" not in data:
        return None
    return {"id": data["id"], "login": data["login"]}
