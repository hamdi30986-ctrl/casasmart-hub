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
from casasmart.auth_engine import MAX_DEVICE_NAME_LENGTH, AuthEngine  # noqa: E402
from casasmart.const import (  # noqa: E402
    BOOTSTRAP_CODE_HASH_CONFIG_KEY,
    REMOTE_PAIRING_ENABLED_CONFIG_KEY,
)
from casasmart.pairing import PairingManager, hash_code  # noqa: E402
from casasmart.storage import HubStorage  # noqa: E402
from casasmart.throttle import MAX_FAILURES  # noqa: E402

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


class RecordingDispatcher:
    """Stands in for ``runtime_data.push_dispatcher`` — records the enroll
    view's device-paired sends (Phase 5 / D6) without a relay."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def async_send_device_paired(self, name, role, device_id) -> None:
        self.sent.append({"name": name, "role": role, "device_id": device_id})


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
        self.runtime = types.SimpleNamespace(
            auth=self.auth,
            pairing=self.pairing,
            hub_config=self.hub_config,
            recovery=None,  # arm_recovery no-ops; not under test here
            push_dispatcher=None,  # relay push leg not running (default)
        )
        self.hass = H.FakeHass(self.runtime)
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

    # -- Phase 5 (D5): throttle-bucket isolation at the wire seam ---------------

    async def test_remote_lockout_does_not_block_lan_owner_claim(self) -> None:
        # ONE source string ("127.0.0.1"), classified remote first and LAN
        # after: a tunnel guessing burst locks the remote bucket (429 +
        # Retry-After); the operator then widens pairing_extra_lan_cidrs
        # (the existing private-only knob for proxies that rewrite every
        # source — loopback space is private, so 127.0.0.0/8 is accepted),
        # the SAME source becomes LAN-classified, and the owner's bootstrap
        # claim goes straight through — the remote lockout never touched
        # the LAN bucket.
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        code = self.pairing.ensure_bootstrap_code()
        for _ in range(MAX_FAILURES):
            status, body = await self._enroll("WRONGCOD", TUNNEL_IP)
            self.assertEqual(status, 401)
        resp = await self.view.post(
            H.FakeRequest(
                body={
                    "pairing_code": "WRONGCOD",
                    "public_key": make_public_pem(),
                    "name": "Phone",
                },
                remote=TUNNEL_IP,
            )
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", resp.headers)
        self.assertIn("retry_after", body)
        self.hub_config.set("pairing_extra_lan_cidrs", ["127.0.0.0/8"])
        status, body = await self._enroll(code, TUNNEL_IP, name="Owner phone")
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")

    # -- Phase 5 (D6): admin notification fires on enroll -----------------------

    async def _drain_tasks(self) -> None:
        """Run the fire-and-forget work the view spawned (the push send)."""
        pending, self.hass.created_tasks = self.hass.created_tasks, []
        for coro in pending:
            await coro

    async def test_enroll_fires_admin_notification(self) -> None:
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        self._claim_hub()
        issued = self.pairing.generate_code("user", rooms=["area_living"])
        status, body = await self._enroll(issued["code"], LAN_IP, name="  Kid iPad  ")
        self.assertEqual(status, 201)
        await self._drain_tasks()
        self.assertEqual(
            recorder.sent,
            [
                {
                    # The engine's stored normalization (strip), not the raw body.
                    "name": "Kid iPad",
                    "role": "user",
                    "device_id": body["device_id"],
                }
            ],
        )

    async def test_remote_enroll_fires_admin_notification(self) -> None:
        # The D6 story itself: a member code redeemed through the tunnel
        # (flag on) — the owner's phone hears about it.
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        self._claim_hub()
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll(issued["code"], TUNNEL_IP, name="LTE phone")
        self.assertEqual(status, 201)
        await self._drain_tasks()
        self.assertEqual(len(recorder.sent), 1)
        self.assertEqual(recorder.sent[0]["name"], "LTE phone")
        self.assertEqual(recorder.sent[0]["device_id"], body["device_id"])

    async def test_bootstrap_claim_also_notifies(self) -> None:
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll(code, LAN_IP, name="Owner phone")
        self.assertEqual(status, 201)
        await self._drain_tasks()
        self.assertEqual(len(recorder.sent), 1)
        self.assertEqual(recorder.sent[0]["role"], "admin")

    async def test_failed_enroll_does_not_notify(self) -> None:
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        self._claim_hub()
        status, _ = await self._enroll("WRONGCOD", LAN_IP)
        self.assertEqual(status, 401)
        self.assertEqual(self.hass.created_tasks, [])
        self.assertEqual(recorder.sent, [])

    async def test_idempotent_repair_does_not_notify(self) -> None:
        # The SAME phone re-running onboarding returns early on key
        # idempotency — that is not a new device, so no second push.
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        pem = make_public_pem()
        body_dict = {
            "pairing_code": issued["code"],
            "public_key": pem,
            "name": "Phone",
        }
        resp = await self.view.post(H.FakeRequest(body=body_dict, remote=LAN_IP))
        self.assertEqual(resp.status, 201)
        resp = await self.view.post(H.FakeRequest(body=body_dict, remote=LAN_IP))
        self.assertEqual(resp.status, 201)  # idempotent re-pair
        await self._drain_tasks()
        self.assertEqual(len(recorder.sent), 1)

    async def test_notification_name_capped_like_the_stored_record(self) -> None:
        recorder = RecordingDispatcher()
        self.runtime.push_dispatcher = recorder
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, _ = await self._enroll(
            issued["code"], LAN_IP, name=" " + "X" * (MAX_DEVICE_NAME_LENGTH + 20)
        )
        self.assertEqual(status, 201)
        await self._drain_tasks()
        self.assertEqual(recorder.sent[0]["name"], "X" * MAX_DEVICE_NAME_LENGTH)

    async def test_no_dispatcher_enroll_still_works(self) -> None:
        # push_dispatcher=None (relay leg not running) — pairing is never
        # blocked on notification plumbing.
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, _ = await self._enroll(issued["code"], LAN_IP)
        self.assertEqual(status, 201)
        self.assertEqual(self.hass.created_tasks, [])


class IdempotentRePairCodeTests(EnrollGateTests):
    """A remembered keypair is NOT a licence to accept any code.

    2026-07-31, in the field: an installer's bench hub and the client's real
    hub were both on one LAN, both holding the same phone's key. The owner
    removed the bench hub in the app and typed the REAL hub's code — and the
    app came back paired to the BENCH hub. The enroll view recognised the key
    and returned the existing identity before ever looking at the code, so the
    bench hub said yes to a code minted somewhere else; the app's enroll chain
    takes the first hub that answers. Every layer reported success, the home
    was empty because that hub had no devices, and nothing anywhere logged an
    error.
    """

    async def _repair(self, code: str, pem: str, remote: str = LAN_IP):
        resp = await self.view.post(
            H.FakeRequest(
                body={"pairing_code": code, "public_key": pem, "name": "Phone"},
                remote=remote,
            )
        )
        return H.read_response(resp)

    async def _enrolled_phone(self) -> tuple[str, str]:
        """Enroll a phone the normal way; returns (its pem, the code it used)."""
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        pem = make_public_pem()
        status, _ = await self._repair(issued["code"], pem)
        self.assertEqual(status, 201)
        return pem, issued["code"]

    async def test_a_code_from_ANOTHER_hub_is_refused(self) -> None:
        pem, _ = await self._enrolled_phone()
        # A perfectly valid code — minted by a different hub, which this hub
        # has never seen. Recognising the phone must not be enough.
        status, body = await self._repair("OTHERHUB", pem)
        self.assertEqual(status, 401)
        self.assertEqual(body["message"], "Invalid pairing code")

    async def test_re_submitting_the_consumed_code_still_works(self) -> None:
        # The UI-glitch / double-tap / retry-after-timeout case the idempotent
        # path exists for: the code is gone from the table, but it is the one
        # that enrolled THIS device, so it still authorises.
        pem, code = await self._enrolled_phone()
        status, body = await self._repair(code, pem)
        self.assertEqual(status, 201)
        self.assertNotIn(
            "enrolled_code_hash", body, "internal hash must never be returned"
        )

    async def test_a_fresh_code_from_THIS_hub_works(self) -> None:
        pem, _ = await self._enrolled_phone()
        issued = self.pairing.generate_code("user")
        status, _ = await self._repair(issued["code"], pem)
        self.assertEqual(status, 201)
        # Not consumed — the phone was already enrolled, so the code stays
        # available for the member it was actually minted for.
        self.assertIn(issued["code_id"], self.pairing._codes)

    async def test_the_owner_can_still_re_onboard_with_the_sticker_code(
        self,
    ) -> None:
        # The case the leniency was written for: on a CLAIMED hub the bootstrap
        # code is dropped from the live table, so the owner's printed code is
        # only recognisable through the persisted hash.
        sticker = "STICKER1"
        self.hub_config.set(BOOTSTRAP_CODE_HASH_CONFIG_KEY, hash_code(sticker))
        self.pairing.install_bootstrap_hash(hash_code(sticker))
        pem = make_public_pem()
        self.auth.enroll_device("Owner", "admin", pem)

        status, _ = await self._repair(sticker, pem)
        self.assertEqual(status, 201)

    async def test_guessing_through_this_path_is_throttled(self) -> None:
        pem, _ = await self._enrolled_phone()
        for _ in range(6):
            status, _ = await self._repair("NOPENOPE", pem)
        # The wall goes up exactly as it does on the redeem path — a
        # remembered key must not become a free code-guessing oracle.
        self.assertEqual(status, 429)


if __name__ == "__main__":
    unittest.main()
