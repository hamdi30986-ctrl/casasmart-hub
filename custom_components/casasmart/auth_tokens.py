"""CasaSmart runtime component."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

ISSUER = "casasmart-hub"
ALGORITHM = "HS256"

CLOCK_SKEW = 30

ROLE_ADMIN = "admin"
ROLE_SUB_ADMIN = "sub-admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER)


SCOPE_WIDGET = "widget"
VALID_SCOPES = (SCOPE_WIDGET,)


class TokenError(Exception):
    """CasaSmart runtime component."""

    def __init__(self, message: str, code: str = "token_invalid") -> None:
        super().__init__(message)
        self.code = code


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, UnicodeDecodeError) as err:
        raise TokenError("Invalid base64url segment") from err


def _sign(secret: bytes, signing_input: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def issue_token(
    secret: bytes,
    device_id: str,
    role: str,
    rooms: list[str] | None,
    ttl: int,
    ver: int = 1,
    now: float | None = None,
    scope: str | None = None,
) -> str:
    """CasaSmart runtime component."""
    if not secret:
        raise ValueError("Empty signing secret")
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role {role!r}")
    if scope is not None and scope not in VALID_SCOPES:
        raise ValueError(f"Unknown scope {scope!r}")
    issued_at = int(now if now is not None else time.time())
    header = {"alg": ALGORITHM, "typ": "JWT"}
    claims = {
        "iss": ISSUER,
        "sub": device_id,
        "role": role,
        "rooms": rooms,
        "ver": int(ver),
        "iat": issued_at,
        "exp": issued_at + int(ttl),
        "jti": secrets.token_urlsafe(16),
    }
    if scope is not None:
        claims["scope"] = scope
    signing_input = (
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = _b64url_encode(_sign(secret, signing_input))
    return signing_input.decode("ascii") + "." + signature


def validate_token(
    secret: bytes, token: str, now: float | None = None
) -> dict[str, Any]:
    """CasaSmart runtime component."""
    if not secret:
        raise TokenError("Empty signing secret")
    if not isinstance(token, str):
        raise TokenError("Token must be a string")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("Token must have exactly 3 segments")
    header_b64, claims_b64, signature_b64 = parts

    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    expected = _sign(secret, signing_input)
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        raise TokenError("Signature mismatch")

    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(claims_b64))
    except (ValueError, UnicodeDecodeError) as err:
        raise TokenError("Malformed token JSON") from err

    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        raise TokenError("Unsupported algorithm")
    if not isinstance(claims, dict):
        raise TokenError("Claims must be an object")
    if claims.get("iss") != ISSUER:
        raise TokenError("Wrong issuer")
    if claims.get("role") not in VALID_ROLES:
        raise TokenError("Unknown role")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("Missing subject")
    rooms = claims.get("rooms")
    if rooms is not None and (
        not isinstance(rooms, list)
        or any(not isinstance(room, str) for room in rooms)
    ):
        raise TokenError("Malformed rooms claim")
    if not isinstance(claims.get("ver"), int):
        raise TokenError("Missing version claim")
    if "scope" in claims and claims["scope"] not in VALID_SCOPES:


        raise TokenError("Unknown scope claim")

    current = now if now is not None else time.time()
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise TokenError("Missing iat/exp")
    if current > exp + CLOCK_SKEW:
        raise TokenError("Token expired", code="token_expired")
    if iat > current + CLOCK_SKEW:
        raise TokenError("Token issued in the future")

    return claims


def unverified_subject(secret: bytes, token: str) -> str | None:
    """CasaSmart runtime component."""
    if not secret or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, claims_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    try:
        if not hmac.compare_digest(
            _sign(secret, signing_input), _b64url_decode(signature_b64)
        ):
            return None
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(claims_b64))
    except (TokenError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        return None
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER:
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    return sub


def generate_secret() -> str:
    """CasaSmart runtime component."""
    return secrets.token_hex(32)
