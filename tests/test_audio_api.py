"""View-layer tests for ``audio_api`` — the speaker/athan/broker wire contract.

Pins the logic that lives in the VIEW + the thin engine validation it
surfaces as HTTP status codes:
* speakers GET/POST (enroll), speaker PUT/DELETE — registry round-trip + bad-mac
  rejection.
* athan GET/PUT — the opaque-blob round-trip (extra scheduler keys
  survive) + the enabled/lat validation path.
* broker + pa-config GET — secret REDACTION (never plaintext).
* command + broadcast — command vocabulary (volume/stop ok, unknown 400, play is
  not a per-speaker control).
* AUTH gate — a role lacking ``audio.manage`` -> 403, no token -> 401.
* ``_parse_targets`` — the pure module helper, unit-tested directly.
* room scope on control — command/airplay/broadcast/PA reach only the speakers
  the caller can list (the PA POST is driven through a minimal multipart fake).

Container/CI only (imports Home Assistant). Run:
    docker exec -w /config/tests homeassistant python3 -m unittest test_audio_api -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.audio_api import (
        CasaSmartAudioAirplayView,
        CasaSmartAudioAthanView,
        CasaSmartAudioBroadcastView,
        CasaSmartAudioBrokerView,
        CasaSmartAudioCommandView,
        CasaSmartAudioPaConfigView,
        CasaSmartAudioPaView,
        CasaSmartAudioProvisionView,
        CasaSmartAudioSpeakersView,
        CasaSmartAudioSpeakerView,
        _pa_store,
        _parse_targets,
    )
    from casasmart.const import EVENT_AUDIO_CHANGED, PROVISION_SECRET_CONFIG_KEY

    _ERR = None
except Exception as err:
    CasaSmartAudioSpeakersView = CasaSmartAudioSpeakerView = None
    CasaSmartAudioAthanView = CasaSmartAudioBrokerView = None
    CasaSmartAudioPaConfigView = CasaSmartAudioCommandView = None
    CasaSmartAudioBroadcastView = _parse_targets = None
    CasaSmartAudioPaView = _pa_store = None
    CasaSmartAudioProvisionView = None
    EVENT_AUDIO_CHANGED = PROVISION_SECRET_CONFIG_KEY = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _raw_body(resp) -> str:
    """The response's raw serialised body as text (to scan for a leaked secret)."""
    body = getattr(resp, "body", None)
    if body is None:
        return ""
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)


class FakeAdapter:
    """Records publishes so command/broadcast tests can assert the wire payload.

    The views call ``_adapter_or_503`` (the adapter MUST exist) and then
    ``adapter.publish(topic, payload, qos=, retain=)``. The fake records every
    publish; build_command/build_play validation runs for real on the engine.
    """

    def __init__(self) -> None:
        self.published: list[tuple] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def clear_speaker_retained(self, mac6):
        self.published.append(("__clear__", mac6, None, None))


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class AudioViewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)

    def _admin(self):
        _, hdr = H.session(self.rt.auth, role="admin")
        return hdr

    def _user(self):
        _, hdr = H.session(self.rt.auth, role="user")
        return hdr


# --------------------------------------------------------------------------- #
# Speakers registry: GET list, POST enroll
# --------------------------------------------------------------------------- #
class SpeakersView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioSpeakersView(self.hass)

    async def test_get_lists_enrolled_speakers(self) -> None:
        # Enroll one directly on the engine, then read it back through the view.
        self.rt.audio.enroll_speaker("aabbccddeeff", "Kitchen", "Kitchen")
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        names = [s["name"] for s in body["speakers"]]
        self.assertIn("Kitchen", names)
        macs = [s["mac6"] for s in body["speakers"]]
        self.assertIn("ddeeff", macs)  # mac6 = last 6 hex

    async def test_post_enroll_adds_speaker(self) -> None:
        resp = await self.view.post(
            H.FakeRequest(
                headers=self._admin(),
                body={"mac": "11:22:33:44:55:66", "name": "Salon", "room": "Salon"},
            )
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["speaker"]["name"], "Salon")
        self.assertEqual(body["speaker"]["mac6"], "445566")
        # It really landed in the registry.
        self.assertTrue(self.rt.audio.is_enrolled("445566"))
        # The mutation fired the re-fetch nudge.
        self.assertIn(EVENT_AUDIO_CHANGED, [evt for evt, _ in self.hass.bus.fired])

    async def test_post_enroll_stores_custom_icon(self) -> None:
        resp = await self.view.post(
            H.FakeRequest(
                headers=self._admin(),
                body={"mac": "aabbcc", "name": "Salon", "icon": "sonos"},
            )
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["speaker"]["custom_icon"], "sonos")
        stored = next(s for s in self.rt.audio.speakers() if s["mac6"] == "aabbcc")
        self.assertEqual(stored["custom_icon"], "sonos")

    async def test_get_speaker_shape_has_custom_icon_key(self) -> None:
        # A speaker enrolled with no icon still serves the key (null).
        self.rt.audio.enroll_speaker("112233", "No Icon", None)
        served = next(s for s in self.rt.audio.speakers() if s["mac6"] == "112233")
        self.assertIn("custom_icon", served)
        self.assertIsNone(served["custom_icon"])

    async def test_post_bad_mac_is_400(self) -> None:
        # "zzzz" is not hex -> normalize_mac6 raises AudioError -> 400.
        resp = await self.view.post(
            H.FakeRequest(headers=self._admin(), body={"mac": "zzzzzz", "name": "Bad"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.rt.audio.speakers(), [])

    async def test_post_missing_name_is_400(self) -> None:
        resp = await self.view.post(
            H.FakeRequest(headers=self._admin(), body={"mac": "aabbcc", "name": ""})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_post_requires_manage_user_is_403(self) -> None:
        # "user" role lacks audio.manage (admin/sub-admin only).
        resp = await self.view.post(
            H.FakeRequest(headers=self._user(), body={"mac": "aabbcc", "name": "Nope"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)
        self.assertEqual(self.rt.audio.speakers(), [])  # not enrolled

    async def test_get_no_token_is_401(self) -> None:
        resp = await self.view.get(H.FakeRequest(headers={}))
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)


# --------------------------------------------------------------------------- #
# One speaker: PUT rename, DELETE drop
# --------------------------------------------------------------------------- #
class SpeakerView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioSpeakerView(self.hass)
        self.rt.audio.enroll_speaker("aabbccddeeff", "Old Name", "Kitchen")

    async def test_put_renames(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"name": "New Name"}),
            mac6="ddeeff",
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["speaker"]["name"], "New Name")
        # Stored name actually changed.
        stored = next(s for s in self.rt.audio.speakers() if s["mac6"] == "ddeeff")
        self.assertEqual(stored["name"], "New Name")

    async def test_put_sets_and_clears_custom_icon(self) -> None:
        # Set an icon.
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"icon": "hifi"}),
            mac6="ddeeff",
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["speaker"]["custom_icon"], "hifi")
        # Renaming without an icon field leaves the icon untouched.
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"name": "Renamed"}),
            mac6="ddeeff",
        )
        _, body = H.read_response(resp)
        self.assertEqual(body["speaker"]["custom_icon"], "hifi")
        # Empty-string icon clears it.
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"icon": ""}),
            mac6="ddeeff",
        )
        _, body = H.read_response(resp)
        self.assertIsNone(body["speaker"]["custom_icon"])

    async def test_put_unknown_speaker_is_404(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"name": "X"}),
            mac6="000000",
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_delete_removes(self) -> None:
        # A live adapter so the best-effort deprovision path runs cleanly.
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: FakeAdapter()
        ):
            resp = await self.view.delete(
                H.FakeRequest(headers=self._admin()), mac6="ddeeff"
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], "ddeeff")
        self.assertFalse(self.rt.audio.is_enrolled("ddeeff"))

    async def test_delete_unknown_is_404(self) -> None:
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: FakeAdapter()
        ):
            resp = await self.view.delete(
                H.FakeRequest(headers=self._admin()), mac6="000000"
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_put_requires_manage_user_is_403(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._user(), body={"name": "Nope"}),
            mac6="ddeeff",
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)
        # Name unchanged.
        stored = next(s for s in self.rt.audio.speakers() if s["mac6"] == "ddeeff")
        self.assertEqual(stored["name"], "Old Name")


# --------------------------------------------------------------------------- #
# Athan config — the opaque relay blob
# --------------------------------------------------------------------------- #
class AthanView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioAthanView(self.hass)

    async def test_get_returns_stored(self) -> None:
        self.rt.audio.set_athan({"enabled": False, "method": "MWL"})
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["athan"]["method"], "MWL")

    async def test_get_location_falls_back_to_home(self) -> None:
        # No pinned coords → the resolved location is the hub's own home config.
        self.rt.audio.set_athan({"enabled": True, "method": "makkah"})
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, body = H.read_response(resp)
        self.assertEqual(body["location"]["source"], "home")
        self.assertEqual(body["location"]["timezone"], "Asia/Riyadh")
        self.assertEqual(body["location"]["lat"], 21.5433)

    async def test_get_location_reflects_pinned_coords(self) -> None:
        self.rt.audio.set_athan(
            {"enabled": True, "lat": 41.0, "lon": 29.0, "timezone": "Europe/Istanbul"}
        )
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, body = H.read_response(resp)
        self.assertEqual(body["location"]["source"], "config")
        self.assertEqual(body["location"]["lat"], 41.0)
        self.assertEqual(body["location"]["timezone"], "Europe/Istanbul")

    async def test_get_location_falls_back_per_coordinate_like_the_scheduler(
        self,
    ) -> None:
        # With one coordinate pinned, the scheduler uses it plus the home's
        # other one; the GET must report the location the scheduler uses.
        from casasmart.athan_scheduler import AthanScheduler

        for pinned in ({"lat": 41.0}, {"lon": 29.0}):
            with self.subTest(pinned=pinned):
                self.rt.audio.set_athan({"enabled": True, **pinned})
                resp = await self.view.get(H.FakeRequest(headers=self._admin()))
                location = H.read_response(resp)[1]["location"]
                scheduler = AthanScheduler(self.hass, self.rt.audio, None)
                lat, lon, *_ = scheduler._resolve_config()
                self.assertEqual((location["lat"], location["lon"]), (lat, lon))
                self.assertEqual(location["source"], "config")

    async def test_put_stores_and_extra_keys_survive(self) -> None:
        # Opaque-blob round-trip: the hub does NOT model per_prayer / a
        # nested override map, yet those EXTRA keys must survive PUT -> GET.
        config = {
            "enabled": True,
            "lat": 21,
            "lon": 39,
            "per_prayer": {"fajr": 5},
            "method": "UmmAlQura",
        }
        with mock.patch("casasmart.audio_api.get_audio_adapter", lambda hass: None):
            put = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"athan": config})
            )
        put_status, put_body = H.read_response(put)
        self.assertEqual(put_status, 200)
        self.assertEqual(put_body["athan"]["per_prayer"], {"fajr": 5})

        get = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, get_body = H.read_response(get)
        # Every extra key the hub doesn't model still round-trips intact.
        self.assertEqual(get_body["athan"]["per_prayer"], {"fajr": 5})
        self.assertEqual(get_body["athan"]["lat"], 21)
        self.assertEqual(get_body["athan"]["method"], "UmmAlQura")
        self.assertTrue(get_body["athan"]["enabled"])

    async def test_get_includes_schedule_block(self) -> None:
        # Observability: GET always carries a `schedule` key (null until the
        # scheduler has run) so the app can render "next athan".
        self.rt.audio.set_athan({"enabled": True, "method": "makkah"})
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, body = H.read_response(resp)
        self.assertIn("schedule", body)

    async def test_put_speakers_selection_round_trips(self) -> None:
        config = {"enabled": True, "method": "makkah", "speakers": ["aabbcc", "ddeeff"]}
        with mock.patch("casasmart.audio_api.get_audio_adapter", lambda hass: None):
            put = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"athan": config})
            )
        self.assertEqual(H.read_response(put)[0], 200)
        get = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, get_body = H.read_response(get)
        self.assertEqual(get_body["athan"]["speakers"], ["aabbcc", "ddeeff"])

    async def test_put_accepts_bare_config_without_athan_wrapper(self) -> None:
        # The view falls back to the whole body when there is no "athan" key.
        with mock.patch("casasmart.audio_api.get_audio_adapter", lambda hass: None):
            resp = await self.view.put(
                H.FakeRequest(
                    headers=self._admin(), body={"enabled": False, "city": "Riyadh"}
                )
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["athan"]["city"], "Riyadh")

    async def test_put_enabled_non_bool_is_400(self) -> None:
        # enabled must be a real boolean — 1 is rejected by the engine.
        resp = await self.view.put(
            H.FakeRequest(
                headers=self._admin(),
                body={"athan": {"enabled": 1, "lat": 21, "lon": 39}},
            )
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.rt.audio.get_athan(), {})  # nothing stored

    async def test_put_enabled_without_finite_lat_is_400(self) -> None:
        # enabled:true but lat is not a finite number — the validator rejects it
        # (a true NaN/inf can't ride through JSON, so use a non-numeric value
        # that the same _is_number gate refuses).
        resp = await self.view.put(
            H.FakeRequest(
                headers=self._admin(),
                body={"athan": {"enabled": True, "lat": "not-a-number", "lon": 39}},
            )
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.rt.audio.get_athan(), {})

    async def test_put_requires_manage_user_is_403(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._user(), body={"athan": {"enabled": False}})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)


# --------------------------------------------------------------------------- #
# Broker config — REDACTION
# --------------------------------------------------------------------------- #
class BrokerView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioBrokerView(self.hass)

    async def test_get_redacts_password(self) -> None:
        self.rt.audio.set_broker(
            host="10.0.0.5", username="mq", password="super-secret"
        )
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        broker = body["broker"]
        # The plaintext password NEVER leaves the hub.
        self.assertNotIn("password", broker)
        self.assertNotIn("super-secret", _raw_body(resp))
        # Only a boolean "set" flag is exposed.
        self.assertTrue(broker["password_set"])
        self.assertEqual(broker["host"], "10.0.0.5")

    async def test_get_password_set_false_when_unset(self) -> None:
        self.rt.audio.set_broker(host="10.0.0.5")
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, body = H.read_response(resp)
        self.assertFalse(body["broker"]["password_set"])

    async def test_put_good_host_updates(self) -> None:
        # No adapter present -> get_audio_adapter would AttributeError, so patch
        # it to None (the reconnect is best-effort and skipped on None).
        with mock.patch("casasmart.audio_api.get_audio_adapter", lambda hass: None):
            resp = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"host": "192.168.1.50"})
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["broker"]["host"], "192.168.1.50")
        self.assertEqual(self.rt.audio.get_broker()["host"], "192.168.1.50")

    async def test_put_garbage_host_is_400(self) -> None:
        with mock.patch("casasmart.audio_api.get_audio_adapter", lambda hass: None):
            resp = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"host": "not a host"})
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertIsNone(self.rt.audio.get_broker()["host"])

    async def test_get_requires_manage_user_is_403(self) -> None:
        resp = await self.view.get(H.FakeRequest(headers=self._user()))
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)


# --------------------------------------------------------------------------- #
# PA config — mirror broker, api_key redaction
# --------------------------------------------------------------------------- #
class PaConfigView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioPaConfigView(self.hass)

    async def test_get_redacts_api_key(self) -> None:
        self.rt.audio.set_pa(host="10.0.0.9", api_key="pa-key-XYZ")
        resp = await self.view.get(H.FakeRequest(headers=self._admin()))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        pa = body["pa"]
        self.assertNotIn("api_key", pa)
        self.assertNotIn("pa-key-XYZ", _raw_body(resp))
        self.assertTrue(pa["api_key_set"])
        self.assertEqual(pa["host"], "10.0.0.9")

    async def test_put_good_host_updates(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"host": "10.0.0.9"})
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["pa"]["host"], "10.0.0.9")
        self.assertEqual(self.rt.audio.get_pa()["host"], "10.0.0.9")

    async def test_put_garbage_host_is_400(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._admin(), body={"host": "bad host name"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_put_requires_manage_user_is_403(self) -> None:
        resp = await self.view.put(
            H.FakeRequest(headers=self._user(), body={"host": "10.0.0.9"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)


# --------------------------------------------------------------------------- #
# Per-speaker command + broadcast — the control vocabulary
# --------------------------------------------------------------------------- #
class CommandView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioCommandView(self.hass)
        self.rt.audio.enroll_speaker("aabbccddeeff", "Kitchen")
        self.adapter = FakeAdapter()

    def _with_adapter(self):
        return mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter
        )

    async def test_valid_volume_builds_and_publishes(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(
                    headers=self._admin(), body={"cmd": "volume", "value": 40}
                ),
                mac6="ddeeff",
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["command"]["cmd"], "volume")
        self.assertEqual(body["command"]["value"], 40)
        self.assertEqual(body["topic"], "speakers/ddeeff/command")
        # It actually went out on the bus.
        self.assertEqual(len(self.adapter.published), 1)

    async def test_valid_stop_builds(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"cmd": "stop"}),
                mac6="ddeeff",
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["command"]["cmd"], "stop")

    async def test_unknown_command_is_400(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"cmd": "explode"}),
                mac6="ddeeff",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.adapter.published, [])  # nothing published

    async def test_non_string_command_is_400(self) -> None:
        # An unhashable JSON value used to escape as a TypeError (500).
        for cmd in ([], {"cmd": "stop"}):
            with self.subTest(cmd=cmd), self._with_adapter():
                resp = await self.view.post(
                    H.FakeRequest(headers=self._admin(), body={"cmd": cmd}),
                    mac6="ddeeff",
                )
                self.assertEqual(H.read_response(resp)[0], 400)
        self.assertEqual(self.adapter.published, [])

    async def test_play_is_not_a_control_command(self) -> None:
        # "play" is deliberately excluded from the per-speaker control path —
        # audio sources must go via broadcast/PA, never smuggled through command.
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(
                    headers=self._admin(),
                    body={"cmd": "play", "value": "http://x/a.mp3"},
                ),
                mac6="ddeeff",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.adapter.published, [])

    async def test_volume_out_of_range_is_400(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(
                    headers=self._admin(), body={"cmd": "volume", "value": 999}
                ),
                mac6="ddeeff",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_command_unknown_speaker_is_404(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"cmd": "stop"}),
                mac6="000000",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_command_requires_control_no_token_is_401(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers={}, body={"cmd": "stop"}), mac6="ddeeff"
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)


class AirplayView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioAirplayView(self.hass)
        self.rt.audio.enroll_speaker("aabbccddeeff", "Kitchen")
        self.adapter = FakeAdapter()

    def _with_adapter(self):
        return mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter
        )

    async def test_playpause_publishes_raw_verb_to_remote_topic(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"action": "playpause"}),
                mac6="ddeeff",
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "playpause")
        self.assertEqual(body["topic"], "speakers/ddeeff/airplay/remote")
        # RAW string (shairport expects a bare verb, not JSON), non-retained —
        # it's a fire-and-forget command, not persisted state.
        topic, payload, _qos, retain = self.adapter.published[0]
        self.assertEqual(topic, "speakers/ddeeff/airplay/remote")
        self.assertEqual(payload, "playpause")
        self.assertIsInstance(payload, str)
        self.assertFalse(retain)

    async def test_next_and_previous_map_to_dacp_verbs(self) -> None:
        with self._with_adapter():
            r1 = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"action": "next"}),
                mac6="ddeeff",
            )
            r2 = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"action": "previous"}),
                mac6="ddeeff",
            )
        self.assertEqual(H.read_response(r1)[1]["action"], "nextitem")
        self.assertEqual(H.read_response(r2)[1]["action"], "previtem")
        self.assertEqual(self.adapter.published[0][1], "nextitem")
        self.assertEqual(self.adapter.published[1][1], "previtem")

    async def test_unknown_action_is_400(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"action": "yeet"}),
                mac6="ddeeff",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.adapter.published, [])  # nothing published

    async def test_unknown_speaker_is_404(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"action": "playpause"}),
                mac6="000000",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_requires_control_no_token_is_401(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers={}, body={"action": "playpause"}),
                mac6="ddeeff",
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)


class BroadcastView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioBroadcastView(self.hass)
        self.adapter = FakeAdapter()

    def _with_adapter(self):
        return mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter
        )

    async def test_valid_play_url_broadcasts(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"url": "http://x/clip.mp3"})
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["command"]["cmd"], "play")
        self.assertEqual(body["command"]["url"], "http://x/clip.mp3")
        self.assertEqual(body["topic"], "speakers/broadcast")
        self.assertEqual(len(self.adapter.published), 1)

    async def test_both_url_and_file_is_400(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(
                    headers=self._admin(),
                    body={"url": "http://x/a.mp3", "file": "b.mp3"},
                )
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.adapter.published, [])

    async def test_neither_url_nor_file_is_400(self) -> None:
        with self._with_adapter():
            resp = await self.view.post(
                H.FakeRequest(headers=self._admin(), body={"volume": 30})
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- #
# _parse_targets — the pure module helper (PA multipart POST itself is SKIPPED)
# --------------------------------------------------------------------------- #
@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class ParseTargets(unittest.TestCase):
    """Direct unit test of the pure helper — the multipart UPLOAD body around it
    is intentionally NOT driven (faking aiohttp multipart is brittle); only the
    targets-normalisation logic is pinned here."""

    def test_empty_and_blank(self) -> None:
        self.assertEqual(_parse_targets(""), [])
        self.assertEqual(_parse_targets("   "), [])
        self.assertEqual(_parse_targets(None), [])

    def test_comma_split_normalises_to_mac6(self) -> None:
        # Full MACs and bare ids both collapse to the canonical last-6 hex.
        out = _parse_targets("AA:BB:CC:DD:EE:FF, 112233")
        self.assertEqual(out, ["ddeeff", "112233"])

    def test_whitespace_split(self) -> None:
        self.assertEqual(_parse_targets("aabbcc  ddeeff"), ["aabbcc", "ddeeff"])

    def test_json_array(self) -> None:
        self.assertEqual(_parse_targets('["aabbcc", "112233"]'), ["aabbcc", "112233"])

    def test_dedupes_preserving_order(self) -> None:
        self.assertEqual(_parse_targets("aabbcc,aabbcc,112233"), ["aabbcc", "112233"])

    def test_drops_invalid_silently(self) -> None:
        # "zzz" is not hex; it is dropped, the valid id survives.
        self.assertEqual(_parse_targets("aabbcc, zzzzzz"), ["aabbcc"])


# --------------------------------------------------------------------------- #
# Provision — NO JWT, the response IS the broker secret
# --------------------------------------------------------------------------- #
# The hub_config.json key an operator sets to allow keyless LAN provisioning.
_KEYLESS = "keyless_speaker_provisioning"
_PROVISION_KEY = "test-provision-key"


class ProvisionView(AudioViewTestCase):
    """The provisioning key is required; keyless LAN access is an opt-in.

    A valid ``X-CasaSmart-Provision-Key`` works from any source. Without one
    the hub refuses even a LAN client, unless hub_config sets
    ``keyless_speaker_provisioning`` to exactly ``true`` — then the LAN gate
    admits it, as every hub did before.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioProvisionView(self.hass)
        # The broker secret the speaker agent needs to connect.
        self.rt.audio.set_broker(
            host="broker.local", username="pi", password="s3cret-pw"
        )
        self.rt.hub_config.set(PROVISION_SECRET_CONFIG_KEY, _PROVISION_KEY)

    async def _get(self, *, lan: bool, key: str | None = None):
        headers = {"X-CasaSmart-Provision-Key": key} if key is not None else {}
        remote = "192.168.1.50" if lan else "203.0.113.9"
        with mock.patch("casasmart.audio_api.is_lan_request", return_value=lan):
            resp = await self.view.get(H.FakeRequest(headers=headers, remote=remote))
        return resp

    def _assert_refused(self, resp) -> None:
        status, body = H.read_response(resp)
        self.assertEqual(status, 403)
        self.assertIn("provisioning key", body["message"])
        self.assertNotIn("s3cret-pw", _raw_body(resp))

    def _assert_served(self, resp) -> None:
        # No JWT: the response IS the secret, so — unlike GET /audio/broker —
        # the password is NOT redacted.
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["broker"]["username"], "pi")
        self.assertEqual(body["broker"]["password"], "s3cret-pw")

    async def test_the_setting_lives_in_hub_config_under_its_documented_key(
        self,
    ) -> None:
        from casasmart import const

        self.assertEqual(const.KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY, _KEYLESS)

    async def test_lan_without_key_is_refused_by_default(self) -> None:
        self._assert_refused(await self._get(lan=True))

    async def test_lan_without_key_is_refused_unless_exactly_true(self) -> None:
        for value in (False, "true", "on", 1, None):
            with self.subTest(value=value):
                self.rt.hub_config.set(_KEYLESS, value)
                self._assert_refused(await self._get(lan=True))

    async def test_lan_without_key_is_served_when_keyless_is_on(self) -> None:
        self.rt.hub_config.set(_KEYLESS, True)
        self._assert_served(await self._get(lan=True))

    async def test_valid_key_is_served_from_anywhere_in_both_modes(self) -> None:
        for keyless in (False, True):
            self.rt.hub_config.set(_KEYLESS, keyless)
            for lan in (False, True):
                with self.subTest(keyless=keyless, lan=lan):
                    self._assert_served(await self._get(lan=lan, key=_PROVISION_KEY))

    async def test_wrong_key_is_refused(self) -> None:
        self._assert_refused(await self._get(lan=True, key="wrong-key"))
        self.rt.hub_config.set(_KEYLESS, True)
        self._assert_refused(await self._get(lan=False, key="wrong-key"))

    async def test_non_ascii_key_is_refused_like_a_wrong_key(self) -> None:
        # aiohttp passes non-ASCII header bytes through as text, and
        # hmac.compare_digest raises TypeError for non-ASCII str arguments.
        for key in ("١٢٣", "café", "\udcff\udcfe"):
            with self.subTest(key=key):
                self._assert_refused(await self._get(lan=False, key=key))

    async def test_a_non_ascii_provisioning_key_still_works(self) -> None:
        # provision_secret lives in hub_config.json, so an operator may set it.
        self.rt.hub_config.set(PROVISION_SECRET_CONFIG_KEY, "clé-secrète")
        self._assert_served(await self._get(lan=False, key="clé-secrète"))
        self._assert_refused(await self._get(lan=False, key="cle-secrete"))

    async def test_non_lan_without_key_is_refused_in_both_modes(self) -> None:
        # A leaked/photographed provision URL is useless off-LAN.
        for keyless in (False, True):
            with self.subTest(keyless=keyless):
                self.rt.hub_config.set(_KEYLESS, keyless)
                self._assert_refused(await self._get(lan=False))

    async def test_refusal_logs_never_carry_the_secrets(self) -> None:
        with self.assertLogs("casasmart.audio_api", level="WARNING") as logs:
            self._assert_refused(await self._get(lan=True, key="wrong-key"))
            self._assert_refused(await self._get(lan=False))
        for line in logs.output:
            for secret in ("s3cret-pw", _PROVISION_KEY, "wrong-key"):
                self.assertNotIn(secret, line)


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class KeylessProvisioningSetupWarning(unittest.TestCase):
    """Turning keyless provisioning on is visible in the HA log at setup."""

    def _setup_with(self, value=None):
        """Run the setup check over a hub_config holding ``value`` (None: unset)."""
        integration = H.import_integration()
        hub_config = H.FakeHubConfig()
        hub_config.set(PROVISION_SECRET_CONFIG_KEY, _PROVISION_KEY)
        if value is not None:
            hub_config.set(_KEYLESS, value)
        integration._warn_keyless_speaker_provisioning(
            hub_config, Path("/config/casasmart")
        )

    def test_on_logs_a_warning_without_any_secret(self) -> None:
        with self.assertLogs("casasmart", level="WARNING") as logs:
            self._setup_with(True)
        self.assertEqual(len(logs.records), 1)
        message = logs.output[0]
        self.assertIn(_KEYLESS, message)
        self.assertIn("password", message)
        self.assertNotIn(_PROVISION_KEY, message)

    def test_off_says_nothing(self) -> None:
        for value in (None, False, "true", 1):
            with self.subTest(value=value), self.assertNoLogs("casasmart"):
                self._setup_with(value)


class SpeakerScope(AudioViewTestCase):
    """A room-scoped user sees only its rooms' speakers (the speaker's
    free-text room label matched vs the caller's scoped area names); an unroomed
    speaker is shared house infra; admin/unscoped sees all; fail-closed."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioSpeakersView(self.hass)
        self.rt.audio.enroll_speaker("aabbccddee01", "Kitchen Spk", "Kitchen")
        self.rt.audio.enroll_speaker("aabbccddee02", "Bedroom Spk", "Bedroom")
        self.rt.audio.enroll_speaker("aabbccddee03", "Hall Spk", None)  # no room

    def _areas(self, mapping):
        """Patch the HA area registry: area_id -> area(name)."""
        reg = mock.Mock()

        def _get_area(aid):
            if aid not in mapping:
                return None
            area = mock.Mock()
            area.name = mapping[aid]
            return area

        reg.async_get_area.side_effect = _get_area
        return mock.patch("casasmart.audio_api.ar.async_get", return_value=reg)

    async def _names(self, hdr):
        resp = await self.view.get(H.FakeRequest(headers=hdr))
        _, body = H.read_response(resp)
        return sorted(s["name"] for s in body["speakers"])

    async def test_admin_sees_all(self) -> None:
        self.assertEqual(
            await self._names(self._admin()),
            ["Bedroom Spk", "Hall Spk", "Kitchen Spk"],
        )

    async def test_scoped_user_sees_its_room_plus_unroomed(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user", rooms=["area-kitchen"])
        with self._areas({"area-kitchen": "Kitchen"}):
            names = await self._names(hdr)
        # Kitchen (label match) + Hall (no room = house-wide); Bedroom hidden.
        self.assertEqual(names, ["Hall Spk", "Kitchen Spk"])

    async def test_scoped_user_unmatched_label_is_fail_closed(self) -> None:
        # The caller's scope resolves to an area no speaker room matches -> only
        # the unroomed (house-wide) speaker shows. Fail-closed (no leak).
        _, hdr = H.session(self.rt.auth, role="user", rooms=["area-garage"])
        with self._areas({"area-garage": "Garage"}):
            names = await self._names(hdr)
        self.assertEqual(names, ["Hall Spk"])

    async def test_scoped_user_matches_by_area_id_exactly(self) -> None:
        # A speaker assigned an area_id is scoped by EXACT id membership — no
        # dependence on the free-text label matching the area name.
        self.rt.audio.enroll_speaker(
            "aabbccddee04", "Studio Sonos", "some label", area_id="area-studio"
        )
        _, hdr = H.session(self.rt.auth, role="user", rooms=["area-studio"])
        with self._areas({"area-studio": "Studio"}):
            names = await self._names(hdr)
        # Studio speaker (id match) + Hall (house-wide); label never consulted.
        self.assertEqual(names, ["Hall Spk", "Studio Sonos"])

    async def test_area_id_out_of_scope_is_hidden(self) -> None:
        # area_id present but not in scope -> hidden even if the label would
        # have matched the caller's area name (id takes priority, fail-closed).
        self.rt.audio.enroll_speaker(
            "aabbccddee05", "Sneaky", "Kitchen", area_id="area-bedroom"
        )
        _, hdr = H.session(self.rt.auth, role="user", rooms=["area-kitchen"])
        with self._areas({"area-kitchen": "Kitchen"}):
            names = await self._names(hdr)
        # Kitchen Spk (legacy label match) + Hall; "Sneaky" hidden despite its
        # Kitchen *label*, because its area_id is area-bedroom.
        self.assertEqual(names, ["Hall Spk", "Kitchen Spk"])


class _Part:
    """One multipart part, as aiohttp's reader yields it to the PA view."""

    def __init__(self, name, data: bytes, filename=None, content_type=None) -> None:
        self.name = name
        self.filename = filename
        self.headers = {"Content-Type": content_type} if content_type else {}
        self._data = data
        self._read = False

    async def text(self) -> str:
        return self._data.decode()

    async def read_chunk(self) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._data


class _Reader:
    def __init__(self, parts) -> None:
        self._parts = list(parts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._parts:
            raise StopAsyncIteration
        return self._parts.pop(0)


class _PaRequest(H.FakeRequest):
    """A PA upload: an ``audio`` file part plus an optional ``targets`` field."""

    def __init__(self, headers, targets=None) -> None:
        super().__init__(headers=headers)
        self._parts = []
        if targets is not None:
            self._parts.append(_Part("targets", ",".join(targets).encode()))
        self._parts.append(
            _Part("audio", b"\x00clip", filename="pa.m4a", content_type="audio/mp4")
        )

    async def multipart(self):
        return _Reader(self._parts)


class ScopedSpeakerControl(AudioViewTestCase):
    """Control follows the speaker list's room scope.

    A room-scoped caller may control exactly the speakers ``GET /speakers``
    shows it: its rooms' speakers plus the unassigned (house-wide) ones. A
    speaker outside that set answers exactly like an unknown one (no
    enumeration), and a whole-home play reaches only the visible speakers.
    Unscoped callers keep the whole-home broadcast topic.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rt.audio.enroll_speaker("aabbcc000001", "Mine", area_id="room-a")
        self.rt.audio.enroll_speaker("aabbcc000002", "Hidden", area_id="room-b")
        self.rt.audio.enroll_speaker("aabbcc000003", "Hall")  # unassigned
        self.adapter = FakeAdapter()
        registry = mock.Mock()
        registry.async_get_area.side_effect = lambda area_id: SimpleNamespace(
            name=area_id
        )
        for target, value in (
            ("casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter),
            ("casasmart.audio_api.ar.async_get", lambda hass: registry),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        _, self.scoped = H.session(self.rt.auth, role="user", rooms=["room-a"])

    def _topics(self) -> list[str]:
        return [topic for topic, *_ in self.adapter.published]

    async def _command(self, headers, mac6):
        return H.read_response(
            await CasaSmartAudioCommandView(self.hass).post(
                H.FakeRequest(headers=headers, body={"cmd": "volume", "value": 30}),
                mac6=mac6,
            )
        )

    async def _airplay(self, headers, mac6):
        return H.read_response(
            await CasaSmartAudioAirplayView(self.hass).post(
                H.FakeRequest(headers=headers, body={"action": "pause"}), mac6=mac6
            )
        )

    async def _broadcast(self, headers):
        return H.read_response(
            await CasaSmartAudioBroadcastView(self.hass).post(
                H.FakeRequest(headers=headers, body={"url": "http://x/clip.mp3"})
            )
        )

    async def _pa(self, headers, targets=None):
        return H.read_response(
            await CasaSmartAudioPaView(self.hass).post(_PaRequest(headers, targets))
        )

    async def test_hidden_speaker_answers_like_an_unknown_one(self) -> None:
        for send in (self._command, self._airplay):
            with self.subTest(handler=send.__name__):
                hidden = await send(self.scoped, "000002")
                unknown = await send(self.scoped, "0000ff")
                self.assertEqual(hidden[0], 404)
                self.assertEqual(unknown[0], 404)
                self.assertEqual(
                    hidden[1]["message"],
                    unknown[1]["message"].replace("0000ff", "000002"),
                )
        self.assertEqual(self.adapter.published, [])

    async def test_own_and_unassigned_speakers_stay_controllable(self) -> None:
        for mac6 in ("000001", "000003"):
            with self.subTest(mac6=mac6):
                self.assertEqual((await self._command(self.scoped, mac6))[0], 200)
                self.assertEqual((await self._airplay(self.scoped, mac6))[0], 200)
        self.assertEqual(
            self._topics(),
            [
                "speakers/000001/command",
                "speakers/000001/airplay/remote",
                "speakers/000003/command",
                "speakers/000003/airplay/remote",
            ],
        )

    async def test_scoped_broadcast_fans_out_to_visible_speakers_only(self) -> None:
        status, body = await self._broadcast(self.scoped)
        self.assertEqual(status, 200)
        self.assertEqual(body["played_on"], ["000001", "000003"])
        self.assertEqual(
            self._topics(), ["speakers/000001/command", "speakers/000003/command"]
        )
        # Each speaker gets the same play the whole-home topic would carry.
        for _topic, payload, *_ in self.adapter.published:
            self.assertEqual(payload["cmd"], "play")
            self.assertEqual(payload["url"], "http://x/clip.mp3")

    async def test_scoped_pa_without_targets_fans_out_to_visible_speakers(self) -> None:
        status, body = await self._pa(self.scoped)
        self.assertEqual(status, 200)
        self.assertEqual(body["played_on"], ["000001", "000003"])
        self.assertEqual(
            self._topics(), ["speakers/000001/command", "speakers/000003/command"]
        )

    async def test_scoped_pa_with_a_hidden_target_is_refused_whole(self) -> None:
        for targets in (["000002"], ["000001", "000002"], ["000001", "0000ff"]):
            with self.subTest(targets=targets):
                status, body = await self._pa(self.scoped, targets)
                self.assertEqual(status, 404)
                self.assertEqual(
                    body["message"], f"No speaker enrolled under {targets[-1]!r}"
                )
        self.assertEqual(self.adapter.published, [])
        self.assertEqual(_pa_store(self.hass)._clips, {})  # no clip hosted

    async def test_scoped_pa_with_visible_targets_plays_on_them(self) -> None:
        status, body = await self._pa(self.scoped, ["000003", "000001"])
        self.assertEqual(status, 200)
        self.assertEqual(body["played_on"], ["000003", "000001"])
        self.assertEqual(
            self._topics(), ["speakers/000003/command", "speakers/000001/command"]
        )

    async def test_empty_scope_reaches_only_unassigned_speakers(self) -> None:
        _, nothing = H.session(self.rt.auth, role="user", rooms=[])
        self.assertEqual((await self._command(nothing, "000001"))[0], 404)
        self.assertEqual((await self._command(nothing, "000003"))[0], 200)
        self.adapter.published.clear()
        status, body = await self._broadcast(nothing)
        self.assertEqual((status, body["played_on"]), (200, ["000003"]))
        self.assertEqual(self._topics(), ["speakers/000003/command"])

    async def test_no_visible_speaker_is_a_clear_404(self) -> None:
        self.rt.audio.remove_speaker("000003")
        _, nothing = H.session(self.rt.auth, role="user", rooms=["room-c"])
        for status, body in (await self._broadcast(nothing), await self._pa(nothing)):
            self.assertEqual(status, 404)
            self.assertEqual(body["message"], "No speakers in your rooms")
        self.assertEqual(self.adapter.published, [])
        self.assertEqual(_pa_store(self.hass)._clips, {})

    async def test_unscoped_callers_keep_the_whole_home_topic(self) -> None:
        _, unscoped_user = H.session(self.rt.auth, role="user")
        for headers in (self._admin(), unscoped_user):
            with self.subTest(headers=headers):
                self.adapter.published.clear()
                self.assertEqual((await self._command(headers, "000002"))[0], 200)
                status, body = await self._broadcast(headers)
                self.assertEqual((status, body["topic"]), (200, "speakers/broadcast"))
                status, body = await self._pa(headers)
                self.assertEqual((status, body["played_on"]), (200, "all"))
                # An unknown PA target is still skipped, not fatal.
                status, body = await self._pa(headers, ["000002", "0000ff"])
                self.assertEqual((status, body["played_on"]), (200, ["000002"]))
                self.assertEqual(
                    self._topics(),
                    [
                        "speakers/000002/command",
                        "speakers/broadcast",
                        "speakers/broadcast",
                        "speakers/000002/command",
                    ],
                )


class _StreamProtocol:
    """The parts of aiohttp's protocol a request StreamReader touches."""

    _reading_paused = False

    def pause_reading(self, **_kwargs) -> None:
        pass

    def resume_reading(self, **_kwargs) -> None:
        pass


def _raw_request(headers: dict, body: bytes):
    """A real aiohttp request carrying ``body``, so ``multipart()`` parses it."""
    from aiohttp import streams
    from aiohttp.test_utils import make_mocked_request

    payload = streams.StreamReader(
        _StreamProtocol(), 2**16, loop=asyncio.get_running_loop()
    )
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST", "/api/casasmart/audio/pa", headers=headers, payload=payload
    )


_BOUNDARY = "multipart/form-data; boundary=XyZ"
_AUDIO_PART = (
    b'--XyZ\r\nContent-Disposition: form-data; name="audio"; filename="a.mp3"\r\n'
    b"Content-Type: audio/mpeg\r\n\r\n\x00\x01\r\n--XyZ--\r\n"
)


def _part(headers: bytes, value: bytes) -> bytes:
    return b"--XyZ\r\n" + headers + b"\r\n\r\n" + value + b"\r\n"


class PaUploadBody(AudioViewTestCase):
    """A PA upload body aiohttp can't parse is a 400, never a 500."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter = FakeAdapter()
        patcher = mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _post(self, body: bytes, content_type: str | None = _BOUNDARY):
        headers = dict(self._admin())
        if content_type is not None:
            headers["Content-Type"] = content_type
        view = CasaSmartAudioPaView(self.hass)
        return H.read_response(await view.post(_raw_request(headers, body)))

    async def test_unparseable_bodies_are_a_400(self) -> None:
        targets = b'Content-Disposition: form-data; name="targets"'
        cases = {
            "no content type": (b"hello", None),
            "no starting boundary": (b"garbage", _BOUNDARY),
            "undecodable targets": (
                _part(targets, b"\xff\xfe") + _AUDIO_PART,
                _BOUNDARY,
            ),
            "unknown charset": (
                _part(targets + b"\r\nContent-Type: text/plain; charset=nope", b"x")
                + _AUDIO_PART,
                _BOUNDARY,
            ),
            "malformed part header": (
                _part(b"Content-Disposition form-data", b"x") + b"--XyZ--\r\n",
                _BOUNDARY,
            ),
            "part header too long": (
                _part(b"X-A: " + b"a" * 20000, b"x") + b"--XyZ--\r\n",
                _BOUNDARY,
            ),
            "truncated part": (
                b'--XyZ\r\nContent-Disposition: form-data; name="audio"\r\n\r\nzz',
                _BOUNDARY,
            ),
            "nested multipart": (
                _part(
                    b"Content-Type: multipart/mixed; boundary=QQ",
                    b"--QQ\r\n\r\nx\r\n--QQ--",
                )
                + b"--XyZ--\r\n",
                _BOUNDARY,
            ),
        }
        for name, (body, content_type) in cases.items():
            with self.subTest(case=name):
                status, _ = await self._post(body, content_type)
                self.assertEqual(status, 400)
        self.assertEqual(self.adapter.published, [])
        self.assertEqual(_pa_store(self.hass)._clips, {})

    async def test_a_well_formed_upload_still_plays(self) -> None:
        status, body = await self._post(_AUDIO_PART)
        self.assertEqual((status, body["played_on"]), (200, "all"))
        self.assertEqual(
            [t for t, *_ in self.adapter.published], ["speakers/broadcast"]
        )


class _OfflinePaho:
    """A paho client that is running but can't reach its broker."""

    def __init__(self, client_id) -> None:
        self.published: list = []

    def __getattr__(self, name):  # username_pw_set, connect_async, loop_start...
        return lambda *args, **kwargs: None

    def is_connected(self) -> bool:
        return False

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append(topic)


class BrokerUnreachable(AudioViewTestCase):
    """With the broker down, control fails fast with a 503 instead of queueing."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from casasmart.audio_adapter import AudioAdapter

        self.rt.audio.enroll_speaker("aabbccddeeff", "Kitchen")
        self.rt.audio.set_broker(host="broker.local")
        clients = []
        self.adapter = AudioAdapter(
            self.hass,
            self.rt.audio,
            client_factory=lambda cid: clients.append(_OfflinePaho(cid)) or clients[-1],
        )
        await self.adapter.async_start()
        self.client = clients[0]
        patcher = mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: self.adapter
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_command_broadcast_and_pa_answer_503(self) -> None:
        calls = {
            "command": CasaSmartAudioCommandView(self.hass).post(
                H.FakeRequest(headers=self._admin(), body={"cmd": "stop"}),
                mac6="ddeeff",
            ),
            "broadcast": CasaSmartAudioBroadcastView(self.hass).post(
                H.FakeRequest(headers=self._admin(), body={"url": "http://x/a.mp3"})
            ),
            "pa": CasaSmartAudioPaView(self.hass).post(_PaRequest(self._admin())),
        }
        for name, call in calls.items():
            with self.subTest(endpoint=name):
                status, body = H.read_response(await call)
                self.assertEqual(status, 503)
                self.assertIn("broker", body["message"])
        self.assertEqual(self.client.published, [])


if __name__ == "__main__":
    unittest.main()
