"""Hub-issued JWTs: mint and validate.

The hub is the only issuer and the only validator, so this small stdlib
module accepts one format (HS256, our issuer, our claim shape) and rejects
everything else; there is no algorithm negotiation to get wrong. Claims are
iss, sub (device id), role, rooms (None for all rooms), ver (the device's
auth version, checked by the engine so edits revoke tokens at once), iat,
exp, jti, an optional scope ("widget" for the home-screen widget token) and
an optional camera_access (none / rooms / all; a token without it keeps the
pre-2.4 behaviour, cameras following the room scope).
"""

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
# Seconds of clock drift tolerated on the iat and exp checks.
CLOCK_SKEW = 30

ROLE_ADMIN = "admin"
ROLE_SUB_ADMIN = "sub-admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER)

# The only non-default scope a hub token can carry (home-screen widgets).
SCOPE_WIDGET = "widget"
VALID_SCOPES = (SCOPE_WIDGET,)

# A member's camera access: no cameras at all, only the cameras in the rooms
# the member can see (which for an unscoped member is every camera), or every
# camera whatever the room scope. The admin always has every camera.
CAMERA_ACCESS_NONE = "none"
CAMERA_ACCESS_ROOMS = "rooms"
CAMERA_ACCESS_ALL = "all"
VALID_CAMERA_ACCESS = (CAMERA_ACCESS_NONE, CAMERA_ACCESS_ROOMS, CAMERA_ACCESS_ALL)


def effective_camera_access(camera_access: Any, rooms: Any) -> str:
    """The camera access a record or token grants.

    A record from before camera access existed has no value, and keeps the
    behaviour it had: cameras follow the room scope, so an unscoped member
    sees every camera and a scoped one the cameras in their rooms. No member
    is locked out by the upgrade.
    """
    if camera_access in VALID_CAMERA_ACCESS:
        return camera_access
    return CAMERA_ACCESS_ALL if rooms is None else CAMERA_ACCESS_ROOMS


class TokenError(Exception):
    """The token is malformed, forged, expired, or not ours.

    code is the reason sent to the app in the 401 body: token_invalid and
    token_expired call for a fresh login, token_stale means the device was
    edited and should log in again, and unenrolled is the only one that means
    the device must pair again.
    """

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
    camera_access: str | None = None,
) -> str:
    """Mint a signed token for an authenticated device.

    camera_access is written as a claim when given; the engine reads the
    device's current value from its cache on every check anyway, so the
    claim is informational (it lets a holder know its own access).
    """
    if not secret:
        raise ValueError("Empty signing secret")
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role {role!r}")
    if scope is not None and scope not in VALID_SCOPES:
        raise ValueError(f"Unknown scope {scope!r}")
    if camera_access is not None and camera_access not in VALID_CAMERA_ACCESS:
        raise ValueError(f"Unknown camera access {camera_access!r}")
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
    if camera_access is not None:
        claims["camera_access"] = camera_access
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
    """Verify the signature and claims; return the claims or raise TokenError.

    The signature is checked first, in constant time, so claims are never
    parsed from unauthenticated input. Any other input, non-ASCII strings
    included, raises TokenError and nothing else.
    """
    if not secret:
        raise TokenError("Empty signing secret")
    if not isinstance(token, str):
        raise TokenError("Token must be a string")
    if not token.isascii():
        raise TokenError("Token must be ASCII")
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
        not isinstance(rooms, list) or any(not isinstance(room, str) for room in rooms)
    ):
        raise TokenError("Malformed rooms claim")
    if not isinstance(claims.get("ver"), int):
        raise TokenError("Missing version claim")
    if "scope" in claims and claims["scope"] not in VALID_SCOPES:
        # Reject an unknown scope so it can't validate as an unscoped token.
        raise TokenError("Unknown scope claim")
    if "camera_access" in claims and claims["camera_access"] not in VALID_CAMERA_ACCESS:
        raise TokenError("Malformed camera_access claim")

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
    """The device id of a token this hub signed, ignoring expiry and version.

    Returns None for anything forged or malformed. Only for /auth/whoami,
    which asks whether the device is still paired: an expired token still
    proves which device holds it. Never use it to grant access.
    """
    if not secret or not isinstance(token, str) or not token.isascii():
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
    """A fresh 256-bit signing secret, hex-encoded for the config store."""
    return secrets.token_hex(32)
