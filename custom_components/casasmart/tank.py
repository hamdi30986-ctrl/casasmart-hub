"""Shelly tank engine (post-funeral mini-block MB-1) — the pure half.

Replaces the Phase 4 tank-cloud casualty: the Shelly's old script POSTed
voltage readings to Supabase 24/7; the funeral deleted the cloud leg,
leaving readings app-open-only. MB-1 restores 24/7 history with the hub
as the destination: the Shelly POSTs to ``POST /api/casasmart/tank/
reading`` and the HUB writes the monitoring script to the device itself
over Gen2 scripting RPC — no hand-edited scripts, no hardcoded IPs in
the app (the plan's hard rule).

This module is the flat-importable engine (stdlib only, no HA imports —
unit-tests on a temp SQLite file like ``registry.py``/``auth_engine.py``):

- ``TankEngine`` — tank device records (name, ip, hashed token) + the
  bounded per-device readings log. Tokens are minted here
  (``secrets.token_hex``), stored SHA-256-hashed (the plaintext exists
  exactly once: baked into the script pushed to the Shelly — same
  posture as pairing codes). Re-provisioning mints a fresh token, which
  kills the old one on the next POST.
- ``build_tank_script`` — generates the mJS script the provisioner
  pushes. Mirrors what the Supabase-era script on the live Shelly did
  (read ``Voltmeter`` component, POST ``{device_token, voltage}`` every
  5 minutes + once on start), with the hub ingest URL + token baked in
  via JSON encoding so no value can break out of the string literal.
- ``chunk_script_code`` — Gen2 ``Script.PutCode`` bodies are size-capped;
  the provisioner uploads the script in append-mode chunks.

Storage-touching methods are synchronous (call via executor). Readings
are one JSON blob per device, bounded by count and age — at the script's
5-minute cadence the cap holds ~31 days of 24/7 data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The script name on the device — the provisioner replaces a script with
# this name and the delete path cleans it up. Matches the app's legacy
# `tankWebhookScriptName` so re-provisioning a Supabase-era device
# replaces the old cloud script instead of leaving it running beside us.
TANK_SCRIPT_NAME = "CasaSmart"
# Shelly Plus Uni analog voltmeter component (the tank sensor input).
TANK_VOLTMETER_ID = 100
# Reading cadence — same 5 minutes the Supabase-era script used.
TANK_PUSH_INTERVAL_SECONDS = 300
# Gen2 Script.PutCode caps the per-call code size; 1024 is the safe
# chunk used by every reference implementation.
SCRIPT_CHUNK_SIZE = 1024
# hub_config key: full override for the ingest URL baked into scripts.
# Default is http://<hub-lan-ip>:<ha-port>/... — deployments whose
# LAN-visible address differs from what the hub can see (the dev rig's
# Docker port proxy) set this; production HAOS/LXC never needs it.
TANK_INGEST_URL_CONFIG_KEY = "tank_ingest_url"

_NAME_MAX = 64
# 5-minute cadence -> 288/day; 9000 ≈ 31 days of 24/7 readings.
_MAX_READINGS = 9000
_RETENTION_SECONDS = 31 * 24 * 3600


class TankError(Exception):
    """Tank input rejected (maps to HTTP 400)."""


class UnknownTankError(TankError):
    """No tank device under that id (maps to HTTP 404)."""


class UnknownTokenError(Exception):
    """Ingest token didn't match any device (maps to HTTP 401, generic)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TankError("Tank name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise TankError(f"Tank name is too long (max {_NAME_MAX})")
    return cleaned


def build_tank_script(
    ingest_url: str,
    device_token: str,
    voltmeter_id: int = TANK_VOLTMETER_ID,
    interval_seconds: int = TANK_PUSH_INTERVAL_SECONDS,
) -> str:
    """The mJS monitoring script pushed to the Shelly.

    Same behavior as the Supabase-era reference script (read the
    Voltmeter, POST ``{device_token, voltage}``, repeat every 5 min,
    once immediately on start) with the hub as the destination. URL and
    token are emitted through ``json.dumps`` so arbitrary config values
    can never escape the mJS string literal.
    """
    if not isinstance(ingest_url, str) or not ingest_url.startswith(
        ("http://", "https://")
    ):
        raise TankError(f"Ingest URL must be http(s), got {ingest_url!r}")
    if not isinstance(device_token, str) or not device_token:
        raise TankError("Device token is required")
    if not isinstance(interval_seconds, int) or interval_seconds < 60:
        raise TankError("Push interval must be at least 60 seconds")
    url = json.dumps(ingest_url)
    token = json.dumps(device_token)
    return (
        "// CasaSmart tank monitor — provisioned by the hub, do not edit.\n"
        f"let C={{url:{url},token:{token},vm:{int(voltmeter_id)},"
        f"sec:{int(interval_seconds)}}};\n"
        "function push(){\n"
        '  let v=Shelly.getComponentStatus("Voltmeter",C.vm);\n'
        "  if(!v||typeof v.voltage!==\"number\")return;\n"
        '  Shelly.call("HTTP.Request",{method:"POST",url:C.url,'
        "body:JSON.stringify({device_token:C.token,voltage:v.voltage}),"
        'content_type:"application/json",timeout:15},'
        "function(r,e){if(e!==0){print(\"CasaSmart push fail: \"+e);}"
        "else if(r&&r.code>=300){print(\"CasaSmart push HTTP \"+r.code);}});\n"
        "}\n"
        "push();Timer.set(C.sec*1000,true,push);\n"
    )


def chunk_script_code(code: str, chunk_size: int = SCRIPT_CHUNK_SIZE) -> list[str]:
    """Split script code into ``Script.PutCode``-sized pieces (>=1 chunk).

    Splits on UTF-8 BYTE length (the device-side cap), never inside a
    multi-byte sequence — the generated script is ASCII today, but a
    future name/comment must not be able to corrupt the upload.
    """
    if chunk_size <= 0:
        raise TankError("chunk_size must be positive")
    encoded = code.encode("utf-8")
    if not encoded:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + chunk_size, len(encoded))
        # Back off a UTF-8 continuation byte boundary.
        while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
    return chunks


class TankEngine:
    """Tank device records + bounded readings log over storage tables."""

    def __init__(self, devices_table: Any, readings_table: Any) -> None:
        self._devices = devices_table
        self._readings = readings_table
        # Serializes storage mutations (held across SQLite I/O), same
        # posture as RegistryEngine.
        self._lock = threading.RLock()

    # -- devices (storage — call via executor) ---------------------------------

    def mint_device(
        self, device_id: Any, name: Any, ip: Any, model: Any = None
    ) -> tuple[dict[str, Any], str]:
        """Create/replace a tank device record and mint its ingest token.

        Returns ``(public_record, plaintext_token)`` — the plaintext is
        never stored; the caller bakes it into the device script. A
        re-provision (same ``device_id``) keeps ``created_at`` but mints
        a fresh token, killing the old one on its next POST.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise TankError("device_id is required")
        device_id = device_id.strip().lower()
        if not isinstance(ip, str) or not ip.strip():
            raise TankError("ip is required")
        token = secrets.token_hex(16)
        now = int(time.time())
        with self._lock:
            existing = self._devices.get(device_id)
            record = {
                "name": _clean_name(name),
                "ip": ip.strip(),
                "model": model if isinstance(model, str) and model else None,
                "token_sha256": _hash_token(token),
                "created_at": (existing or {}).get("created_at", now),
                "provisioned_at": now,
            }
            self._devices[device_id] = record
        _LOGGER.info("Tank %s provisioned (%s @ %s)", device_id, record["name"], ip)
        return self._public(device_id, record), token

    def list_devices(self) -> list[dict[str, Any]]:
        """Every tank device with its last reading — never the token hash."""
        with self._lock:
            return [
                self._public(device_id, record)
                for device_id, record in self._devices.items()
            ]

    def get_device(self, device_id: str) -> dict[str, Any]:
        record = self._devices.get(device_id)
        if record is None:
            raise UnknownTankError("Unknown tank device")
        return self._public(device_id, record)

    def delete_device(self, device_id: str) -> None:
        """Drop the device and its readings (token dies with the record)."""
        with self._lock:
            try:
                del self._devices[device_id]
            except KeyError:
                raise UnknownTankError("Unknown tank device") from None
            self._readings.pop(device_id, None)
        _LOGGER.info("Tank %s deleted", device_id)

    # -- ingest (storage — call via executor) ----------------------------------

    def ingest(self, token: Any, voltage: Any) -> str:
        """Record one reading for the device matching ``token``.

        Returns the device id. Raises ``UnknownTokenError`` on a token
        that matches nothing (the view answers a generic 401) and
        ``TankError`` on a malformed voltage.
        """
        if not isinstance(token, str) or not token:
            raise UnknownTokenError
        if isinstance(voltage, bool) or not isinstance(voltage, (int, float)):
            raise TankError("voltage must be a number")
        token_hash = _hash_token(token)
        with self._lock:
            device_id = None
            for candidate_id, record in self._devices.items():
                if hmac.compare_digest(
                    record.get("token_sha256", ""), token_hash
                ):
                    device_id = candidate_id
                    break
            if device_id is None:
                raise UnknownTokenError
            entries = (self._readings.get(device_id) or {}).get("entries", [])
            entries.append({"t": int(time.time()), "v": float(voltage)})
            self._prune(entries)
            self._readings[device_id] = {"entries": entries}
        return device_id

    def recent_readings(self, device_id: str, days: Any = 7) -> list[dict[str, Any]]:
        """Readings from the last ``days`` days, NEWEST first (the app's
        ``fetchRecentReadings`` contract). Raises on an unknown device —
        "no tank" and "no data yet" must stay distinguishable."""
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise TankError("days must be a positive integer")
        with self._lock:
            if device_id not in self._devices:
                raise UnknownTankError("Unknown tank device")
            entries = (self._readings.get(device_id) or {}).get("entries", [])
        cutoff = time.time() - days * 24 * 3600
        return [entry for entry in reversed(entries) if entry["t"] >= cutoff]

    def last_reading(self, device_id: str) -> dict[str, Any] | None:
        """The newest reading, or None (also None for unknown devices —
        the provision wait loop polls this before the record is hot)."""
        with self._lock:
            entries = (self._readings.get(device_id) or {}).get("entries", [])
        return entries[-1] if entries else None

    # -- internals ---------------------------------------------------------------

    def _public(self, device_id: str, record: dict[str, Any]) -> dict[str, Any]:
        entries = (self._readings.get(device_id) or {}).get("entries", [])
        return {
            "device_id": device_id,
            "name": record.get("name"),
            "ip": record.get("ip"),
            "model": record.get("model"),
            "created_at": record.get("created_at", 0),
            "provisioned_at": record.get("provisioned_at", 0),
            "last_reading": entries[-1] if entries else None,
        }

    @staticmethod
    def _prune(entries: list[dict[str, Any]]) -> None:
        cutoff = time.time() - _RETENTION_SECONDS
        entries[:] = [entry for entry in entries if entry.get("t", 0) >= cutoff]
        if len(entries) > _MAX_READINGS:
            del entries[: len(entries) - _MAX_READINGS]
