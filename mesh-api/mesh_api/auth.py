import hmac
import json
import os
import sys
import time
from pathlib import Path

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_MESH_HOME = os.environ.get("MESH_HOME", os.path.expanduser("~/mesh"))
TOKENS_PATH = Path(os.environ.get("MESH_TOKENS_PATH", f"{_MESH_HOME}/api-tokens.json"))
_TOKEN_CACHE_TTL = 30  # seconds

_bearer = HTTPBearer(auto_error=False)
_cached_tokens: set[str] = set()
_cache_loaded_at: float = 0.0


def _load_tokens() -> set[str]:
    global _cached_tokens, _cache_loaded_at
    now = time.monotonic()
    if now - _cache_loaded_at < _TOKEN_CACHE_TTL:
        return _cached_tokens
    if not TOKENS_PATH.exists():
        _cached_tokens = set()
        _cache_loaded_at = now
        return _cached_tokens
    # Warn — never block — when the token file is readable by anyone but its
    # owner (the usual 0644 slip). Refusing to start over a permission bit would
    # take the mesh down for something the operator can fix in one command.
    try:
        mode = TOKENS_PATH.stat().st_mode
        if mode & 0o077:
            print(
                f"WARNING: {TOKENS_PATH} is accessible by group/others "
                f"(mode {oct(mode & 0o777)}); tokens may leak. Run: chmod 600 {TOKENS_PATH}",
                file=sys.stderr,
            )
    except OSError:
        pass
    try:
        data = json.loads(TOKENS_PATH.read_text())
        _cached_tokens = {entry["token"] for entry in data if "token" in entry}
    except Exception:
        _cached_tokens = set()
    _cache_loaded_at = now
    return _cached_tokens


def require_auth(credentials: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    # Deliberate bypass: the dashboard runs on a private network the operator
    # already trusts, and typing a token into every page there buys nothing.
    # Reversible — drop MESH_API_NO_AUTH from mesh.env and authentication is back.
    # What still protects the API is the firewall: it is reachable from the
    # loopback and the private interface only, everything else is dropped. Note
    # that this bypass covers READS alone; see require_write_auth below.
    if os.environ.get("MESH_API_NO_AUTH") == "1":
        return "noauth"
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = credentials.credentials
    if not any(hmac.compare_digest(token, t) for t in _load_tokens()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return token


def verify_ws_token(token: str | None) -> bool:
    if os.environ.get("MESH_API_NO_AUTH") == "1":
        return True
    if not token:
        return False
    return any(hmac.compare_digest(token, t) for t in _load_tokens())


def require_write_auth(credentials: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    """Write authentication — deliberately IGNORES MESH_API_NO_AUTH.

    The "no token" bypass was granted for a READ-ONLY dashboard, where the worst
    case is someone on the private network reading a board. A route that writes
    is not covered by that trade: there the worst case is losing work. So reads
    stay open and writes keep their token.

    The token is NOT served inside the page — an audit had to rotate the
    dashboard token for exactly that reason. The page asks for it once and keeps
    it in the browser's localStorage.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Write access requires a token")
    token = credentials.credentials
    if not any(hmac.compare_digest(token, t) for t in _load_tokens()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide")
    return token
