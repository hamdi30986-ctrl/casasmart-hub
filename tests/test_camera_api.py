"""View-layer tests for camera_api: stream tickets and the HLS proxy.

A ticket must stop working once the device that minted it loses access
(unpaired, wiped by a reset, or edited), when the camera leaves its rooms,
and while the hub is unloaded. Needs a real Home Assistant.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart import camera_api

    _ERR = H.IMPORT_ERROR
except Exception as err:
    _ERR = err

CAM = "camera.front_door"
PLAYLIST = "master_playlist.m3u8"


class _UpstreamResponse:
    status = 200
    content_type = "application/vnd.apple.mpegurl"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return b"#EXTM3U\n"


class _Session:
    """Home Assistant's own HLS endpoint, as the proxy fetches it."""

    def get(self, url, **kwargs):
        return _UpstreamResponse()


async def _request_stream(hass, entity_id, fmt="hls"):
    return "/api/hls/stream-token/master_playlist.m3u8"


@unittest.skipIf(_ERR, f"CasaSmart unimportable: {_ERR}")
class CameraStreamTicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hass, self.rt = H.make_hub(tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.http = types.SimpleNamespace(ssl_certificate=None, server_port=8123)
        self.hass.states.add(CAM, state="streaming")
        self.scope = {"room-a": {CAM}}
        for name, value in (
            ("async_request_stream", _request_stream),
            ("async_get_clientsession", lambda hass: _Session()),
            ("is_served", H.is_served_for([CAM])),
        ):
            patcher = mock.patch.object(camera_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The views decide through filtering.visible_to, which reads
        # filtering.in_scope for the room part.
        patcher = mock.patch("casasmart.filtering.in_scope", H.in_scope_for(self.scope))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _mint(self, *, role="user", rooms=None) -> tuple[str, str]:
        """Mint a ticket as a new device; return (device_id, ticket)."""
        device_id, headers = H.session(self.rt.auth, role=role, rooms=rooms)
        view = camera_api.CasaSmartCameraStreamView(self.hass)
        status, body = H.read_response(
            await view.get(H.FakeRequest(headers=headers), CAM)
        )
        self.assertEqual(status, 200, body)
        return device_id, body["url"].split("/")[-2]

    async def _fetch(self, ticket: str) -> int:
        request = H.FakeRequest()
        request.query_string = ""
        view = camera_api.CasaSmartCameraHlsProxyView(self.hass)
        response = await view.get(request, CAM, ticket, PLAYLIST)
        return response.status

    async def test_ticket_streams_for_the_device_that_minted_it(self) -> None:
        _, ticket = await self._mint()
        self.assertEqual(await self._fetch(ticket), 200)

    async def test_unpairing_the_device_revokes_its_ticket(self) -> None:
        device_id, ticket = await self._mint()
        self.rt.auth.delete_device(device_id)
        self.assertEqual(await self._fetch(ticket), 401)

    async def test_wiping_every_device_revokes_the_ticket(self) -> None:
        _, ticket = await self._mint(role="admin")
        self.rt.auth.wipe_all_devices()
        self.assertEqual(await self._fetch(ticket), 401)

    async def test_editing_the_device_revokes_its_ticket(self) -> None:
        device_id, ticket = await self._mint(rooms=["room-a"])
        self.rt.auth.update_device(device_id, rooms=["room-a", "room-b"])
        self.assertEqual(await self._fetch(ticket), 401)

    async def test_camera_leaving_the_device_rooms_stops_the_stream(self) -> None:
        _, ticket = await self._mint(rooms=["room-a"])
        self.scope["room-a"] = set()
        self.assertEqual(await self._fetch(ticket), 404)

    async def test_unloaded_hub_answers_503(self) -> None:
        _, ticket = await self._mint()
        self.hass.config_entries.async_loaded_entries = lambda domain: []
        self.assertEqual(await self._fetch(ticket), 503)

    async def test_unload_drops_every_ticket(self) -> None:
        _, ticket = await self._mint()
        entry = self.hass.config_entries.async_loaded_entries("casasmart")[0]
        for callback in entry.on_unload:
            callback()
        self.assertEqual(await self._fetch(ticket), 401)


class _Image:
    content = b"\xff\xd8jpeg"
    content_type = "image/jpeg"


async def _get_image(hass, entity_id, timeout=None):
    return _Image()


@unittest.skipIf(_ERR, f"CasaSmart unimportable: {_ERR}")
class CameraAccessTests(unittest.IsolatedAsyncioTestCase):
    """The member's camera access on every camera path: the snapshot, the
    stream mint and the HLS proxy. A limited member must not reach a camera
    by any of them, and the admin always can."""

    IN_ROOM = "camera.kitchen"
    ELSEWHERE = "camera.garage"

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hass, self.rt = H.make_hub(tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.http = types.SimpleNamespace(ssl_certificate=None, server_port=8123)
        for cam in (self.IN_ROOM, self.ELSEWHERE):
            self.hass.states.add(cam, state="streaming")
        self.scope = {"kitchen": {self.IN_ROOM}, "garage": {self.ELSEWHERE}}
        for name, value in (
            ("async_request_stream", _request_stream),
            ("async_get_image", _get_image),
            ("async_get_clientsession", lambda hass: _Session()),
            ("is_served", H.is_served_for([self.IN_ROOM, self.ELSEWHERE])),
        ):
            patcher = mock.patch.object(camera_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("casasmart.filtering.in_scope", H.in_scope_for(self.scope))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _member(self, **kwargs) -> tuple[str, dict]:
        """A paired user phone scoped to the kitchen, with the given access."""
        device_id = H.enroll(self.rt.auth, role="user", rooms=["kitchen"], **kwargs)
        token = H.token_for(self.rt.auth, device_id, role="user", rooms=["kitchen"])
        return device_id, H.bearer(token)

    async def _snapshot(self, headers, cam) -> int:
        view = camera_api.CasaSmartCameraSnapshotView(self.hass)
        return (await view.get(H.FakeRequest(headers=headers), cam)).status

    async def _stream(self, headers, cam) -> tuple[int, str | None]:
        view = camera_api.CasaSmartCameraStreamView(self.hass)
        status, body = H.read_response(
            await view.get(H.FakeRequest(headers=headers), cam)
        )
        ticket = body["url"].split("/")[-2] if status == 200 else None
        return status, ticket

    async def _hls(self, cam, ticket) -> int:
        request = H.FakeRequest()
        request.query_string = ""
        view = camera_api.CasaSmartCameraHlsProxyView(self.hass)
        return (await view.get(request, cam, ticket, PLAYLIST)).status

    async def _every_path(self, headers, cam) -> tuple[int, int, int | None]:
        """(snapshot, stream mint, HLS fetch) statuses; HLS None when no ticket."""
        snapshot = await self._snapshot(headers, cam)
        stream, ticket = await self._stream(headers, cam)
        hls = await self._hls(cam, ticket) if ticket else None
        return snapshot, stream, hls

    async def test_none_reaches_no_camera_on_any_path(self) -> None:
        _, headers = self._member(camera_access="none")
        self.assertEqual(
            await self._every_path(headers, self.IN_ROOM), (404, 404, None)
        )
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (404, 404, None)
        )

    async def test_rooms_reaches_only_the_cameras_in_its_rooms(self) -> None:
        _, headers = self._member(camera_access="rooms")
        self.assertEqual(await self._every_path(headers, self.IN_ROOM), (200, 200, 200))
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (404, 404, None)
        )

    async def test_all_reaches_a_camera_outside_its_rooms(self) -> None:
        _, headers = self._member(camera_access="all")
        self.assertEqual(await self._every_path(headers, self.IN_ROOM), (200, 200, 200))
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (200, 200, 200)
        )

    async def test_a_record_from_before_the_field_follows_its_rooms(self) -> None:
        # The pre-upgrade behaviour: an existing scoped member keeps the
        # cameras in their rooms and nothing else; no silent lock-out.
        _, headers = self._member()
        self.assertEqual(await self._every_path(headers, self.IN_ROOM), (200, 200, 200))
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (404, 404, None)
        )

    async def test_an_unscoped_record_from_before_the_field_sees_all(self) -> None:
        device_id = H.enroll(self.rt.auth, role="user")
        headers = H.bearer(H.token_for(self.rt.auth, device_id, role="user"))
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (200, 200, 200)
        )

    async def test_the_admin_always_has_every_camera(self) -> None:
        # Even a stray "none" in storage can't take the owner's cameras away.
        device_id = H.enroll(self.rt.auth, role="admin", camera_access="none")
        self.rt.auth.warm_up()
        headers = H.bearer(H.token_for(self.rt.auth, device_id, role="admin"))
        self.assertEqual(
            await self._every_path(headers, self.ELSEWHERE), (200, 200, 200)
        )

    async def test_an_old_token_without_the_claim_uses_the_record(self) -> None:
        # Minted by a pre-upgrade hub: no camera_access claim. The record
        # decides, as it does for the role and rooms.
        device_id, _ = self._member(camera_access="none")
        headers = H.bearer(
            H.token_for(self.rt.auth, device_id, role="user", rooms=["kitchen"])
        )
        self.assertEqual(await self._snapshot(headers, self.IN_ROOM), 404)
        self.rt.auth.update_device(device_id, camera_access="all")
        fresh = H.bearer(
            H.auth_tokens.issue_token(
                self.rt.auth._signing_secret(),
                device_id,
                "user",
                ["kitchen"],
                3600,
                ver=2,
            )
        )
        self.assertEqual(await self._snapshot(fresh, self.ELSEWHERE), 200)

    async def test_taking_cameras_away_revokes_a_live_ticket(self) -> None:
        device_id, headers = self._member(camera_access="all")
        _, ticket = await self._stream(headers, self.ELSEWHERE)
        self.assertEqual(await self._hls(self.ELSEWHERE, ticket), 200)
        self.rt.auth.update_device(device_id, camera_access="none")
        self.assertEqual(await self._hls(self.ELSEWHERE, ticket), 401)

    async def test_a_ticket_minted_with_all_keeps_its_grant_until_an_edit(self) -> None:
        # The ticket records the access at mint time; only an edit (a ver
        # bump) or the camera leaving its rooms changes what it serves.
        _, headers = self._member(camera_access="all")
        _, ticket = await self._stream(headers, self.ELSEWHERE)
        self.scope["garage"] = set()
        self.assertEqual(await self._hls(self.ELSEWHERE, ticket), 200)
        _, headers = self._member(camera_access="rooms")
        _, ticket = await self._stream(headers, self.IN_ROOM)
        self.scope["kitchen"] = set()
        self.assertEqual(await self._hls(self.IN_ROOM, ticket), 404)

    async def test_a_non_camera_entity_is_never_served_here(self) -> None:
        self.hass.states.add("light.kitchen", state="on")
        _, headers = self._member(camera_access="all")
        self.assertEqual(await self._snapshot(headers, "light.kitchen"), 404)


if __name__ == "__main__":
    unittest.main()
