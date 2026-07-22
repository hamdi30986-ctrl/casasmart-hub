"""View-layer tests for ``audio_api`` — the speaker/athan/broker wire contract.

Pins the Phase 6/7 logic that lives in the VIEW + the thin engine validation it
surfaces as HTTP status codes:
* speakers GET/POST (enroll), speaker PUT/DELETE — registry round-trip + bad-mac
  rejection.
* athan GET/PUT — the Phase-7 opaque-blob round-trip (extra scheduler keys
  survive) + the enabled/lat validation path.
* broker + pa-config GET — secret REDACTION (never plaintext).
* command + broadcast — command vocabulary (volume/stop ok, unknown 400, play is
  not a per-speaker control).
* AUTH gate — a role lacking ``audio.manage`` -> 403, no token -> 401.
* ``_parse_targets`` — the pure module helper, unit-tested directly (the PA
  multipart UPLOAD body is hard to fake, so the multipart POST is SKIPPED; only
  ``_parse_targets`` is covered here).

Container/CI only (imports Home Assistant). Run:
    docker exec -w /config/tests homeassistant python3 -m unittest test_audio_api -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H  # noqa: E402

try:
    from casasmart.audio_api import (
        CasaSmartAudioSpeakersView,
        CasaSmartAudioSpeakerView,
        CasaSmartAudioAthanView,
        CasaSmartAudioBrokerView,
        CasaSmartAudioPaConfigView,
        CasaSmartAudioCommandView,
        CasaSmartAudioAirplayView,
        CasaSmartAudioBroadcastView,
        CasaSmartAudioProvisionView,
        _parse_targets,
    )
    from casasmart.const import EVENT_AUDIO_CHANGED

    _ERR = None
except Exception as err:  # noqa: BLE001 — HA-free local env
    CasaSmartAudioSpeakersView = CasaSmartAudioSpeakerView = None
    CasaSmartAudioAthanView = CasaSmartAudioBrokerView = None
    CasaSmartAudioPaConfigView = CasaSmartAudioCommandView = None
    CasaSmartAudioBroadcastView = _parse_targets = None
    CasaSmartAudioProvisionView = None
    EVENT_AUDIO_CHANGED = None
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
        self.assertIn(
            EVENT_AUDIO_CHANGED, [evt for evt, _ in self.hass.bus.fired]
        )

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
        stored = next(
            s for s in self.rt.audio.speakers() if s["mac6"] == "aabbcc"
        )
        self.assertEqual(stored["custom_icon"], "sonos")

    async def test_get_speaker_shape_has_custom_icon_key(self) -> None:
        # A speaker enrolled with no icon still serves the key (null).
        self.rt.audio.enroll_speaker("112233", "No Icon", None)
        served = next(
            s for s in self.rt.audio.speakers() if s["mac6"] == "112233"
        )
        self.assertIn("custom_icon", served)
        self.assertIsNone(served["custom_icon"])

    async def test_post_bad_mac_is_400(self) -> None:
        # "zzzz" is not hex -> normalize_mac6 raises AudioError -> 400.
        resp = await self.view.post(
            H.FakeRequest(
                headers=self._admin(), body={"mac": "zzzzzz", "name": "Bad"}
            )
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)
        self.assertEqual(self.rt.audio.speakers(), [])

    async def test_post_missing_name_is_400(self) -> None:
        resp = await self.view.post(
            H.FakeRequest(
                headers=self._admin(), body={"mac": "aabbcc", "name": ""}
            )
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_post_requires_manage_user_is_403(self) -> None:
        # "user" role lacks audio.manage (admin/sub-admin only).
        resp = await self.view.post(
            H.FakeRequest(
                headers=self._user(), body={"mac": "aabbcc", "name": "Nope"}
            )
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
        stored = next(
            s for s in self.rt.audio.speakers() if s["mac6"] == "ddeeff"
        )
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
        stored = next(
            s for s in self.rt.audio.speakers() if s["mac6"] == "ddeeff"
        )
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

    async def test_put_stores_and_extra_keys_survive(self) -> None:
        # Phase-7 opaque-blob round-trip: the hub does NOT model per_prayer / a
        # nested override map, yet those EXTRA keys must survive PUT -> GET.
        config = {
            "enabled": True,
            "lat": 21,
            "lon": 39,
            "per_prayer": {"fajr": 5},
            "method": "UmmAlQura",
        }
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: None
        ):
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
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: None
        ):
            put = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"athan": config})
            )
        self.assertEqual(H.read_response(put)[0], 200)
        get = await self.view.get(H.FakeRequest(headers=self._admin()))
        _, get_body = H.read_response(get)
        self.assertEqual(get_body["athan"]["speakers"], ["aabbcc", "ddeeff"])

    async def test_put_accepts_bare_config_without_athan_wrapper(self) -> None:
        # The view falls back to the whole body when there is no "athan" key.
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: None
        ):
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
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: None
        ):
            resp = await self.view.put(
                H.FakeRequest(headers=self._admin(), body={"host": "192.168.1.50"})
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["broker"]["host"], "192.168.1.50")
        self.assertEqual(self.rt.audio.get_broker()["host"], "192.168.1.50")

    async def test_put_garbage_host_is_400(self) -> None:
        with mock.patch(
            "casasmart.audio_api.get_audio_adapter", lambda hass: None
        ):
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
                H.FakeRequest(
                    headers=self._admin(), body={"action": "playpause"}
                ),
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
                H.FakeRequest(
                    headers=self._admin(), body={"url": "http://x/clip.mp3"}
                )
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
# Provision — LAN-only, NO JWT, the response IS the broker secret
# --------------------------------------------------------------------------- #
class ProvisionView(AudioViewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.view = CasaSmartAudioProvisionView(self.hass)
        # The broker secret the LAN speaker agent needs to connect.
        self.rt.audio.set_broker(
            host="broker.local", username="pi", password="s3cret-pw"
        )

    async def test_non_lan_source_is_403(self) -> None:
        # A leaked/photographed provision URL is useless off-LAN.
        with mock.patch(
            "casasmart.audio_api.get_extra_lan_cidrs", return_value=[]
        ), mock.patch(
            "casasmart.audio_api.is_lan_request", return_value=False
        ):
            resp = await self.view.get(
                H.FakeRequest(headers={}, remote="8.8.8.8")
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)

    async def test_lan_source_returns_plaintext_creds_no_jwt(self) -> None:
        # LAN-only with NO JWT: the response IS the secret (it never leaves the
        # LAN), so — unlike GET /audio/broker — the password is NOT redacted.
        with mock.patch(
            "casasmart.audio_api.get_extra_lan_cidrs", return_value=[]
        ), mock.patch(
            "casasmart.audio_api.is_lan_request", return_value=True
        ):
            resp = await self.view.get(
                H.FakeRequest(headers={}, remote="192.168.1.50")  # no token
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)  # served with no Authorization at all
        broker = body["broker"]
        self.assertEqual(broker["username"], "pi")
        self.assertEqual(broker["password"], "s3cret-pw")  # plaintext, by design
        self.assertIn("s3cret-pw", _raw_body(resp))  # cred source, not redacted


class SpeakerScope(AudioViewTestCase):
    """Phase 8 — a room-scoped user sees only its rooms' speakers (the speaker's
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
        # dependence on the free-text label matching the area name (SPK-5).
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


if __name__ == "__main__":
    unittest.main()
