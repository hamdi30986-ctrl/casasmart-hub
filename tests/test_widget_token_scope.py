"""View-layer tests: a widget token never reaches identity or push surfaces.

The home-screen widget holds a long-lived ``scope: widget`` token that the
auth engine caps to device read + control. The push-token and self-unpair
handlers used to gate on ``devices.read`` — inside that cap — so a widget
token could repoint (or drop) its owner's push registration and unpair the
owner's device, handing a last-admin hub back to its sticker code.

The engines are REAL (AuthEngine, PairingManager, PushTokenStore over a temp
HubStorage); widget tokens come from the real ``mint_widget_token``. Only
``hass`` + the request are ``view_harness`` fakes.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.auth_api import CasaSmartUnpairSelfView  # noqa: E402
from casasmart.const import BOOTSTRAP_CODE_HASH_CONFIG_KEY  # noqa: E402
from casasmart.pairing import (  # noqa: E402
    BOOTSTRAP_CODE_ID,
    CodeInvalidError,
    PairingManager,
    hash_code,
)
from casasmart.push import PushTokenStore  # noqa: E402
from casasmart.push_api import CasaSmartPushTokenView  # noqa: E402

try:  # These views pull in HA's recorder / registries: real Home Assistant only.
    from casasmart.api import (
        CasaSmartCommandView,
        CasaSmartDevicesView,
        CasaSmartDeviceView,
    )
    from casasmart.registry_api import CasaSmartFavoritesView
    from casasmart.settings_api import CasaSmartUserSettingsView
    from casasmart.suggestion_api import (
        CasaSmartGeneratedSuggestionActionView,
        CasaSmartSuggestionActionView,
    )
    from casasmart.user_settings import UserSettingsEngine

    _DEVICE_VIEWS_ERR: Exception | None = None
except Exception as err:
    _DEVICE_VIEWS_ERR = err

_STICKER_CODE = "TESTCODE"
_ROLES = ("admin", "sub-admin", "user")


class _FakeUserSettings:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, member_id: str) -> None:
        self.deleted.append(member_id)


class _HubTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        # Wired like __init__.py: the sticker hash persisted, the unpair path's
        # collaborators present, a real push-token store.
        self.rt.pairing = PairingManager(
            self.rt.storage.table("pairing_codes"), self.rt.auth.has_admin
        )
        self.rt.hub_config.set(BOOTSTRAP_CODE_HASH_CONFIG_KEY, hash_code(_STICKER_CODE))
        self.rt.user_settings = _FakeUserSettings()
        self.rt.push = PushTokenStore(self.rt.storage.table("push_tokens"))
        self.push_view = CasaSmartPushTokenView(self.hass)
        self.unpair_view = CasaSmartUnpairSelfView(self.hass)

    def _widget_headers(self, device_id: str) -> dict[str, str]:
        return H.bearer(self.rt.auth.mint_widget_token(device_id)["token"])


class WidgetTokenIsRefusedTests(_HubTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # The owner's phone paired and registered its own FCM destination.
        self.owner_id = H.enroll(self.rt.auth, role="admin")
        self.rt.push.register(self.owner_id, "owner-fcm-token", "android")
        self.push_before = self.rt.push.get_all_tokens()
        self.devices_before = self._enrollment()
        self.widget = self._widget_headers(self.owner_id)

    def _enrollment(self) -> list[dict]:
        # last_seen moves on any validated token, refused or not — not state.
        return [
            {k: v for k, v in device.items() if k != "last_seen"}
            for device in self.rt.auth.list_devices()
        ]

    def _assert_nothing_changed(self) -> None:
        self.assertEqual(self.rt.push.get_all_tokens(), self.push_before)
        self.assertEqual(self._enrollment(), self.devices_before)
        self.assertIsNotNone(self.rt.auth.get_device(self.owner_id))
        self.assertTrue(self.rt.auth.has_admin())
        # Still claimed: the printed sticker code must not have been re-armed.
        self.assertNotIn(BOOTSTRAP_CODE_ID, self.rt.pairing._codes)
        with self.assertRaises(CodeInvalidError):
            self.rt.pairing.redeem(_STICKER_CODE, "192.168.1.50")
        self.assertEqual(self.rt.user_settings.deleted, [])

    async def test_widget_token_cannot_replace_the_push_destination(self) -> None:
        status, _ = H.read_response(
            await self.push_view.post(
                H.FakeRequest(
                    headers=self.widget,
                    body={"fcm_token": "someone-elses-fcm", "platform": "android"},
                )
            )
        )
        self.assertEqual(status, 403)
        self._assert_nothing_changed()

    async def test_widget_token_cannot_remove_the_push_registration(self) -> None:
        status, _ = H.read_response(
            await self.push_view.delete(H.FakeRequest(headers=self.widget))
        )
        self.assertEqual(status, 403)
        self._assert_nothing_changed()

    async def test_widget_token_cannot_unpair_its_owner(self) -> None:
        status, _ = H.read_response(
            await self.unpair_view.post(H.FakeRequest(headers=self.widget))
        )
        self.assertEqual(status, 403)
        self._assert_nothing_changed()

    async def test_refused_for_every_role(self) -> None:
        for role in _ROLES:
            with self.subTest(role=role):
                device_id = H.enroll(self.rt.auth, role=role)
                self.rt.push.register(device_id, f"{role}-fcm", "ios")
                widget = self._widget_headers(device_id)
                before = self.rt.push.get_token(device_id)

                post, _ = H.read_response(
                    await self.push_view.post(
                        H.FakeRequest(
                            headers=widget,
                            body={"fcm_token": "other-fcm", "platform": "ios"},
                        )
                    )
                )
                delete, _ = H.read_response(
                    await self.push_view.delete(H.FakeRequest(headers=widget))
                )
                unpair, _ = H.read_response(
                    await self.unpair_view.post(H.FakeRequest(headers=widget))
                )

                self.assertEqual((post, delete, unpair), (403, 403, 403))
                self.assertEqual(self.rt.push.get_token(device_id), before)
                self.assertIsNotNone(self.rt.auth.get_device(device_id))


class SessionTokenStillWorksTests(_HubTestCase):
    async def test_every_role_registers_and_removes_its_push_token(self) -> None:
        for role in _ROLES:
            with self.subTest(role=role):
                device_id, headers = H.session(self.rt.auth, role=role)

                status, body = H.read_response(
                    await self.push_view.post(
                        H.FakeRequest(
                            headers=headers,
                            body={"fcm_token": f"{role}-fcm", "platform": "android"},
                        )
                    )
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, {"device_id": device_id, "registered": True})
                self.assertEqual(
                    self.rt.push.get_token(device_id)["fcm_token"], f"{role}-fcm"
                )

                status, body = H.read_response(
                    await self.push_view.delete(H.FakeRequest(headers=headers))
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, {"device_id": device_id, "removed": True})
                self.assertIsNone(self.rt.push.get_token(device_id))

    async def test_every_role_can_unpair_itself(self) -> None:
        # The admin goes last so the hub stays claimed while the others leave.
        for role in ("user", "sub-admin", "admin"):
            with self.subTest(role=role):
                device_id, headers = H.session(self.rt.auth, role=role)
                status, body = H.read_response(
                    await self.unpair_view.post(H.FakeRequest(headers=headers))
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["unpaired"], device_id)
                self.assertIsNone(self.rt.auth.get_device(device_id))


@unittest.skipIf(
    _DEVICE_VIEWS_ERR is not None,
    f"Home Assistant unavailable: {_DEVICE_VIEWS_ERR}",
)
class WidgetTokenKeepsItsDeviceSurfaceTests(_HubTestCase):
    """The narrowing above must not cost the widget what it is for."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.hass.states.add("light.lamp", state="on")
        device_id = H.enroll(self.rt.auth, role="user")
        self.widget = self._widget_headers(device_id)
        # The command view waits for the entity's state_changed after the
        # service call; answer it at once so the test doesn't sit out the
        # 2 s timeout.
        listeners: list = []

        def _listen(_event_type, handler):
            listeners.append(handler)
            return lambda: listeners.remove(handler)

        record_call = self.hass.services.async_call

        async def _call(domain, service, data, *, blocking=False):
            await record_call(domain, service, data, blocking=blocking)
            for handler in list(listeners):
                handler(types.SimpleNamespace(data={"entity_id": data["entity_id"]}))

        self.hass.bus.async_listen = _listen
        self.hass.services.async_call = _call
        for name, value in (
            ("is_served", lambda hass, eid: True),
            ("in_scope", lambda hass, eid, rooms: True),
            ("serialize_device", lambda hass, state: {"entity_id": state.entity_id}),
        ):
            patcher = mock.patch(f"casasmart.api.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_widget_token_lists_reads_and_commands_devices(self) -> None:
        status, body = H.read_response(
            await CasaSmartDevicesView(self.hass).get(
                H.FakeRequest(headers=self.widget)
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], [{"entity_id": "light.lamp"}])

        status, body = H.read_response(
            await CasaSmartDeviceView(self.hass).get(
                H.FakeRequest(headers=self.widget), "light.lamp"
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_id"], "light.lamp")

        status, body = H.read_response(
            await CasaSmartCommandView(self.hass).post(
                H.FakeRequest(headers=self.widget, body={"action": "turn_off"}),
                "light.lamp",
            )
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [(d, s) for d, s, _data, _blocking in self.hass.services.calls],
            [("light", "turn_off")],
        )


@unittest.skipIf(
    _DEVICE_VIEWS_ERR is not None,
    f"Home Assistant unavailable: {_DEVICE_VIEWS_ERR}",
)
class WidgetTokenCannotWritePersonalStateTests(_HubTestCase):
    """Settings, favorites and suggestion dismiss/snooze belong to the person,
    not to a device: session writes, out of a widget token's reach."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.hass.states.add("light.lamp", state="on")
        self.settings = UserSettingsEngine(self.rt.storage.table("user_settings"))
        self.rt.user_settings = self.settings
        for module in ("registry_api", "settings_api"):
            patcher = mock.patch.multiple(
                f"casasmart.{module}",
                is_served=lambda hass, eid: True,
                in_scope=lambda hass, eid, rooms: True,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _put_settings(self, headers):
        return H.read_response(
            await CasaSmartUserSettingsView(self.hass).put(
                H.FakeRequest(headers=headers, body={"display_name": "Changed"})
            )
        )

    async def _put_favorites(self, headers):
        return H.read_response(
            await CasaSmartFavoritesView(self.hass).put(
                H.FakeRequest(headers=headers, body={"entity_ids": ["light.lamp"]})
            )
        )

    async def test_widget_token_cannot_write_settings_or_favorites(self) -> None:
        for role in _ROLES:
            with self.subTest(role=role):
                device_id = H.enroll(self.rt.auth, role=role)
                member = self.rt.auth.member_id_for(device_id)
                self.settings.update(member, {"display_name": "Original"})
                widget = self._widget_headers(device_id)

                self.assertEqual((await self._put_settings(widget))[0], 403)
                self.assertEqual((await self._put_favorites(widget))[0], 403)

                self.assertEqual(self.settings.get(member)["display_name"], "Original")
                self.assertEqual(self.rt.registry.get_favorites(member), [])
        self.assertEqual(self.hass.bus.fired, [])  # no settings/favorites nudge

    async def test_every_role_session_still_writes_settings_and_favorites(self) -> None:
        for role in _ROLES:
            with self.subTest(role=role):
                device_id, headers = H.session(self.rt.auth, role=role)
                member = self.rt.auth.member_id_for(device_id)

                status, body = await self._put_settings(headers)
                self.assertEqual(status, 200)
                self.assertEqual(body["display_name"], "Changed")
                status, body = await self._put_favorites(headers)
                self.assertEqual(status, 200)
                self.assertEqual(self.rt.registry.get_favorites(member), ["light.lamp"])

    async def test_widget_token_cannot_dismiss_or_snooze_a_suggestion(self) -> None:
        # Refused before the suggestion service is touched: spec-less stand-ins
        # raise on any attribute access, so a 403 here proves nothing ran.
        self.rt.suggestions = mock.NonCallableMock(
            spec=["generated"], generated=mock.NonCallableMock(spec=[])
        )
        widget = self._widget_headers(H.enroll(self.rt.auth, role="admin"))
        for view in (
            CasaSmartSuggestionActionView,
            CasaSmartGeneratedSuggestionActionView,
        ):
            for action in ("dismiss", "snooze"):
                with self.subTest(view=view.__name__, action=action):
                    status, _ = H.read_response(
                        await view(self.hass).post(
                            H.FakeRequest(
                                headers=widget,
                                body={"action": action, "occurrence_id": "0" * 64},
                            )
                        )
                    )
                    self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
