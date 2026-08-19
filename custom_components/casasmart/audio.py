"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)





TOPIC_BROADCAST = "speakers/broadcast"
TOPIC_ATHAN_CONFIG = "athan/config"


def speaker_command_topic(mac6: str) -> str:
    return f"speakers/{mac6}/command"


def speaker_status_topic(mac6: str) -> str:
    return f"speakers/{mac6}/status"


def speaker_state_topic(mac6: str) -> str:
    return f"speakers/{mac6}/state"


def speaker_airplay_remote_topic(mac6: str) -> str:
    """CasaSmart runtime component."""
    return f"speakers/{mac6}/airplay/remote"






AIRPLAY_ACTIONS = {
    "playpause": "playpause",
    "play": "play",
    "pause": "pause",
    "next": "nextitem",
    "previous": "previtem",
    "stop": "stop",
}



CMD_VOLUME = "volume"
CMD_STOP = "stop"
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_RESET = "reset"
CMD_STATUS = "status"
CMD_PLAY = "play"




CONTROL_COMMANDS = frozenset(
    {CMD_VOLUME, CMD_STOP, CMD_PAUSE, CMD_RESUME, CMD_RESET, CMD_STATUS}
)


_BROKER_KEY = "broker"
_PA_KEY = "pa"
_ATHAN_KEY = "athan"


_NAME_MAX = 64
_VOLUME_MIN = 0
_VOLUME_MAX = 100
_PORT_MIN = 1
_PORT_MAX = 65535


_ATHAN_MAX_KEYS = 64
_ATHAN_MAX_BYTES = 8192


_ATHAN_MAX_SPEAKERS = 64


_LIVE_STR_MAX = 128


PRIORITY_VALUES = frozenset({"athan", "pa", "normal"})




_DISCOVERY_TTL_SECONDS = 600


_MAC6_RE = re.compile(r"^[0-9a-f]{6}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class AudioError(Exception):
    """CasaSmart runtime component."""


class UnknownSpeakerError(AudioError):
    """CasaSmart runtime component."""


def normalize_mac6(value: Any) -> str:
    """CasaSmart runtime component."""
    if not isinstance(value, str) or not value.strip():
        raise AudioError("Speaker id is required")
    cleaned = value.strip().lower().replace(":", "").replace("-", "")
    if not _HEX_RE.match(cleaned):
        raise AudioError(f"Invalid speaker id {value!r} (expected hex)")
    mac6 = cleaned[-6:]
    if not _MAC6_RE.match(mac6):
        raise AudioError(f"Invalid speaker id {value!r} (need >= 6 hex chars)")
    return mac6


def _clean_name(name: Any, *, field: str = "name") -> str:
    if not isinstance(name, str) or not name.strip():
        raise AudioError(f"{field} is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise AudioError(f"{field} is too long (max {_NAME_MAX})")
    return cleaned


def _clean_optional_name(name: Any, *, field: str) -> Optional[str]:
    if name is None:
        return None
    return _clean_name(name, field=field)





_ICON_KEY_MAX = 64


def _clean_optional_icon(icon: Any) -> Optional[str]:
    """CasaSmart runtime component."""
    if icon is None:
        return None
    if not isinstance(icon, str):
        raise AudioError("icon must be a string")
    cleaned = icon.strip()
    if not cleaned:
        return None
    return cleaned[:_ICON_KEY_MAX]




_AREA_ID_MAX = 128


def _clean_optional_area_id(area_id: Any) -> Optional[str]:
    """CasaSmart runtime component."""
    if area_id is None:
        return None
    if not isinstance(area_id, str):
        raise AudioError("area_id must be a string")
    cleaned = area_id.strip()
    if not cleaned:
        return None
    return cleaned[:_AREA_ID_MAX]


def _validate_volume(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AudioError("volume must be an integer 0-100")
    if not _VOLUME_MIN <= value <= _VOLUME_MAX:
        raise AudioError(f"volume must be {_VOLUME_MIN}-{_VOLUME_MAX}")
    return value


def _validate_port(value: Any, *, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AudioError(f"{field} must be an integer port")
    if not _PORT_MIN <= value <= _PORT_MAX:
        raise AudioError(f"{field} must be {_PORT_MIN}-{_PORT_MAX}")
    return value


def _opt_str(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AudioError(f"{field} must be a string")
    return value


def _is_number(value: Any) -> bool:
    """CasaSmart runtime component."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _validate_host(value: Any, *, field: str) -> str:
    """CasaSmart runtime component."""
    if not isinstance(value, str) or not value.strip():
        raise AudioError(f"{field} is required")
    host = value.strip()
    if not (
        1 <= len(host) <= 253
        and host[0].isalnum()
        and host[-1].isalnum()
        and all(c.isalnum() or c in ".-:" for c in host)
    ):
        raise AudioError(f"{field} must be a valid hostname or IP")
    return host


def _clean_live(payload: dict[str, Any]) -> dict[str, Any]:
    """CasaSmart runtime component."""
    out: dict[str, Any] = {}
    room = payload.get("room")
    if isinstance(room, str) and room.strip():
        out["room"] = room.strip()[:_LIVE_STR_MAX]
    for flag in ("playing", "airplay_active"):
        if isinstance(payload.get(flag), bool):
            out[flag] = payload[flag]
    for text in ("playing_file", "airplay_title", "airplay_artist"):
        value = payload.get(text)
        if isinstance(value, str):
            out[text] = value[:_LIVE_STR_MAX]
    if _is_number(payload.get("volume")):
        out["volume"] = max(_VOLUME_MIN, min(_VOLUME_MAX, int(payload["volume"])))
    if _is_number(payload.get("uptime")):
        out["uptime"] = payload["uptime"]
    return out


class AudioEngine:
    """CasaSmart runtime component."""

    def __init__(
        self,
        config_table: Any,
        speakers_table: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_table = config_table
        self._speakers_table = speakers_table
        self._clock = clock
        self._lock = threading.RLock()

        self._broker: dict[str, Any] = self._default_broker()
        self._pa: dict[str, Any] = self._default_pa()
        self._athan: dict[str, Any] = {}
        self._speakers: dict[str, dict[str, Any]] = {}

        self._live: dict[str, dict[str, Any]] = {}



    def warm_up(self) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            self._broker = self._coerce_broker(self._config_table.get(_BROKER_KEY))
            self._pa = self._coerce_pa(self._config_table.get(_PA_KEY))
            stored_athan = self._config_table.get(_ATHAN_KEY)
            self._athan = dict(stored_athan) if isinstance(stored_athan, dict) else {}
            self._speakers = {
                mac6: dict(record)
                for mac6, record in self._speakers_table.items()
            }



    @staticmethod
    def _default_broker() -> dict[str, Any]:
        return {
            "host": None,
            "port": 1883,
            "tls": False,
            "username": None,
            "password": None,
        }

    def _coerce_broker(self, stored: Any) -> dict[str, Any]:
        broker = self._default_broker()
        if isinstance(stored, dict):
            if isinstance(stored.get("host"), str):
                broker["host"] = stored["host"]
            if isinstance(stored.get("port"), int) and not isinstance(
                stored.get("port"), bool
            ):
                broker["port"] = stored["port"]
            broker["tls"] = bool(stored.get("tls", False))
            for key in ("username", "password"):
                if isinstance(stored.get(key), str):
                    broker[key] = stored[key]
        return broker

    def get_broker(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            return dict(self._broker)

    def set_broker(
        self,
        *,
        host: Any = None,
        port: Any = None,
        tls: Any = None,
        username: Any = None,
        password: Any = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            updated = dict(self._broker)
            if host is not None:
                updated["host"] = _validate_host(host, field="broker host")
            updated["port"] = _validate_port(
                port, field="broker port", default=updated["port"]
            )
            if tls is not None:
                updated["tls"] = bool(tls)
            if username is not None:
                updated["username"] = _opt_str(username, field="broker username")
            if password is not None:
                updated["password"] = _opt_str(password, field="broker password")
            self._broker = updated
            self._config_table[_BROKER_KEY] = dict(updated)
            return dict(updated)

    def provision(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            return {
                "host": self._broker["host"],
                "port": self._broker["port"],
                "tls": self._broker["tls"],
                "username": self._broker["username"],
                "password": self._broker["password"],
            }



    @staticmethod
    def _default_pa() -> dict[str, Any]:
        return {"host": None, "port": 9876, "api_key": None}

    def _coerce_pa(self, stored: Any) -> dict[str, Any]:
        pa = self._default_pa()
        if isinstance(stored, dict):
            if isinstance(stored.get("host"), str):
                pa["host"] = stored["host"]
            if isinstance(stored.get("port"), int) and not isinstance(
                stored.get("port"), bool
            ):
                pa["port"] = stored["port"]
            if isinstance(stored.get("api_key"), str):
                pa["api_key"] = stored["api_key"]
        return pa

    def get_pa(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._pa)

    def set_pa(
        self, *, host: Any = None, port: Any = None, api_key: Any = None
    ) -> dict[str, Any]:
        with self._lock:
            updated = dict(self._pa)
            if host is not None:
                updated["host"] = _validate_host(host, field="PA host")
            updated["port"] = _validate_port(
                port, field="PA port", default=updated["port"]
            )
            if api_key is not None:
                updated["api_key"] = _opt_str(api_key, field="PA api_key")
            self._pa = updated
            self._config_table[_PA_KEY] = dict(updated)
            return dict(updated)



    def get_athan(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            return dict(self._athan)

    def set_athan(self, config: Any) -> dict[str, Any]:
        """CasaSmart runtime component."""
        if not isinstance(config, dict):
            raise AudioError("athan config must be a JSON object")
        if len(config) > _ATHAN_MAX_KEYS:
            raise AudioError(f"athan config has too many keys (max {_ATHAN_MAX_KEYS})")
        for key in config:
            if not isinstance(key, str):
                raise AudioError("athan config keys must be strings")
        enabled = config.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise AudioError("athan 'enabled' must be a boolean")





        for coord in ("lat", "lon"):
            value = config.get(coord)
            if value is not None and not _is_number(value):
                raise AudioError(f"athan {coord!r} must be a finite number")





        speakers = config.get("speakers")
        if speakers is not None:
            if not isinstance(speakers, list):
                raise AudioError("athan 'speakers' must be a list")
            if len(speakers) > _ATHAN_MAX_SPEAKERS:
                raise AudioError(
                    f"athan 'speakers' has too many entries (max {_ATHAN_MAX_SPEAKERS})"
                )
            for item in speakers:
                if not isinstance(item, str) or not item.strip():
                    raise AudioError("athan 'speakers' entries must be non-empty strings")
        try:
            import json



            encoded = json.dumps(config, allow_nan=False)
        except (TypeError, ValueError) as err:
            raise AudioError(f"athan config is not JSON-serialisable: {err}") from err
        if len(encoded.encode("utf-8")) > _ATHAN_MAX_BYTES:
            raise AudioError(f"athan config too large (max {_ATHAN_MAX_BYTES} bytes)")
        with self._lock:
            self._athan = dict(config)
            self._config_table[_ATHAN_KEY] = dict(config)
            return dict(self._athan)



    def enroll_speaker(
        self,
        mac: Any,
        name: Any,
        room: Any = None,
        icon: Any = None,
        area_id: Any = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        clean_name = _clean_name(name)
        clean_room = _clean_optional_name(room, field="room")
        clean_icon = _clean_optional_icon(icon)
        clean_area = _clean_optional_area_id(area_id)
        with self._lock:
            existing = self._speakers.get(mac6)
            record = {
                "mac6": mac6,
                "name": clean_name,
                "room": clean_room,


                "custom_icon": clean_icon
                if icon is not None
                else (existing.get("custom_icon") if existing else None),


                "area_id": clean_area
                if area_id is not None
                else (existing.get("area_id") if existing else None),
                "enrolled_at": existing["enrolled_at"]
                if existing
                else self._clock(),
            }
            self._speakers_table[mac6] = record
            self._speakers[mac6] = dict(record)
            return dict(record)

    def update_speaker(
        self,
        mac: Any,
        *,
        name: Any = None,
        room: Any = None,
        icon: Any = None,
        area_id: Any = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        with self._lock:
            existing = self._speakers.get(mac6)
            if existing is None:
                raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
            record = dict(existing)
            if name is not None:
                record["name"] = _clean_name(name)
            if room is not None:
                record["room"] = _clean_name(room, field="room")
            if icon is not None:
                record["custom_icon"] = _clean_optional_icon(icon)
            if area_id is not None:
                record["area_id"] = _clean_optional_area_id(area_id)
            self._speakers_table[mac6] = record
            self._speakers[mac6] = dict(record)
            return dict(record)

    def remove_speaker(self, mac: Any) -> None:
        mac6 = normalize_mac6(mac)
        with self._lock:
            if mac6 not in self._speakers:
                raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
            del self._speakers_table[mac6]
            self._speakers.pop(mac6, None)
            self._live.pop(mac6, None)

    def is_enrolled(self, mac: Any) -> bool:
        try:
            mac6 = normalize_mac6(mac)
        except AudioError:
            return False
        return mac6 in self._speakers

    def speakers(self) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        with self._lock:
            result = []
            for mac6 in sorted(self._speakers):
                record = dict(self._speakers[mac6])


                record.setdefault("custom_icon", None)
                record.setdefault("area_id", None)
                record["live"] = dict(self._live.get(mac6, {"online": False}))
                result.append(record)
            return result



    def ingest_status(self, mac: Any, online: Any) -> None:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        is_online = online is True or (
            isinstance(online, str) and online.strip().lower() == "online"
        )
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = is_online
            live["last_seen"] = self._clock()

    def ingest_state(self, mac: Any, payload: Any) -> None:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        if not isinstance(payload, dict):
            return
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = True
            live["last_seen"] = self._clock()



            live.update(_clean_live(payload))

    def ingest_announce(self, mac: Any, room: Any = None) -> None:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = True
            live["last_seen"] = self._clock()
            if isinstance(room, str) and room.strip():
                live["room"] = room.strip()[:_LIVE_STR_MAX]

    def discovered(self, *, ttl: Optional[float] = _DISCOVERY_TTL_SECONDS) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        with self._lock:
            now = self._clock()
            result = []
            for mac6 in sorted(self._live):
                if mac6 in self._speakers:
                    continue
                live = self._live[mac6]
                if ttl is not None:
                    last_seen = live.get("last_seen")
                    if isinstance(last_seen, (int, float)) and now - last_seen > ttl:
                        continue
                entry = {"mac6": mac6}
                entry.update(live)
                result.append(entry)
            return result

    def live_status(self, mac: Any) -> dict[str, Any]:
        mac6 = normalize_mac6(mac)
        with self._lock:
            return dict(self._live.get(mac6, {"online": False}))



    def build_command(
        self, mac: Any, cmd: Any, *, value: Any = None, now: Optional[float] = None
    ) -> tuple[str, dict[str, Any]]:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        if mac6 not in self._speakers:
            raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
        if cmd not in CONTROL_COMMANDS:
            raise AudioError(
                f"Unknown command {cmd!r} (expected one of {sorted(CONTROL_COMMANDS)})"
            )
        payload: dict[str, Any] = {"cmd": cmd}
        if cmd == CMD_VOLUME:
            payload["value"] = _validate_volume(value)
            payload["ts"] = self._clock() if now is None else now
        return speaker_command_topic(mac6), payload

    def build_airplay_remote(self, mac: Any, action: Any) -> tuple[str, str]:
        """CasaSmart runtime component."""
        mac6 = normalize_mac6(mac)
        if mac6 not in self._speakers:
            raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
        verb = AIRPLAY_ACTIONS.get(action)
        if verb is None:
            raise AudioError(
                f"Unknown airplay action {action!r} "
                f"(expected one of {sorted(AIRPLAY_ACTIONS)})"
            )
        return speaker_airplay_remote_topic(mac6), verb

    def build_play(
        self,
        *,
        mac: Any = None,
        url: Any = None,
        file: Any = None,
        volume: Any = None,
        priority: Any = None,
        now: Optional[float] = None,
    ) -> tuple[str, dict[str, Any]]:
        """CasaSmart runtime component."""
        if (url is None) == (file is None):
            raise AudioError("play requires exactly one of url / file")
        source = url if url is not None else file
        if not isinstance(source, str) or not source.strip():
            raise AudioError("play source must be a non-empty string")
        payload: dict[str, Any] = {"cmd": CMD_PLAY}
        payload["url" if url is not None else "file"] = source.strip()
        if volume is not None:
            payload["volume"] = _validate_volume(volume)
        if priority is not None:
            if priority not in PRIORITY_VALUES:
                raise AudioError(
                    f"priority must be one of {sorted(PRIORITY_VALUES)}"
                )
            payload["priority"] = priority
        payload["ts"] = self._clock() if now is None else now
        if mac is None:
            return TOPIC_BROADCAST, payload
        mac6 = normalize_mac6(mac)
        if mac6 not in self._speakers:
            raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
        return speaker_command_topic(mac6), payload
