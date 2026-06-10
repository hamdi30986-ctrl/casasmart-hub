"""Hub-issued JWT layer (Track B — B1.6): mint + validate, nothing else.

Pure stdlib (hmac/hashlib/base64/json) so the token rules are
unit-testable without an HA install, exactly like ``ws_protocol`` and
``entity_bridge``.

Why hand-rolled and not PyJWT: the hub is BOTH the issuer and the only
validator — there is no third party, no interop, and therefore no reason
to accept anything but our own narrow format. This module hard-rejects
everything that isn't ``HS256`` + our issuer + our claim shape, which is
a smaller attack surface than a general-purpose JWT library with
algorithm negotiation (the classic ``alg: none`` family of bugs can't
exist here by construction).

Token shape (claims):

- ``iss`` — always ``casasmart-hub``; anything else is rejected.
- ``sub`` — the device id the token was issued to.
- ``role`` — ``admin`` / ``sub-admin`` / ``user`` (plan: 3-tier model).
- ``rooms`` — list of area ids the subject is scoped to, or ``None`` for
  unrestricted (plan: per-user ``allowedRoomIds`` toggle).
- ``iat`` / ``exp`` — issued-at / expiry, epoch seconds. Validation
  allows ``CLOCK_SKEW`` seconds of slack both ways (plan: JWT expiry math
  must survive small clock drift; the real NTP gate is a separate block).
- ``jti`` — unique token id (random), for future revocation lists.
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
# Seconds of clock slack tolerated on iat/exp checks (NTP gate is B-clock).
CLOCK_SKEW = 30

ROLE_ADMIN = "admin"
ROLE_SUB_ADMIN = "sub-admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER)


class TokenError(Exception):
    """The token is malformed, forged, expired, or not ours."""


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
    now: float | None = None,
) -> str:
    """Mint a signed token for an authenticated device."""
    if not secret:
        raise ValueError("Empty signing secret")
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role {role!r}")
    issued_at = int(now if now is not None else time.time())
    header = {"alg": ALGORITHM, "typ": "JWT"}
    claims = {
        "iss": ISSUER,
        "sub": device_id,
        "role": role,
        "rooms": rooms,
        "iat": issued_at,
        "exp": issued_at + int(ttl),
        "jti": secrets.token_urlsafe(16),
    }
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
    """Verify signature + claims; return the claims dict or raise TokenError.

    Signature is checked FIRST (constant-time) so claim parsing never runs
    on unauthenticated input.
    """
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

    current = now if now is not None else time.time()
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise TokenError("Missing iat/exp")
    if current > exp + CLOCK_SKEW:
        raise TokenError("Token expired")
    if iat > current + CLOCK_SKEW:
        raise TokenError("Token issued in the future")

    return claims


def generate_secret() -> str:
    """A fresh 256-bit signing secret, hex-encoded for the config store."""
    return secrets.token_hex(32)
