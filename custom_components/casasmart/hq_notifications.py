"""Authenticated, privacy-safe ingress for Hamdi HQ reminder notifications."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY = "hq_notification_public_key"
HQ_NOTIFICATION_PATH = "/api/casasmart/notifications/hq"
HQ_NOTIFICATION_MAX_SKEW_SECONDS = 60
HQ_NOTIFICATION_NONCE_RETENTION_SECONDS = 5 * 60
HQ_NOTIFICATION_MAX_NONCES = 1000
HQ_NOTIFICATION_MAX_AUDIT_ROWS = 500
HQ_NOTIFICATION_MAX_BODY_BYTES = 2048

_EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


class HqNotificationError(ValueError):
    """A request that must not reach the CasaSmart push dispatcher."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedHqNotification:
    """The deliberately tiny notification request accepted from HQ."""

    event_id: str
    nonce: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HqNotificationError("invalid_json")
        result[key] = value
    return result


def canonical_request(timestamp: int, nonce: str, raw_body: bytes) -> bytes:
    """Return the exact v1 request bytes signed by the independent HQ key."""

    digest = hashlib.sha256(raw_body).hexdigest()
    return (f"v1\nPOST\n{HQ_NOTIFICATION_PATH}\n{timestamp}\n{nonce}\n{digest}").encode(
        "ascii"
    )


def normalize_public_key(value: str | None) -> tuple[str, str]:
    """Validate one Ed25519 public key and return canonical PEM + fingerprint."""

    if not isinstance(value, str) or not value.strip():
        raise HqNotificationError("public_key_required")
    try:
        key = serialization.load_pem_public_key(value.encode("utf-8"))
    except (TypeError, ValueError) as err:
        raise HqNotificationError("invalid_public_key") from err
    if not isinstance(key, Ed25519PublicKey):
        raise HqNotificationError("invalid_public_key")
    raw = key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    pem = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return pem, hashlib.sha256(raw).hexdigest()[:16]


class HqNotificationVerifier:
    """Verify HQ requests and retain bounded replay/idempotency audit state."""

    def __init__(
        self, table: MutableMapping[str, Any], public_key_pem: str | None
    ) -> None:
        self._table = table
        try:
            canonical, _fingerprint = normalize_public_key(public_key_pem)
            key = serialization.load_pem_public_key(canonical.encode("ascii"))
        except HqNotificationError:
            key = None
        self._public_key = key if isinstance(key, Ed25519PublicKey) else None

    @property
    def configured(self) -> bool:
        return self._public_key is not None

    def verify(
        self, headers: Mapping[str, str], raw_body: bytes, now: float | None = None
    ) -> VerifiedHqNotification:
        """Authenticate and strictly decode a request without changing state."""

        if self._public_key is None:
            raise HqNotificationError("not_configured")
        if not raw_body or len(raw_body) > HQ_NOTIFICATION_MAX_BODY_BYTES:
            raise HqNotificationError("invalid_body_size")
        timestamp_raw = headers.get("X-CasaSmart-HQ-Timestamp", "")
        if not isinstance(timestamp_raw, str) or not timestamp_raw.isdigit():
            raise HqNotificationError("invalid_timestamp")
        timestamp = int(timestamp_raw)
        current = time.time() if now is None else now
        if abs(current - timestamp) > HQ_NOTIFICATION_MAX_SKEW_SECONDS:
            raise HqNotificationError("expired_request")
        nonce = headers.get("X-CasaSmart-HQ-Nonce", "")
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise HqNotificationError("invalid_nonce")
        signature_raw = headers.get("X-CasaSmart-HQ-Signature", "")
        try:
            signature = base64.b64decode(signature_raw, validate=True)
        except (TypeError, ValueError) as err:
            raise HqNotificationError("invalid_signature") from err
        if len(signature) != 64:
            raise HqNotificationError("invalid_signature")
        try:
            self._public_key.verify(
                signature, canonical_request(timestamp, nonce, raw_body)
            )
        except InvalidSignature as err:
            raise HqNotificationError("invalid_signature") from err
        try:
            payload = json.loads(
                raw_body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise HqNotificationError("invalid_json") from err
        if not isinstance(payload, dict) or set(payload) != {
            "event_id",
            "source_type",
            "target",
        }:
            raise HqNotificationError("invalid_payload")
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
            raise HqNotificationError("invalid_event_id")
        if payload.get("source_type") != "reminder" or payload.get("target") != "today":
            raise HqNotificationError("unsupported_notification")
        return VerifiedHqNotification(event_id=event_id, nonce=nonce)

    def reserve_nonce(self, nonce: str, now: float | None = None) -> None:
        """Atomically scoped by the caller's lock: prune, reject, then retain nonce."""

        current = int(time.time() if now is None else now)
        self._prune_nonces(current)
        key = f"nonce:{nonce}"
        if key in self._table:
            raise HqNotificationError("replayed_request")
        self._table[key] = {"at": current}
        self._prune_nonces(current)

    def previous(self, event_id: str) -> dict[str, Any] | None:
        value = self._table.get(f"event:{event_id}")
        return value if isinstance(value, dict) else None

    def record_delivery(
        self,
        event_id: str,
        outcome: str,
        reason: str | None = None,
        now: float | None = None,
    ) -> None:
        """Audit every attempt; retain only relay acceptance as terminal success."""

        current = int(time.time() if now is None else now)
        safe_outcome = (
            outcome
            if outcome
            in {
                "relay_accepted",
                "no_registered_tokens",
                "unavailable",
                "failed",
            }
            else "failed"
        )
        safe_reason = (
            reason
            if isinstance(reason, str)
            and re.fullmatch(r"[a-z0-9_]{1,80}", reason) is not None
            else None
        )
        if safe_outcome == "relay_accepted":
            self._table[f"event:{event_id}"] = {
                "outcome": safe_outcome,
                "at": current,
            }
        self._append_audit(
            {"event_id": event_id, "outcome": safe_outcome, "reason": safe_reason},
            current,
        )

    def record_rejection(self, code: str, now: float | None = None) -> None:
        """Retain a bounded, privacy-safe rejection audit without request content."""

        current = int(time.time() if now is None else now)
        safe_code = code if re.fullmatch(r"[a-z0-9_]{1,80}", code) else "rejected"
        self._append_audit({"outcome": "rejected", "reason": safe_code}, current)

    def record_duplicate(self, event_id: str, now: float | None = None) -> None:
        """Audit a successfully deduplicated retry without changing first acceptance."""

        current = int(time.time() if now is None else now)
        self._append_audit(
            {"event_id": event_id, "outcome": "duplicate", "reason": None},
            current,
        )

    def _append_audit(self, value: dict[str, Any], current: int) -> None:
        self._table[f"audit:{time.time_ns()}"] = {**value, "at": current}
        audit_keys = sorted(key for key in self._table if key.startswith("audit:"))
        for key in audit_keys[:-HQ_NOTIFICATION_MAX_AUDIT_ROWS]:
            del self._table[key]

    def _prune_nonces(self, current: int) -> None:
        nonce_rows: list[tuple[str, int]] = []
        for key, value in self._table.items():
            if not key.startswith("nonce:"):
                continue
            at = value.get("at", 0) if isinstance(value, dict) else 0
            if (
                not isinstance(at, int)
                or at < current - HQ_NOTIFICATION_NONCE_RETENTION_SECONDS
            ):
                del self._table[key]
            else:
                nonce_rows.append((key, at))
        overflow = len(nonce_rows) - HQ_NOTIFICATION_MAX_NONCES
        for key, _at in sorted(nonce_rows, key=lambda item: (item[1], item[0]))[
            : max(0, overflow)
        ]:
            del self._table[key]
