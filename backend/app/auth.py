"""
Minimal Clerk JWT verification for protecting FastAPI routes.

Enabled only when CLERK_AUTH_ENABLED=true (see app/config.py). When
enabled, requests must include:

    Authorization: Bearer <clerk-session-jwt>

The frontend (Next.js + Clerk) attaches this automatically via
`useAuth().getToken()` — see frontend/lib/api.ts.
"""
import time

import httpx
from fastapi import HTTPException
from jose import jwt

from app.config import get_settings

_jwks_cache: dict = {"keys": None, "fetched_at": 0}
_JWKS_TTL_SECONDS = 3600


async def _get_jwks() -> dict:
    settings = get_settings()
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < _JWKS_TTL_SECONDS:
        return _jwks_cache["keys"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(settings.CLERK_JWKS_URL)
        resp.raise_for_status()
        jwks = resp.json()

    _jwks_cache["keys"] = jwks
    _jwks_cache["fetched_at"] = now
    return jwks


async def verify_clerk_token(token: str) -> dict:
    settings = get_settings()
    try:
        jwks = await _get_jwks()
        header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == header["kid"]), None)
        if key is None:
            raise HTTPException(status_code=401, detail="Unknown signing key")

        claims = jwt.decode(
            token,
            key,
            algorithms=[header.get("alg", "RS256")],
            issuer=settings.CLERK_ISSUER or None,
            options={"verify_aud": False},
        )
        return claims
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
