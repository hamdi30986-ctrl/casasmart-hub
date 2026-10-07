"""A recovery card shown to the installer is the one that survives a restart.

The card's hash lives in hub_config and is reinstalled at every boot. If the
installed card was dropped (arming ran while the hub had no admin), the next
claim minted and announced a new card without saving it, so a restart
brought the old card back and the engraved one stopped working. Uses the
real setup wiring (_open_storage) and enroll view; needs a real Home
Assistant.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.auth_api import CasaSmartEnrollView, arm_recovery
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    integration = H.import_integration()
    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR
LAN_IP = "192.168.1.20"


def _public_pem() -> str:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class RecoveryCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.rt, self.bootstrap, _ = integration._open_storage(self.data_dir)
        self.addCleanup(self.rt.storage.close)
        self.hass = H.FakeHass(self.rt)
        self.announced: list[str] = []
        self.hass.loop = types.SimpleNamespace(
            call_soon_threadsafe=lambda func, *args: self.announced.append(args[-1])
        )

    def _restart(self):
        self.rt.storage.close()
        restarted, _, _ = integration._open_storage(self.data_dir)
        self.addCleanup(restarted.storage.close)
        return restarted

    async def test_a_card_announced_on_a_later_claim_survives_a_restart(self) -> None:
        # Arming on a hub nobody owns yet drops the installed card.
        arm_recovery(self.hass)
        self.assertFalse(self.rt.recovery.is_armed())

        request = H.FakeRequest(
            body={
                "pairing_code": self.bootstrap,
                "public_key": _public_pem(),
                "name": "Owner phone",
            },
            remote=LAN_IP,
        )
        status, _ = H.read_response(await CasaSmartEnrollView(self.hass).post(request))
        self.assertEqual(status, 201)
        self.assertEqual(len(self.announced), 1)

        self._restart().recovery.redeem(self.announced[0], LAN_IP)


if __name__ == "__main__":
    unittest.main()
