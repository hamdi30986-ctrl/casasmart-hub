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
            ("in_scope", H.in_scope_for(self.scope)),
        ):
            patcher = mock.patch.object(camera_api, name, value)
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


if __name__ == "__main__":
    unittest.main()
