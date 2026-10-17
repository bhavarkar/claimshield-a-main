"""Verify IAP assertions. Browser login/cookies are owned by IAP, not this app."""

from dataclasses import dataclass
from functools import lru_cache
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
import jwt

from ..config import Settings

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key-jwk"


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    issuer: str
    expires_at: float


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    audience: str
    origin: str
    memberships: dict[str, str]


def configure_auth(settings: Settings) -> AuthConfig:
    if settings.auth_mode == "demo":
        if os.getenv("K_SERVICE") or os.getenv("K_REVISION"):
            raise ValueError("Demo authentication is forbidden on Cloud Run")
        return AuthConfig("demo", "", "", {})
    if settings.auth_mode != "iap":
        raise ValueError("CLAIMSHIELD_AUTH_MODE must be iap or demo")
    origin = urlsplit(settings.public_origin)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment):
        raise ValueError("CLAIMSHIELD_PUBLIC_ORIGIN must be an HTTPS origin without a path")
    if not re.fullmatch(r"/projects/[0-9]+/locations/[a-z0-9-]+/services/[a-z0-9-]+", settings.iap_audience):
        raise ValueError("Set CLAIMSHIELD_IAP_AUDIENCE to the native Cloud Run IAP resource audience")
    try:
        memberships = json.loads(settings.auth_memberships)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("CLAIMSHIELD_AUTH_MEMBERSHIPS must be a JSON object") from exc
    if not isinstance(memberships, dict) or not memberships:
        raise ValueError("Configure verified IAP subject-to-tenant memberships")
    for subject, tenant in memberships.items():
        if (not isinstance(subject, str) or not subject.startswith("accounts.google.com:")
                or not subject.removeprefix("accounts.google.com:")
                or not isinstance(tenant, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tenant)):
            raise ValueError("Invalid subject-to-tenant membership")
    return AuthConfig("iap", settings.iap_audience, settings.public_origin, memberships)


@lru_cache(maxsize=1)
def iap_keys():
    # Fixed Google endpoint; token-supplied jku/x5u headers are never used.
    return jwt.PyJWKClient(IAP_KEYS, timeout=5, lifespan=300)


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def authenticate(request: Request, config: AuthConfig) -> Principal:
    if config.mode == "demo":
        if not request.client or not loopback(request.client.host) or not loopback(request.url.hostname or ""):
            raise HTTPException(403, "Local demo access only")
        principal = Principal("local-demo", "local-demo", "urn:claimshield:demo", float("inf"))
    else:
        token = request.headers.get("x-goog-iap-jwt-assertion", "")
        if not token or len(token) > 16384:
            raise HTTPException(401, "Sign in through the configured IAP service")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
                raise jwt.InvalidTokenError()
            key = iap_keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["ES256"], audience=config.audience,
                                issuer=IAP_ISSUER,
                                options={"require": ["sub", "iss", "aud", "exp", "iat"], "strict_aud": True})
            if (type(claims["exp"]) not in (int, float) or type(claims["iat"]) not in (int, float)
                    or claims["exp"] <= claims["iat"] or not isinstance(claims["sub"], str)
                    or not claims["sub"]):
                raise jwt.InvalidTokenError()
        except jwt.PyJWKClientConnectionError:
            raise HTTPException(503, "Identity verification temporarily unavailable") from None
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise HTTPException(401, "Invalid or expired identity assertion") from None
        tenant = config.memberships.get(claims["sub"])
        if tenant is None:
            raise HTTPException(403, "No application membership")
        principal = Principal(claims["sub"], tenant, IAP_ISSUER, claims["exp"])

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # IAP's browser cookies are ambient credentials: require a non-simple
        # header and a trusted Origin. No cross-origin CORS permissions are granted.
        if request.headers.get("x-claimshield-request") != "1":
            raise HTTPException(403, "Missing request protection header")
        origin = request.headers.get("origin")
        expected = config.origin if config.mode == "iap" else str(request.base_url).rstrip("/")
        if (config.mode == "iap" and origin != expected) or (origin is not None and origin != expected):
            raise HTTPException(403, "Untrusted request origin")
    return principal
