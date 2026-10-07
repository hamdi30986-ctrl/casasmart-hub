"""Water-tank engine: Shelly tank devices, readings and calibration.

A tank is a Gen2+ Shelly with a voltmeter on a water-level sensor. When the
hub provisions one (tank_api), it mints the device record with a random
ingest token, stores only the token's SHA-256, and uploads a small mJS script
that posts the voltmeter reading with that token every few minutes. This
module stores the devices and their readings (kept for 31 days), converts a
voltage to a water-level percent using the tank's calibration, and reports
the status read by the app and by the daily low-water check.

Like the other engines it is stdlib only. Storage-touching methods are
synchronous (call them via the executor) and serialized by an RLock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


# Name of the monitoring script on the Shelly; a re-provision replaces the
# script by this name.
TANK_SCRIPT_NAME = "CasaSmart"
# Component id of the Shelly's voltmeter the script reads.
TANK_VOLTMETER_ID = 100
# How often the script posts a reading.
TANK_PUSH_INTERVAL_SECONDS = 300
# Bytes per Script.PutCode call when uploading the script.
SCRIPT_CHUNK_SIZE = 1024
# hub_config key that overrides the URL the Shelly posts readings to.
TANK_INGEST_URL_CONFIG_KEY = "tank_ingest_url"

_NAME_MAX = 64
# Readings older than this are pruned on the device's next ingest.
_RETENTION_SECONDS = 31 * 24 * 3600

# Defaults for a newly provisioned tank: height in metres, low-water alert
# threshold in percent.
TANK_MAX_HEIGHT_DEFAULT = 3.0
TANK_LOW_PERCENT_DEFAULT = 20
# The range the app's low-water slider offers.
TANK_LOW_PERCENT_MIN = 1
TANK_LOW_PERCENT_MAX = 30


class TankError(Exception):
    """Tank input rejected (maps to HTTP 400)."""


class UnknownTankError(TankError):
    """No tank device under that id (maps to HTTP 404)."""


class DuplicateTankError(TankError):
    """Raised when provisioning would replace an existing tank/token."""


class UnknownTokenError(Exception):
    """Ingest token didn't match any device (maps to HTTP 401, generic)."""


# -- helpers ------------------------------------------------------------------


def _finite_float(value: Any, field: str) -> float:
    """value as a finite float, or a TankError naming the field.

    NaN or infinity would make every later read invalid JSON.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TankError(f"{field} must be a number")
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        number = math.inf
    if not math.isfinite(number):
        raise TankError(f"{field} must be a finite number")
    return number


def _coerce_positive(value: Any, field: str) -> float:
    """A finite, strictly-positive float, or a TankError naming the field."""
    number = _finite_float(value, field)
    if number <= 0:
        raise TankError(f"{field} must be greater than 0")
    return number


def _compute_percent(
    voltage: float, cal_v: float, cal_d: float, height: float
) -> float | None:
    """Water level in percent, or None if the calibration is unusable.

    The full-tank voltage is height * cal_v / cal_d, and the result is
    clamped to 0-100. Shared by the live status and the reading history.
    """
    if cal_v <= 0 or cal_d <= 0 or height <= 0:
        return None
    max_voltage = height * (cal_v / cal_d)
    if max_voltage <= 0:
        return None
    return max(0.0, min(100.0, (float(voltage) / max_voltage) * 100.0))


def _coerce_low_percent(value: Any) -> int:
    """A whole-number low-water threshold in the slider's range, else TankError.

    The slider may send 20.0, so integral floats are accepted.
    """
    if isinstance(value, bool):
        raise TankError("low_percent must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise TankError("low_percent must be a whole number")
        value = int(value)
    if not isinstance(value, int):
        raise TankError("low_percent must be an integer")
    if not TANK_LOW_PERCENT_MIN <= value <= TANK_LOW_PERCENT_MAX:
        raise TankError(
            f"low_percent must be between {TANK_LOW_PERCENT_MIN} and "
            f"{TANK_LOW_PERCENT_MAX}"
        )
    return value


def _hash_token(token: str) -> str:
    """The stored form of an ingest token (hex SHA-256)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_name(name: Any) -> str:
    """A required, stripped, length-capped tank name."""
    if not isinstance(name, str) or not name.strip():
        raise TankError("Tank name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise TankError(f"Tank name is too long (max {_NAME_MAX})")
    return cleaned


# -- Shelly script ------------------------------------------------------------


def build_tank_script(
    ingest_url: str,
    device_token: str,
    voltmeter_id: int = TANK_VOLTMETER_ID,
    interval_seconds: int = TANK_PUSH_INTERVAL_SECONDS,
) -> str:
    """The mJS monitoring script for the Shelly.

    It posts {device_token, voltage} to the hub on start and then every
    interval_seconds. The URL and token go through json.dumps, so they can't
    break out of their string literals.
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
        '  if(!v||typeof v.voltage!=="number")return;\n'
        '  Shelly.call("HTTP.Request",{method:"POST",url:C.url,'
        "body:JSON.stringify({device_token:C.token,voltage:v.voltage}),"
        'content_type:"application/json",timeout:15},'
        'function(r,e){if(e!==0){print("CasaSmart push fail: "+e);}'
        'else if(r&&r.code>=300){print("CasaSmart push HTTP "+r.code);}});\n'
        "}\n"
        "push();Timer.set(C.sec*1000,true,push);\n"
    )


def chunk_script_code(code: str, chunk_size: int = SCRIPT_CHUNK_SIZE) -> list[str]:
    """Split script code into Script.PutCode chunks (at least one).

    The size limit is in UTF-8 bytes, and no chunk ends inside a multi-byte
    character.
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
        # Don't split a multi-byte character.
        while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
    return chunks


# -- engine -------------------------------------------------------------------


class TankEngine:
    """Tank devices (devices_table) and their readings (readings).

    A device record holds the name, IP, model, the ingest token's hash and
    the calibration; _public is the shape the API serves, without the hash.
    """

    def __init__(self, devices_table: Any, readings: Any) -> None:
        self._devices = devices_table
        # A TankReadingsTable: one row per reading.
        self._readings = readings
        # Held across storage I/O.
        self._lock = threading.RLock()

    def mint_device(
        self, device_id: Any, name: Any, ip: Any, model: Any = None
    ) -> tuple[dict[str, Any], str]:
        """Register a new tank and return (public_record, token).

        The plaintext token is returned once, for the Shelly's script; only
        its hash is stored. The id is lower-cased. Raises DuplicateTankError
        if the tank is already registered.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise TankError("device_id is required")
        device_id = device_id.strip().lower()
        if not isinstance(ip, str) or not ip.strip():
            raise TankError("ip is required")
        now = int(time.time())
        with self._lock:
            # A second provision of the same tank is refused (HTTP 409): after
            # a hub IP change the supported path is delete + re-add.
            if self._devices.get(device_id) is not None:
                raise DuplicateTankError("Tank is already registered")
            token = secrets.token_hex(16)
            record = {
                "name": _clean_name(name),
                "ip": ip.strip(),
                "model": model if isinstance(model, str) and model else None,
                "token_sha256": _hash_token(token),
                "created_at": now,
                "provisioned_at": now,
                "calibration_voltage": 0.0,
                "calibration_depth": 0.0,
                "max_height": TANK_MAX_HEIGHT_DEFAULT,
                "low_percent": TANK_LOW_PERCENT_DEFAULT,
            }
            self._devices[device_id] = record
        _LOGGER.info("Tank %s provisioned (%s @ %s)", device_id, record["name"], ip)
        return self._public(device_id, record), token

    def list_devices(self) -> list[dict[str, Any]]:
        """Every tank with its last reading (never the token hash)."""
        with self._lock:
            return [
                self._public(device_id, record)
                for device_id, record in self._devices.items()
            ]

    def get_device(self, device_id: str) -> dict[str, Any]:
        """One tank's public record; UnknownTankError if there is none."""
        record = self._devices.get(device_id)
        if record is None:
            raise UnknownTankError("Unknown tank device")
        return self._public(device_id, record)

    def delete_device(self, device_id: str, *, token: str | None = None) -> None:
        """Delete a tank and its readings, which also invalidates its token.

        With token, only a record minted with that token is deleted, so a
        failed provision can undo its own mint without touching a newer one.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None or (
                token is not None and record.get("token_sha256") != _hash_token(token)
            ):
                raise UnknownTankError("Unknown tank device")
            del self._devices[device_id]
            self._readings.delete_device(device_id)
        _LOGGER.info("Tank %s deleted", device_id)

    def set_calibration(
        self,
        device_id: str,
        *,
        calibration_voltage: Any = None,
        calibration_depth: Any = None,
        max_height: Any = None,
        low_percent: Any = None,
    ) -> dict[str, Any]:
        """Update any of the calibration fields and the low-water threshold.

        None leaves a field unchanged, so the app's calibration dialog and its
        low-water slider share this method. Raises UnknownTankError for an
        unknown device and TankError for a bad value.
        """
        updates: dict[str, Any] = {}
        if calibration_voltage is not None:
            updates["calibration_voltage"] = _coerce_positive(
                calibration_voltage, "calibration_voltage"
            )
        if calibration_depth is not None:
            updates["calibration_depth"] = _coerce_positive(
                calibration_depth, "calibration_depth"
            )
        if max_height is not None:
            updates["max_height"] = _coerce_positive(max_height, "max_height")
        if low_percent is not None:
            updates["low_percent"] = _coerce_low_percent(low_percent)
        if not updates:
            raise TankError("No calibration fields provided")

        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            merged = {**record, **updates}
            # Catches swapped depth and height values.
            depth = merged.get("calibration_depth", 0.0) or 0.0
            height = merged.get("max_height", 0.0) or 0.0
            if depth > 0 and height > 0 and depth > height:
                raise TankError("calibration_depth cannot exceed max_height")
            self._devices[device_id] = merged
        _LOGGER.info("Tank %s calibration updated: %s", device_id, sorted(updates))
        return self._public(device_id, merged)

    def voltage_to_percent(self, device_id: str, voltage: Any) -> float | None:
        """Convert a voltage to a water-level percent with the stored calibration.

        Returns None for an uncalibrated tank, so the app shows no value
        instead of 0%. Raises UnknownTankError for an unknown device and
        TankError for a non-numeric voltage.
        """
        if isinstance(voltage, bool) or not isinstance(voltage, (int, float)):
            raise TankError("voltage must be a number")
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            cal_v = record.get("calibration_voltage", 0.0) or 0.0
            cal_d = record.get("calibration_depth", 0.0) or 0.0
            height = record.get("max_height", TANK_MAX_HEIGHT_DEFAULT) or 0.0
        return _compute_percent(float(voltage), cal_v, cal_d, height)

    def status(self, device_id: str) -> dict[str, Any]:
        """Live status for the app and the low-water check.

        voltage is None without a reading, percent also when uncalibrated, and
        is_low is true only for a computed percent below the threshold. Raises
        UnknownTankError for an unknown device.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            low_percent = int(record.get("low_percent", TANK_LOW_PERCENT_DEFAULT))
            last = self._readings.last(device_id)
        voltage = float(last["v"]) if last else None
        percent = (
            self.voltage_to_percent(device_id, voltage) if voltage is not None else None
        )
        return {
            "device_id": device_id,
            "voltage": voltage,
            "percent": percent,
            "low_percent": low_percent,
            "is_low": percent is not None and percent < low_percent,
            "last_reading": last,
        }

    def ingest(self, token: Any, voltage: Any) -> str:
        """Record one reading for the device that token belongs to.

        Returns the device id. Raises UnknownTokenError for a token that
        matches nothing and TankError for a malformed voltage.
        """
        if not isinstance(token, str) or not token:
            raise UnknownTokenError
        voltage = _finite_float(voltage, "voltage")
        token_hash = _hash_token(token)
        with self._lock:
            device_id = None
            for candidate_id, record in self._devices.items():
                if hmac.compare_digest(record.get("token_sha256", ""), token_hash):
                    device_id = candidate_id
                    break
            if device_id is None:
                raise UnknownTokenError
            # Keep t increasing across a clock step-back (an NTP fix after a
            # power cut), so history stays ordered and (device_id, t) unique.
            now = int(time.time())
            last_t = self._readings.latest_t(device_id)
            t = now if last_t is None or now > last_t else last_t + 1
            self._readings.append(device_id, t, voltage)
            self._readings.prune(device_id, now - _RETENTION_SECONDS)
        return device_id

    def recent_readings(self, device_id: str, days: Any = 7) -> list[dict[str, Any]]:
        """The last ``days`` days of readings, newest first, as {t, v, p}.

        p is the percent from the current calibration (None if uncalibrated).
        An unknown tank raises, so "no tank" stays distinct from "no data yet".
        """
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise TankError("days must be a positive integer")
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            cal_v = record.get("calibration_voltage", 0.0) or 0.0
            cal_d = record.get("calibration_depth", 0.0) or 0.0
            height = record.get("max_height", TANK_MAX_HEIGHT_DEFAULT) or 0.0
            # Clamped at the epoch: no reading is older, and a huge days value
            # would otherwise overflow SQLite's 64-bit INTEGER.
            cutoff = max(0, int(time.time()) - days * 24 * 3600)
            entries = self._readings.recent(device_id, cutoff)
        return [
            {
                "t": entry["t"],
                "v": entry["v"],
                "p": _compute_percent(entry["v"], cal_v, cal_d, height),
            }
            for entry in entries
        ]

    def last_reading(self, device_id: str) -> dict[str, Any] | None:
        """The newest reading, or None (also for an unknown device)."""
        return self._readings.last(device_id)

    def _public(self, device_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """The served shape of a tank record, with its newest reading."""
        last = self._readings.last(device_id)
        cal_v = record.get("calibration_voltage", 0.0) or 0.0
        cal_d = record.get("calibration_depth", 0.0) or 0.0
        return {
            "device_id": device_id,
            "name": record.get("name"),
            "ip": record.get("ip"),
            "model": record.get("model"),
            "created_at": record.get("created_at", 0),
            "provisioned_at": record.get("provisioned_at", 0),
            # The app reads the calibration back to refill its form.
            "calibration_voltage": cal_v,
            "calibration_depth": cal_d,
            "max_height": record.get("max_height", TANK_MAX_HEIGHT_DEFAULT),
            "low_percent": int(record.get("low_percent", TANK_LOW_PERCENT_DEFAULT)),
            "is_calibrated": cal_v > 0 and cal_d > 0,
            "last_reading": last,
        }
