"""Shared harness for VIEW-layer tests — the ``*_api`` ``HomeAssistantView``s.

The audit's recurring gap: every suite imports an ENGINE directly, so the
wire-JSON seam where the contract lives (auth gate, response shapes, the
filter/scope/redaction logic in the views) was never pinned. This harness lets
a test drive a real view with a fake ``hass`` + ``web.Request``:

* The engines (auth / registry / audio) are REAL over a temp ``HubStorage`` —
  ``set_favorites``, ``member_id_for``, ``set_athan`` etc. run for real.
* Tokens are REAL HMAC JWTs minted via ``auth_tokens.issue_token`` and validated
  through the real ``AuthEngine`` (signature + device-cache ``ver`` check), so
  401/403 and the Phase-5 ``member_id`` keying are exercised, not stubbed.
* Only ``hass`` and the request are faked. ``is_served`` / ``in_scope`` are
  patched per test (their own logic is pinned in ``test_filtering``).

The one shortcut is enrollment: :func:`enroll` seeds the device record + cache
directly, skipping the public-key crypto path (covered by ``test_auth``).

Imports the ``casasmart`` package (which pulls in Home Assistant + cryptography),
so it only runs where those exist — the hub container / CI. Run e.g.:

    docker exec homeassistant-dev python3 -m unittest tests.test_registry_api -v
"""

from __future__ import annotations

import json
import secrets
import sys
import types
from pathlib import Path
from typing import Any

# Package-relative engines: put the parent of the package on the path and import
# as ``casasmart.*`` (repo layout in CI; deployed tree inside the container).
_CC = Path(__file__).resolve().parent.parent / "custom_components"
if not (_CC / "casasmart").exists():
    _CC = Path("/config/custom_components")
sys.path.insert(0, str(_CC))

try:  # Home Assistant + cryptography only exist in the container / CI.
    from casasmart import auth_tokens
    from casasmart.auth_engine import AuthEngine
    from casasmart.registry import RegistryEngine
    from casasmart.audio import AudioEngine
    from casasmart.storage import HubStorage

    IMPORT_ERROR: Exception | None = None
except Exception as err:  # noqa: BLE001 — HA-free local env
    auth_tokens = AuthEngine = RegistryEngine = AudioEngine = HubStorage = None
    IMPORT_ERROR = err


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeHubConfig:
    """dict-backed ``JsonConfigStore`` stand-in (holds the jwt_secret)."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)


class FakeBus:
    """Captures ``async_fire`` calls so tests can assert nudges fired."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, Any]] = []

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))


class FakeStates:
    """``hass.states`` over a dict — ``get`` + ``async_all``."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def add(self, entity_id, *, state="on", attributes=None, name=None):
        self._d[entity_id] = types.SimpleNamespace(
            entity_id=entity_id,
            domain=entity_id.split(".")[0],
            state=state,
            attributes=attributes or {},
            name=name or entity_id,
        )
        return self._d[entity_id]

    def get(self, entity_id):
        return self._d.get(entity_id)

    def async_all(self, domain=None):
        vals = list(self._d.values())
        if domain is None:
            return vals
        return [s for s in vals if s.domain == domain]


class _Entry:
    def __init__(self, runtime_data) -> None:
        self.runtime_data = runtime_data
        self.entry_id = "test-entry"


class FakeConfigEntries:
    def __init__(self, entry) -> None:
        self._entry = entry

    def async_loaded_entries(self, domain):
        return [self._entry]


class _Runtime:
    def __init__(self, storage, auth, registry, audio, hub_config=None) -> None:
        self.storage = storage
        self.auth = auth
        self.registry = registry
        self.audio = audio
        self.hub_config = hub_config
        self.pairing = None
        # Present-but-inert in view tests (no live MQTT/loop): the athan PUT
        # handler calls get_athan_scheduler(); None means "skip the reschedule".
        self.athan_scheduler = None


class _FakeHAConfig:
    """The ``hass.config`` location the athan GET reads for its fallback."""

    latitude = 21.5433
    longitude = 39.1728
    time_zone = "Asia/Riyadh"


class FakeHass:
    """Just enough ``hass`` for the views + ``authenticate_request``."""

    def __init__(self, runtime) -> None:
        self.config_entries = FakeConfigEntries(_Entry(runtime))
        self.states = FakeStates()
        self.bus = FakeBus()
        self.config = _FakeHAConfig()
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, func, *args):
        # Engines are sync; run inline (no thread) so tests stay deterministic.
        return func(*args)


class FakeRequest:
    """Duck-typed ``web.Request`` — headers / json body / remote / match_info."""

    def __init__(
        self,
        *,
        headers=None,
        body=None,
        remote="127.0.0.1",
        match_info=None,
        query=None,
    ) -> None:
        self.headers = headers or {}
        self._body = body
        self._has_body = body is not None
        self.remote = remote
        self.match_info = match_info or {}
        self.query = query or {}

    async def json(self):
        if not self._has_body:
            raise ValueError("no JSON body")
        return self._body


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_hub(tmpdir) -> tuple[Any, Any]:
    """A FakeHass wired to real engines over a temp HubStorage.

    Returns ``(hass, runtime)``; ``runtime.{auth,registry,audio,storage}`` are
    the live engines. Caller owns ``tmpdir`` (a TemporaryDirectory).
    """
    storage = HubStorage(Path(tmpdir) / "view-test.db")
    storage.open()
    hub_config = FakeHubConfig()
    auth = AuthEngine(storage.table("auth_devices"), hub_config)
    registry = RegistryEngine(
        storage.table("registry_floors"),
        storage.table("registry_rooms"),
        storage.table("registry_devices"),
        storage.table("registry_scenes"),
        storage.table("registry_favorites"),
        storage.table("registry_user_devices"),
    )
    audio = AudioEngine(
        storage.table("audio_config"),
        storage.table("audio_speakers"),
    )
    audio.warm_up()
    runtime = _Runtime(storage, auth, registry, audio, hub_config)
    return FakeHass(runtime), runtime


def enroll(auth, *, role="admin", rooms=None, member_id=None, name="Test Device"):
    """Seed a device record + auth cache (skips the pubkey crypto path tested in
    test_auth) and return its device_id. ``validate_token`` will accept a token
    minted for this id; ``member_id_for`` resolves to ``member_id``."""
    device_id = f"dev-{secrets.token_urlsafe(9)}"
    auth._devices[device_id] = {
        "name": name,
        "role": role,
        "public_key": "test-pem",
        "rooms": rooms,
        "ver": 1,
        "member_id": member_id or f"mem-{secrets.token_urlsafe(9)}",
    }
    auth._device_cache[device_id] = {"role": role, "rooms": rooms, "ver": 1}
    return device_id


def token_for(auth, device_id, *, role="admin", rooms=None, ttl=3600, scope=None):
    """A real HMAC JWT for ``device_id`` signed with the engine's secret."""
    return auth_tokens.issue_token(
        auth._signing_secret(), device_id, role, rooms, ttl, ver=1, scope=scope
    )


def session(auth, *, role="admin", rooms=None, member_id=None):
    """Convenience: enroll + mint in one call → (device_id, bearer_headers)."""
    device_id = enroll(auth, role=role, rooms=rooms, member_id=member_id)
    token = token_for(auth, device_id, role=role, rooms=rooms)
    return device_id, {"Authorization": f"Bearer {token}"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def read_response(resp) -> tuple[int, Any]:
    """``(status, parsed_json_body)`` from a HomeAssistantView response."""
    body = getattr(resp, "body", None)
    parsed = json.loads(body) if body else None
    return resp.status, parsed


# --------------------------------------------------------------------------- #
# Filter stubs (is_served / in_scope logic is pinned in test_filtering)
# --------------------------------------------------------------------------- #
def is_served_for(served):
    """A fake ``is_served(hass, eid)`` returning True iff eid ∈ served."""
    served = set(served)
    return lambda hass, eid: eid in served


def in_scope_for(scope_map=None):
    """A fake ``in_scope(hass, eid, scope)``: ``scope is None`` ⇒ all in scope;
    else eid must appear under one of ``scope``'s rooms in ``scope_map``."""

    def _in_scope(hass, eid, scope):
        if scope is None:
            return True
        if not scope_map:
            return False
        return any(eid in scope_map.get(room, set()) for room in scope)

    return _in_scope
