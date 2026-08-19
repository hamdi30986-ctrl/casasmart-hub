"""CasaSmart runtime component."""

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





TANK_SCRIPT_NAME = "CasaSmart"

TANK_VOLTMETER_ID = 100

TANK_PUSH_INTERVAL_SECONDS = 300


SCRIPT_CHUNK_SIZE = 1024




TANK_INGEST_URL_CONFIG_KEY = "tank_ingest_url"

_NAME_MAX = 64

_RETENTION_SECONDS = 31 * 24 * 3600






TANK_MAX_HEIGHT_DEFAULT = 3.0
TANK_LOW_PERCENT_DEFAULT = 20


TANK_LOW_PERCENT_MIN = 1
TANK_LOW_PERCENT_MAX = 30


def _coerce_positive(value: Any, field: str) -> float:
    """CasaSmart runtime component."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TankError(f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise TankError(f"{field} must be a finite number")
    if number <= 0:
        raise TankError(f"{field} must be greater than 0")
    return number


def _compute_percent(
    voltage: float, cal_v: float, cal_d: float, height: float
) -> float | None:
    """CasaSmart runtime component."""
    if cal_v <= 0 or cal_d <= 0 or height <= 0:
        return None
    max_voltage = height * (cal_v / cal_d)
    if max_voltage <= 0:
        return None
    return max(0.0, min(100.0, (float(voltage) / max_voltage) * 100.0))


def _coerce_low_percent(value: Any) -> int:
    """CasaSmart runtime component."""
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


class TankError(Exception):
    """CasaSmart runtime component."""


class UnknownTankError(TankError):
    """CasaSmart runtime component."""


class UnknownTokenError(Exception):
    """CasaSmart runtime component."""


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
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    if chunk_size <= 0:
        raise TankError("chunk_size must be positive")
    encoded = code.encode("utf-8")
    if not encoded:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + chunk_size, len(encoded))

        while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
    return chunks


class TankEngine:
    """CasaSmart runtime component."""

    def __init__(self, devices_table: Any, readings: Any) -> None:
        self._devices = devices_table


        self._readings = readings


        self._lock = threading.RLock()



    def mint_device(
        self, device_id: Any, name: Any, ip: Any, model: Any = None
    ) -> tuple[dict[str, Any], str]:
        """CasaSmart runtime component."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise TankError("device_id is required")
        device_id = device_id.strip().lower()
        if not isinstance(ip, str) or not ip.strip():
            raise TankError("ip is required")
        token = secrets.token_hex(16)
        now = int(time.time())
        with self._lock:
            existing = self._devices.get(device_id) or {}
            record = {
                "name": _clean_name(name),
                "ip": ip.strip(),
                "model": model if isinstance(model, str) and model else None,
                "token_sha256": _hash_token(token),
                "created_at": existing.get("created_at", now),
                "provisioned_at": now,



                "calibration_voltage": existing.get("calibration_voltage", 0.0),
                "calibration_depth": existing.get("calibration_depth", 0.0),
                "max_height": existing.get("max_height", TANK_MAX_HEIGHT_DEFAULT),
                "low_percent": existing.get(
                    "low_percent", TANK_LOW_PERCENT_DEFAULT
                ),
            }
            self._devices[device_id] = record
        _LOGGER.info("Tank %s provisioned (%s @ %s)", device_id, record["name"], ip)
        return self._public(device_id, record), token

    def list_devices(self) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._lock:
            try:
                del self._devices[device_id]
            except KeyError:
                raise UnknownTankError("Unknown tank device") from None
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
        """CasaSmart runtime component."""
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


            depth = merged.get("calibration_depth", 0.0) or 0.0
            height = merged.get("max_height", 0.0) or 0.0
            if depth > 0 and height > 0 and depth > height:
                raise TankError("calibration_depth cannot exceed max_height")
            self._devices[device_id] = merged
        _LOGGER.info("Tank %s calibration updated: %s", device_id, sorted(updates))
        return self._public(device_id, merged)

    def voltage_to_percent(
        self, device_id: str, voltage: Any
    ) -> float | None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            low_percent = int(
                record.get("low_percent", TANK_LOW_PERCENT_DEFAULT)
            )
            last = self._readings.last(device_id)
        voltage = float(last["v"]) if last else None
        percent = (
            self.voltage_to_percent(device_id, voltage)
            if voltage is not None
            else None
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
        """CasaSmart runtime component."""
        if not isinstance(token, str) or not token:
            raise UnknownTokenError
        if isinstance(voltage, bool) or not isinstance(voltage, (int, float)):
            raise TankError("voltage must be a number")
        voltage = float(voltage)
        if voltage != voltage or voltage in (float("inf"), float("-inf")):


            raise TankError("voltage must be a finite number")
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



            now = int(time.time())
            last_t = self._readings.latest_t(device_id)
            t = now if last_t is None or now > last_t else last_t + 1
            self._readings.append(device_id, t, voltage)
            self._readings.prune(device_id, now - _RETENTION_SECONDS)
        return device_id

    def recent_readings(self, device_id: str, days: Any = 7) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise TankError("days must be a positive integer")
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownTankError("Unknown tank device")
            cal_v = record.get("calibration_voltage", 0.0) or 0.0
            cal_d = record.get("calibration_depth", 0.0) or 0.0
            height = record.get("max_height", TANK_MAX_HEIGHT_DEFAULT) or 0.0
            cutoff = int(time.time()) - days * 24 * 3600
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
        """CasaSmart runtime component."""
        return self._readings.last(device_id)



    def _public(self, device_id: str, record: dict[str, Any]) -> dict[str, Any]:
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



            "calibration_voltage": cal_v,
            "calibration_depth": cal_d,
            "max_height": record.get("max_height", TANK_MAX_HEIGHT_DEFAULT),
            "low_percent": int(
                record.get("low_percent", TANK_LOW_PERCENT_DEFAULT)
            ),
            "is_calibrated": cal_v > 0 and cal_d > 0,
            "last_reading": last,
        }
