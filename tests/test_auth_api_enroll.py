"""View-layer tests for the enroll gate — Phase 1 of the pairing redesign.

Pins the ``remote_pairing_enabled`` code-class policy at the WIRE seam
(``CasaSmartEnrollView``), not just the manager:

* Flag OFF (default / unset / malformed) — every non-LAN source gets the
  2026-06-10 LAN-only 403 byte-for-byte, member and bootstrap codes alike;
  the LAN path enrolls exactly as before. Zero behavior change at merge.
* Flag ON — an admin-minted MEMBER code enrolls from a tunnel source
  (cloudflared presents as loopback) and from a public source; the
  BOOTSTRAP owner claim still 403s off-LAN and is NOT consumed, so the
  legitimate on-LAN claim afterwards still works.

The engines are REAL (AuthEngine + PairingManager over a temp HubStorage,
wired exactly like ``__init__.py``); only ``hass`` + the request are the
``view_harness`` fakes. ``homeassistant`` comes from the shared stub
package, so this suite runs locally AND in the container.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import view_harness as H  # noqa: E402
from casasmart.auth_api import CasaSmartEnrollView  # noqa: E402
from casasmart.auth_engine import AuthEngine  # noqa: E402
from casasmart.const import REMOTE_PAIRING_ENABLED_CONFIG_KEY  # noqa: E402
from casasmart.pairing import PairingManager  # noqa: E402
from casasmart.storage import HubStorage  # noqa: E402

LAN_IP = "192.168.8.50"  # a phone on the hub's own network
TUNNEL_IP = "127.0.0.1"  # cloudflared traffic reaches HA from loopback
PUBLIC_IP = "203.0.113.9"  # a phone on LTE hitting the hub directly
LAN_ONLY_MSG = "Pairing is only available on the hub's own network"


def make_public_pem() -> str:
    """A phone-side P-256 public key PEM (each device needs its own)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


class EnrollGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(db_path=Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.hub_config = H.FakeHubConfig()
        # Production wiring (__init__.py): engine warmed up, pairing keyed to
        # the engine's live claim state.
        self.auth = AuthEngine(self.storage.table("auth_devices"), self.hub_config)
        self.auth.warm_up()
        self.pairing = PairingManager(
            self.storage.table("pairing_codes"), self.auth.has_admin
        )
        runtime = types.SimpleNamespace(
            auth=self.auth,
            pairing=self.pairing,
            hub_config=self.hub_config,
            recovery=None,  # arm_recovery no-ops; not under test here
        )
        self.hass = H.FakeHass(runtime)
        self.view = CasaSmartEnrollView(self.hass)

    def _claim_hub(self) -> None:
        """Enroll an admin directly on the engine so member codes are mintable
        on a realistically CLAIMED hub."""
        self.auth.enroll_device("Owner", "admin", make_public_pem())

    async def _enroll(self, code: str, remote: str, name: str = "Phone"):
        resp = await self.view.post(
            H.FakeRequest(
                body={
                    "pairing_code": code,
                    "public_key": make_public_pem(),
                    "name": name,
                },
                remote=remote,
            )
        )
        return H.read_response(resp)

    # -- flag OFF (default): today's behavior, byte-for-byte -------------------

    async def test_flag_off_member_code_via_tunnel_403(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll(issued["code"], TUNNEL_IP)
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)
        # Gate fires BEFORE redeem: the code is untouched and still works
        # from the LAN.
        status, body = await self._enroll(issued["code"], LAN_IP)
        self.assertEqual(status, 201)

    async def test_flag_off_bootstrap_via_tunnel_403(self) -> None:
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll(code, TUNNEL_IP)
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)

    async def test_flag_off_lan_member_code_enrolls(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user", rooms=["area_living"])
        status, body = await self._enroll(issued["code"], LAN_IP)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")
        self.assertEqual(body["rooms"], ["area_living"])
        self.assertTrue(body["device_id"])

    async def test_malformed_flag_stays_off(self) -> None:
        # Strictly ``is True`` — truthy junk must not open remote pairing.
        self._claim_hub()
        for junk in ("yes", 1, "true", [True]):
            self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, junk)
            issued = self.pairing.generate_code("user")
            status, body = await self._enroll(issued["code"], TUNNEL_IP)
            self.assertEqual(status, 403, f"flag={junk!r} must stay closed")
            self.assertEqual(body["message"], LAN_ONLY_MSG)

    # -- flag ON: member codes from anywhere, bootstrap stays LAN-only ---------

    async def test_flag_on_member_code_via_tunnel_enrolls(self) -> None:
        self._claim_hub()
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        issued = self.pairing.generate_code("user", rooms=["area_living"])
        status, body = await self._enroll(issued["code"], TUNNEL_IP)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")
        self.assertEqual(body["rooms"], ["area_living"])
        # Single-use survives the remote path: a second phone on the same
        # code gets the generic invalid.
        status, body = await self._enroll(issued["code"], TUNNEL_IP)
        self.assertEqual(status, 401)
        self.assertEqual(body["message"], "Invalid pairing code")

    async def test_flag_on_member_code_via_public_source_enrolls(self) -> None:
        self._claim_hub()
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        issued = self.pairing.generate_code("sub-admin")
        status, body = await self._enroll(issued["code"], PUBLIC_IP)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "sub-admin")

    async def test_flag_on_bootstrap_via_tunnel_403_not_consumed(self) -> None:
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll(code, TUNNEL_IP)
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)
        # NOT consumed — the owner's real on-LAN first claim still works.
        status, body = await self._enroll(code, LAN_IP, name="Owner phone")
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")

    async def test_flag_on_lan_path_unchanged(self) -> None:
        self._claim_hub()
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll(issued["code"], LAN_IP)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")


if __name__ == "__main__":
    unittest.main()
