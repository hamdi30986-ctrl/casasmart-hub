"""The login throttle can't be turned against a device by someone else.

Device ids aren't secret (a sub-admin sees the owner's in the installer
views), and the throttle used to count every failed login against the device
id alone, so anyone could lock the owner out of logging in. Failures now
count against the device as seen from one source, and a made-up challenge id
counts against nobody. Real views, engine and P-256 signatures; needs a real
Home Assistant.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.auth_api import CasaSmartChallengeView, CasaSmartTokenView
    from casasmart.throttle import MAX_FAILURES
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR
OWNER_IP = "192.168.1.20"
OTHER_IP = "192.168.1.66"


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class LoginThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.key = ec.generate_private_key(ec.SECP256R1())
        pem = (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        self.owner = self.rt.auth.enroll_device("Owner phone", "admin", pem)

    async def _post(self, view_cls, body: dict, remote: str):
        request = H.FakeRequest(body=body, remote=remote)
        return H.read_response(await view_cls(self.hass).post(request))

    async def _challenge(self, remote: str):
        return await self._post(
            CasaSmartChallengeView, {"device_id": self.owner}, remote
        )

    async def _redeem(self, challenge_id: str, signature: str, remote: str) -> int:
        body = {
            "device_id": self.owner,
            "challenge_id": challenge_id,
            "signature": signature,
        }
        status, _ = await self._post(CasaSmartTokenView, body, remote)
        return status

    async def _owner_login(self) -> int:
        status, challenge = await self._challenge(OWNER_IP)
        if status != 200:
            return status
        signature = self.key.sign(
            challenge["nonce"].encode(), ec.ECDSA(hashes.SHA256())
        )
        return await self._redeem(
            challenge["challenge_id"], base64.b64encode(signature).decode(), OWNER_IP
        )

    async def test_failures_from_elsewhere_do_not_lock_the_owner_out(self) -> None:
        for _ in range(MAX_FAILURES):
            self.assertEqual(await self._redeem("made-up", "AAAA", OTHER_IP), 401)
            _, challenge = await self._challenge(OTHER_IP)
            self.assertEqual(
                await self._redeem(challenge["challenge_id"], "AAAA", OTHER_IP), 401
            )
        self.assertEqual(await self._owner_login(), 200)

    async def test_bad_signatures_still_lock_out_their_source(self) -> None:
        for _ in range(MAX_FAILURES):
            _, challenge = await self._challenge(OTHER_IP)
            self.assertEqual(
                await self._redeem(challenge["challenge_id"], "AAAA", OTHER_IP), 401
            )
        status, _ = await self._challenge(OTHER_IP)
        self.assertEqual(status, 429)

    async def test_made_up_challenge_ids_count_against_no_one(self) -> None:
        for _ in range(MAX_FAILURES * 2):
            self.assertEqual(await self._redeem("made-up", "AAAA", OWNER_IP), 401)
        self.assertEqual(await self._owner_login(), 200)


if __name__ == "__main__":
    unittest.main()
