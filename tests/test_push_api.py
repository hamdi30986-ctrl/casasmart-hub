"""View-layer tests for push_api: the push-token and HQ notification views.

The engines are REAL (AuthEngine, PushTokenStore and the HQ verifier's table
over a temp HubStorage); ``hass``, the request and the push dispatcher are
fakes.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.hq_notifications import (  # noqa: E402
    HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY,
    canonical_request,
)
from casasmart.push import PushTokenStore  # noqa: E402
from casasmart.push_api import (  # noqa: E402
    CasaSmartHqNotificationView,
    CasaSmartPushTokenView,
)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)


class PushTokenBodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.push = PushTokenStore(self.rt.storage.table("push_tokens"))
        self.view = CasaSmartPushTokenView(self.hass)
        self.device_id, self.headers = H.session(self.rt.auth, role="user")

    async def test_a_json_body_that_is_not_an_object_is_a_400(self) -> None:
        # Valid JSON, wrong shape: a client bug must get the same 400 as
        # unparseable JSON, not an AttributeError (HTTP 500).
        for body in ([], ["fcm_token", "android"], "fcm-token", 42, False):
            with self.subTest(body=body):
                status, payload = H.read_response(
                    await self.view.post(H.FakeRequest(headers=self.headers, body=body))
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"message": "Invalid JSON body"})
        self.assertEqual(self.rt.push.get_all_tokens(), {})

    async def test_a_valid_body_still_registers(self) -> None:
        status, payload = H.read_response(
            await self.view.post(
                H.FakeRequest(
                    headers=self.headers,
                    body={"fcm_token": "fcm-1", "platform": "ios"},
                )
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"device_id": self.device_id, "registered": True})
        self.assertEqual(self.rt.push.get_token(self.device_id)["fcm_token"], "fcm-1")


# -- HQ notification view -------------------------------------------------------


class _YieldingHass(H.FakeHass):
    """Executor jobs yield to the loop before and after, as real ones do, so
    concurrent requests interleave the same way on every run."""

    async def async_add_executor_job(self, func, *args):
        await asyncio.sleep(0)
        result = func(*args)
        await asyncio.sleep(0)
        return result


class _FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def async_send(self, data, priority):
        await asyncio.sleep(0)  # the relay round trip
        self.sent.append(data)
        return {"delivery": "relay_accepted"}


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, limit: int) -> bytes:
        return self._raw[:limit]


class _HqRequest:
    """Just what the HQ view reads from an aiohttp request."""

    content_type = "application/json"

    def __init__(self, raw: bytes, headers: dict[str, str]) -> None:
        self.headers = headers
        self.remote = "203.0.113.7"
        self.content_length = len(raw)
        self.content = _Content(raw)


class HqNotificationViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass = _YieldingHass(self.rt)
        self.key = Ed25519PrivateKey.generate()
        self.rt.hub_config.set(
            HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY,
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii"),
        )
        self.rt.push_dispatcher = _FakeDispatcher()

    def _request(self, event_id: str, nonce: str) -> _HqRequest:
        raw = json.dumps(
            {"event_id": event_id, "source_type": "reminder", "target": "today"}
        ).encode()
        now = int(time.time())
        signature = self.key.sign(canonical_request(now, nonce, raw))
        return _HqRequest(
            raw,
            {
                "X-CasaSmart-HQ-Timestamp": str(now),
                "X-CasaSmart-HQ-Nonce": nonce,
                "X-CasaSmart-HQ-Signature": base64.b64encode(signature).decode(),
            },
        )

    async def test_one_push_per_event_across_both_listeners(self) -> None:
        # build_views makes one view for HA's own HTTP app and one for the TLS
        # listener. An HQ retry of the same event (fresh nonce) arriving on the
        # other listener while the first is mid-delivery must be answered as a
        # duplicate, not pushed a second time.
        plain = CasaSmartHqNotificationView(self.hass)
        tls = CasaSmartHqNotificationView(self.hass)
        first, retry = await asyncio.gather(
            plain.post(self._request("hq-reminder:00000001", "a" * 24)),
            tls.post(self._request("hq-reminder:00000001", "b" * 24)),
        )
        self.assertEqual(len(self.rt.push_dispatcher.sent), 1)
        self.assertEqual(
            H.read_response(first),
            (202, {"accepted": True, "duplicate": False, "delivery": "relay_accepted"}),
        )
        self.assertEqual(
            H.read_response(retry),
            (200, {"accepted": True, "duplicate": True, "delivery": "relay_accepted"}),
        )

    async def test_different_events_are_each_delivered(self) -> None:
        plain = CasaSmartHqNotificationView(self.hass)
        tls = CasaSmartHqNotificationView(self.hass)
        responses = await asyncio.gather(
            plain.post(self._request("hq-reminder:00000001", "a" * 24)),
            tls.post(self._request("hq-reminder:00000002", "b" * 24)),
        )
        self.assertEqual([H.read_response(r)[0] for r in responses], [202, 202])
        self.assertEqual(len(self.rt.push_dispatcher.sent), 2)


if __name__ == "__main__":
    unittest.main()
