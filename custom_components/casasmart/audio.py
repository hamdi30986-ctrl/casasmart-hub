"""Hub-side audio engine (Phase 6, block B14) — the pure half.

Flips the speaker stack to match the rest of Phase 6: hub = brain, phone =
display, Pi = dumb endpoint. Today it is inverted — the phone holds the MQTT
broker creds + PA host/key in ``SharedPreferences``, opens its own
``MqttServerClient`` to drive speakers, and the Pi begs a dead Supabase edge
function for its broker credentials. This module is the hub-side source of
truth that ends all three.

Like ``alarm.py``/``registry.py``/``tank.py`` this is the flat-importable
engine: **stdlib only, no Home Assistant imports, no network I/O**. It owns
the *decisions* and the *state*; it never touches MQTT, the broker, the PA
service or the LAN. Those live in the adapter (a later B14 piece), exactly as
``alarm_adapter.py`` is the HA glue for the pure ``AlarmEngine``. The split is
what lets this be unit-tested on a temp SQLite file with a hand-cranked clock.

What it owns:

- **Broker config** — host / port / TLS / username / password. The single
  source of truth the Pi pulls on boot (``provision``) instead of Supabase,
  and the only place the creds live (the phone stops holding them).
- **PA config** — the PA service host / port / api-key the adapter proxies
  uploads through.
- **Athan config** — stored hub-side and relayed (retained) to the scheduler.
  The engine treats the payload as a validated-but-opaque blob: the scheduler
  owns its semantics, the hub just persists + hands it to the adapter to
  publish. Inventing a field schema here would couple the hub to the
  scheduler's internals.
- **Speaker registry** — the enrolled speakers (``mac6 -> {name, room,
  enrolled_at}``), persisted, plus an **ephemeral** live-status mirror
  (online / volume / playing / …) rebuilt from the broker's retained
  ``status``/``state`` topics on every (re)connect, so it is never persisted.
- **Command construction** — turns an app request ("set speaker X to 40%")
  into the exact ``{"cmd": …}`` payload + topic the Pi agent already speaks,
  validated. The wire format is taken verbatim from the live agent
  (``pi-speaker-agent/app.py``): the command field is ``cmd`` (not
  ``action``), volume carries a ``ts`` for stale-command rejection, status is
  a retained ``online``/``offline`` string, ``state`` is a retained JSON blob.

Storage-touching methods are synchronous (call via executor) and guarded by
an ``RLock`` — same posture as the other engines. The live-status mirror is
updated on the event-loop hot path (every retained ``state`` message) and is
pure in-memory, so ingest never hops the executor.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

# -- MQTT topics (verbatim from the live Pi agent) -----------------------------
# Per-speaker command sink + the all-speakers broadcast sink. The agent
# subscribes both and runs the same handler, so a broadcast is just a command
# with no speaker id.
TOPIC_BROADCAST = "speakers/broadcast"
TOPIC_ATHAN_CONFIG = "athan/config"


def speaker_command_topic(mac6: str) -> str:
    return f"speakers/{mac6}/command"


def speaker_status_topic(mac6: str) -> str:
    return f"speakers/{mac6}/status"


def speaker_state_topic(mac6: str) -> str:
    return f"speakers/{mac6}/state"


# -- Commands the app may issue (the agent's ``cmd`` vocabulary) ---------------
CMD_VOLUME = "volume"
CMD_STOP = "stop"
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_RESET = "reset"
CMD_STATUS = "status"
CMD_PLAY = "play"  # PA / broadcast only — never a bare per-speaker control

# The direct per-speaker controls the app fires. ``play`` is deliberately not
# here: audio sources go out through the PA/broadcast path (build_play), not
# the volume/stop control path, so a control request can never smuggle a file.
CONTROL_COMMANDS = frozenset(
    {CMD_VOLUME, CMD_STOP, CMD_PAUSE, CMD_RESUME, CMD_RESET, CMD_STATUS}
)

# -- Storage keys --------------------------------------------------------------
_BROKER_KEY = "broker"
_PA_KEY = "pa"
_ATHAN_KEY = "athan"

# -- Bounds --------------------------------------------------------------------
_NAME_MAX = 64
_VOLUME_MIN = 0
_VOLUME_MAX = 100
_PORT_MIN = 1
_PORT_MAX = 65535
# Athan config is an opaque relay blob; cap it so a bad client can't store an
# unbounded payload that then gets republished retained forever.
_ATHAN_MAX_KEYS = 64
_ATHAN_MAX_BYTES = 8192
# A mac6 is the last 6 hex of the speaker's MAC, lower-case (matches the
# agent's ``self.mac6``). Accept colons / 12-hex input and normalise.
_MAC6_RE = re.compile(r"^[0-9a-f]{6}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class AudioError(Exception):
    """Audio input rejected (maps to HTTP 400)."""


class UnknownSpeakerError(AudioError):
    """No speaker enrolled under that id (maps to HTTP 404)."""


def normalize_mac6(value: Any) -> str:
    """Coerce a MAC / mac6 to the agent's canonical 6-hex lower-case id.

    Accepts ``AA:BB:CC:DD:EE:FF``, ``aabbccddeeff`` (last 6 taken) or an
    already-trimmed ``ddeeff``. Pure — raises ``AudioError`` on anything that
    isn't hex.
    """
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


class AudioEngine:
    """Speaker registry + audio config over two storage tables.

    Tables (dict-like ``KeyValueTable`` handles):
      * ``config_table``   — single rows: ``broker`` / ``pa`` / ``athan``.
      * ``speakers_table`` — one row per enrolled speaker, ``mac6 -> record``.

    The live per-speaker status (online / volume / playing) is held only in
    memory (``_live``): it is authoritative on the broker as retained
    messages, so it is rebuilt on every (re)connect rather than persisted.
    ``clock`` is injectable for deterministic tests.
    """

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
        # In-memory mirrors so reads on the event loop stay pure CPU.
        self._broker: dict[str, Any] = self._default_broker()
        self._pa: dict[str, Any] = self._default_pa()
        self._athan: dict[str, Any] = {}
        self._speakers: dict[str, dict[str, Any]] = {}
        # mac6 -> ephemeral live status (NOT persisted).
        self._live: dict[str, dict[str, Any]] = {}

    # -- lifecycle -------------------------------------------------------------

    def warm_up(self) -> None:
        """Load persisted config + speaker registry into the mirrors.

        Blocking (storage read) — call via executor at setup, like
        ``AlarmEngine.warm_up``. After this, the read/ingest paths are pure CPU.
        """
        with self._lock:
            self._broker = self._coerce_broker(self._config_table.get(_BROKER_KEY))
            self._pa = self._coerce_pa(self._config_table.get(_PA_KEY))
            stored_athan = self._config_table.get(_ATHAN_KEY)
            self._athan = dict(stored_athan) if isinstance(stored_athan, dict) else {}
            self._speakers = {
                mac6: dict(record)
                for mac6, record in self._speakers_table.items()
            }

    # -- broker config ---------------------------------------------------------

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
        """Broker config the adapter connects with (copy)."""
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
        """Update broker config (omitted fields keep their value)."""
        with self._lock:
            updated = dict(self._broker)
            if host is not None:
                updated["host"] = _clean_name(host, field="broker host")
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
        """Broker coordinates the Pi pulls on boot (replaces the Supabase 404).

        This is the cred source for ``GET /audio/provision``. Returns the
        shared broker identity the dev Pi connects with; per-speaker unique
        creds are a hardening layer addable later without app changes (the
        speaker record already has room for them).
        """
        with self._lock:
            return {
                "host": self._broker["host"],
                "port": self._broker["port"],
                "tls": self._broker["tls"],
                "username": self._broker["username"],
                "password": self._broker["password"],
            }

    # -- PA config -------------------------------------------------------------

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
                updated["host"] = _clean_name(host, field="PA host")
            updated["port"] = _validate_port(
                port, field="PA port", default=updated["port"]
            )
            if api_key is not None:
                updated["api_key"] = _opt_str(api_key, field="PA api_key")
            self._pa = updated
            self._config_table[_PA_KEY] = dict(updated)
            return dict(updated)

    # -- athan config (opaque relay blob) --------------------------------------

    def get_athan(self) -> dict[str, Any]:
        """The stored athan config the app's settings screen renders (copy)."""
        with self._lock:
            return dict(self._athan)

    def set_athan(self, config: Any) -> dict[str, Any]:
        """Replace the athan config. Validated as a bounded JSON object.

        The hub does not interpret the fields — the scheduler owns the schema.
        It only guarantees the payload is a sane, bounded object before it gets
        relayed (retained) to ``athan/config`` by the adapter.
        """
        if not isinstance(config, dict):
            raise AudioError("athan config must be a JSON object")
        if len(config) > _ATHAN_MAX_KEYS:
            raise AudioError(f"athan config has too many keys (max {_ATHAN_MAX_KEYS})")
        for key in config:
            if not isinstance(key, str):
                raise AudioError("athan config keys must be strings")
        try:
            import json

            encoded = json.dumps(config)
        except (TypeError, ValueError) as err:
            raise AudioError(f"athan config is not JSON-serialisable: {err}") from err
        if len(encoded.encode("utf-8")) > _ATHAN_MAX_BYTES:
            raise AudioError(f"athan config too large (max {_ATHAN_MAX_BYTES} bytes)")
        with self._lock:
            self._athan = dict(config)
            self._config_table[_ATHAN_KEY] = dict(config)
            return dict(self._athan)

    # -- speaker registry (storage) --------------------------------------------

    def enroll_speaker(
        self, mac: Any, name: Any, room: Any = None
    ) -> dict[str, Any]:
        """Add (or rename in place) an enrolled speaker.

        Called at the end of the app's add-speaker flow once the speaker is on
        the LAN and named. Idempotent on the id: re-enrolling updates the
        name/room and preserves ``enrolled_at``.
        """
        mac6 = normalize_mac6(mac)
        clean_name = _clean_name(name)
        clean_room = _clean_optional_name(room, field="room")
        with self._lock:
            existing = self._speakers.get(mac6)
            record = {
                "mac6": mac6,
                "name": clean_name,
                "room": clean_room,
                "enrolled_at": existing["enrolled_at"]
                if existing
                else self._clock(),
            }
            self._speakers_table[mac6] = record
            self._speakers[mac6] = dict(record)
            return dict(record)

    def update_speaker(
        self, mac: Any, *, name: Any = None, room: Any = None
    ) -> dict[str, Any]:
        """Rename / re-room an enrolled speaker (omitted fields unchanged)."""
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
        """Enrolled speakers merged with their live status, id-sorted.

        Each entry is the persisted record plus a ``live`` block (``online``,
        ``volume``, ``playing``, ``last_seen``, …) — ``online: False`` with an
        empty live block for an enrolled speaker the hub has not heard from.
        """
        with self._lock:
            result = []
            for mac6 in sorted(self._speakers):
                record = dict(self._speakers[mac6])
                record["live"] = dict(self._live.get(mac6, {"online": False}))
                result.append(record)
            return result

    # -- live status ingest (hot path — pure CPU, no storage) ------------------

    def ingest_status(self, mac: Any, online: Any) -> None:
        """Apply a retained ``speakers/<mac6>/status`` message (online/offline).

        ``online`` is the raw broker payload — the agent publishes the strings
        ``"online"`` / ``"offline"``. Tolerant of bool too. Unknown/unenrolled
        ids are still tracked (a speaker can announce before it is enrolled).
        """
        mac6 = normalize_mac6(mac)
        is_online = online is True or (
            isinstance(online, str) and online.strip().lower() == "online"
        )
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = is_online
            live["last_seen"] = self._clock()

    def ingest_state(self, mac: Any, payload: Any) -> None:
        """Apply a retained ``speakers/<mac6>/state`` JSON blob.

        Copies the agent's published fields (volume/playing/room/airplay/…)
        into the live mirror, marks the speaker online, stamps last_seen.
        A malformed payload is ignored (a bad retained blob must not wedge the
        whole speaker list).
        """
        mac6 = normalize_mac6(mac)
        if not isinstance(payload, dict):
            return
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = True
            live["last_seen"] = self._clock()
            for key in (
                "room",
                "volume",
                "playing",
                "playing_file",
                "airplay_active",
                "airplay_title",
                "airplay_artist",
                "uptime",
            ):
                if key in payload:
                    live[key] = payload[key]

    def ingest_announce(self, mac: Any, room: Any = None) -> None:
        """Apply a ``speakers/announce`` discovery beacon.

        The Pi agent publishes this (qos 0, NOT retained) on connect and in
        reply to ``speakers/ping`` — it carries ``{mac, room, topic, version,
        features}``. Unlike status/state this is how the hub learns a speaker
        *exists* before it is enrolled, so the add-speaker flow can list it.
        Marks the speaker online and records its advertised room.
        """
        mac6 = normalize_mac6(mac)
        with self._lock:
            live = self._live.setdefault(mac6, {})
            live["online"] = True
            live["last_seen"] = self._clock()
            if isinstance(room, str) and room.strip():
                live["room"] = room.strip()

    def discovered(self) -> list[dict[str, Any]]:
        """Speakers the hub has heard from that are NOT yet enrolled.

        This is the source for the add-speaker flow: a speaker shows up here
        (from an announce / retained status / state) once it is on the LAN and
        talking to the broker, and moves out of this list into ``speakers()``
        the moment it is enrolled. Id-sorted; each entry is ``{mac6, ...live}``.
        """
        with self._lock:
            result = []
            for mac6 in sorted(self._live):
                if mac6 in self._speakers:
                    continue
                entry = {"mac6": mac6}
                entry.update(self._live[mac6])
                result.append(entry)
            return result

    def live_status(self, mac: Any) -> dict[str, Any]:
        mac6 = normalize_mac6(mac)
        with self._lock:
            return dict(self._live.get(mac6, {"online": False}))

    # -- command construction (pure — adapter publishes the result) -----------

    def build_command(
        self, mac: Any, cmd: Any, *, value: Any = None, now: Optional[float] = None
    ) -> tuple[str, dict[str, Any]]:
        """Validate a per-speaker control and return ``(topic, payload)``.

        ``payload`` uses the agent's ``cmd`` field verbatim. ``volume`` carries
        a ``ts`` (the engine clock) so the agent's stale-command guard works.
        Raises ``UnknownSpeakerError`` for an un-enrolled speaker and
        ``AudioError`` for a bad command/value.
        """
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
        """Build a ``play`` payload for PA / athan, targeted or broadcast.

        ``mac`` None -> the broadcast topic (all speakers); otherwise the named
        speaker's command topic. Exactly one of ``url`` / ``file`` is required
        (the agent reads ``file`` or ``url``). ``volume`` optionally overrides
        playback level for this clip.
        """
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
            payload["priority"] = _opt_str(priority, field="priority")
        payload["ts"] = self._clock() if now is None else now
        if mac is None:
            return TOPIC_BROADCAST, payload
        mac6 = normalize_mac6(mac)
        if mac6 not in self._speakers:
            raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")
        return speaker_command_topic(mac6), payload
