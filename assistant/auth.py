"""Verifying the portal's tokens at the MCP servers.

Each MCP server is its own OAuth protected resource (RFC 9728) with its own canonical
URL. A token is accepted only if:
  * it is signed by the portal (JWKS) and `iss` is the portal,
  * `aud` is THIS server's URL (a citizen token minted for /mcp/citizen does not work
    at /mcp/officer, and neither works anywhere else),
  * `role` and `scope` match this server (citizen or officer),
  * the sign-in behind it is still alive: the portal lets people end a sign-in from
    "Connected applications", which only revokes the refresh token. Without this check
    a revoked access token keeps working for up to 20-30 minutes, and an admin-issued
    program token can never be revoked at all.
"""

import os
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

PORTAL_INTERNAL = os.environ.get("PORTAL_INTERNAL_URL", "http://app:8000").rstrip("/")
ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")
# Only for clients that do not send RFC 8707 `resource` (the portal then uses its own
# URL as audience). Off by default: the MCP spec requires `resource`.
ACCEPT_PORTAL_AUDIENCE = os.environ.get("ACCEPT_PORTAL_AUDIENCE") == "1"
LIVENESS_TTL = 15  # seconds a positive liveness answer is cached


class PortalAccessToken(AccessToken):
    subject: str
    role: str
    client_name: str = ""


_jwks = {"at": 0.0, "keys": {}}
_alive_cache: dict[str, float] = {}


async def _keys(force=False):
    if force or time.time() - _jwks["at"] > 600 or not _jwks["keys"]:
        async with httpx.AsyncClient(timeout=5) as c:
            data = (await c.get(PORTAL_INTERNAL + "/.well-known/jwks.json")).json()
        _jwks["keys"] = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in data["keys"]}
        _jwks["at"] = time.time()
    return _jwks["keys"]


async def grant_alive(claims) -> bool:
    key = "%s|%s|%s" % (claims.get("kind"), claims.get("sub"), claims.get("client_id") or claims.get("jti"))
    if _alive_cache.get(key, 0) > time.time():
        return True
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(PORTAL_INTERNAL + "/internal/api/grant-alive",
                         headers={"X-Internal-Key": INTERNAL_KEY},
                         json={"kind": claims.get("kind"), "sub": claims.get("sub"),
                               "client_id": claims.get("client_id"), "jti": claims.get("jti")})
    alive = r.status_code == 200 and r.json().get("alive") is True
    if alive:
        _alive_cache[key] = time.time() + LIVENESS_TTL
    return alive


class PortalTokenVerifier(TokenVerifier):
    def __init__(self, role: str, resource_url: str):
        self.role = role
        self.audiences = [resource_url.rstrip("/"), resource_url.rstrip("/") + "/"]
        if ACCEPT_PORTAL_AUDIENCE:
            self.audiences += [ISSUER, ISSUER + "/"]

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            keys = await _keys()
            if kid not in keys:
                keys = await _keys(force=True)
            claims = jwt.decode(token, keys[kid], algorithms=["RS256"], audience=self.audiences,
                                issuer=ISSUER, options={"require": ["exp", "sub", "aud", "iss"]})
        except Exception:
            return None
        if claims.get("role") != self.role or self.role not in (claims.get("scope") or "").split():
            return None
        if not str(claims.get("sub", "")).startswith(self.role + ":"):
            return None
        if not await grant_alive(claims):
            return None
        return PortalAccessToken(token=token, client_id=claims.get("client_id") or "unknown",
                                 scopes=[self.role], expires_at=claims["exp"],
                                 resource=claims["aud"] if isinstance(claims["aud"], str) else None,
                                 subject=claims["sub"], role=claims["role"])
