"""View-layer tests for the enroll gate's code-class network policy.

Pins the ``remote_pairing_enabled`` code-class policy at the WIRE seam
(``CasaSmartEnrollView``), not just the manager:

* Flag OFF (default / unset / malformed) — every non-LAN source gets the
  original LAN-only 403 byte-for-byte, member and bootstrap codes alike;
  the LAN path enrolls exactly as before.
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
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

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
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

LAN_IP = "192.168.1.50"  # a phone on the hub's own network
TUNNEL_IP = "127.0.0.1"  # cloudflared traffic reaches HA from loopback
PUBLIC_IP = "203.0.113.9"  # a phone on LTE hitting the hub directly
LAN_ONLY_MSG = "Pairing is only available on the hub's own network"


def make_public_pem() -> str:
    """A phone-side P-256 public key PEM (each device needs its own)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


class RecordingDispatcher:
    """Stands in for ``runtime_data.push_dispatcher`` — records the enroll
    view's device-paired sends without a relay."""

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

    # -- Throttle-bucket isolation at the wire seam -----------------------------

    async def test_remote_lockout_does_not_block_lan_owner_claim(self) -> None:
        # ONE source string ("127.0.0.1"), classified remote first and LAN
        # after: a tunnel guessing burst locks the remote bucket (429 +
        # Retry-After); the same source then arrives on a TLS listener trusted
        # as LAN ingress (the Docker Desktop relay setup, where every phone
        # shows a synthetic address), and the owner's bootstrap claim goes
        # straight through — the remote lockout never touched the LAN bucket.
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
        from casasmart.tls import TLS_LISTENER_TRUSTED_LAN

        request = H.FakeRequest(
            body={
                "pairing_code": code,
                "public_key": make_public_pem(),
                "name": "Owner phone",
            },
            remote=TUNNEL_IP,
        )
        request.app = {TLS_LISTENER_TRUSTED_LAN: True}
        status, body = H.read_response(await self.view.post(request))
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")

    async def test_retired_extra_lan_cidrs_setting_is_ignored(self) -> None:
        # pairing_extra_lan_cidrs could only ever widen the gate to loopback
        # (private ranges already count, public ones were refused): the very
        # traffic a local tunnel produces. Since 2.3.0 it has no effect.
        self._claim_hub()
        self.hub_config.set("pairing_extra_lan_cidrs", ["127.0.0.0/8"])
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll(issued["code"], TUNNEL_IP)
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)

    # -- Admin notification fires on enroll -------------------------------------

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
        # The core case: a member code redeemed through the tunnel
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


DOCKER_GATEWAY_IP = "172.18.0.1"  # what HA sees for host-forwarded connections
CLOUDFLARE_HEADERS = {"CF-Connecting-IP": "203.0.113.9", "CF-Ray": "8c1f2e3d4a5b-AMS"}


class CloudflareProxiedTests(EnrollGateTests):
    """Since 2.3.0: a request that crossed Cloudflare is never treated as LAN, even
    when the last hop is a private address (a tunnel into the TLS listener)."""

    async def _enroll_via(self, code: str, headers: dict, remote: str):
        resp = await self.view.post(
            H.FakeRequest(
                headers=headers,
                body={
                    "pairing_code": code,
                    "public_key": make_public_pem(),
                    "name": "Phone",
                },
                remote=remote,
            )
        )
        return H.read_response(resp)

    async def test_proxied_member_code_from_private_hop_is_refused(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll_via(
            issued["code"], CLOUDFLARE_HEADERS, DOCKER_GATEWAY_IP
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)
        # Refused before redeem: the code still works from the LAN.
        status, _ = await self._enroll(issued["code"], LAN_IP)
        self.assertEqual(status, 201)

    async def test_proxied_owner_claim_from_private_hop_is_refused(self) -> None:
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll_via(
            code, {"CDN-Loop": "cloudflare"}, DOCKER_GATEWAY_IP
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)

    async def test_remote_pairing_through_the_tunnel_still_works(self) -> None:
        # The tunnel path itself is unchanged: with remote pairing enabled an
        # admin-minted member code still enrolls through Cloudflare.
        self._claim_hub()
        self.hub_config.set(REMOTE_PAIRING_ENABLED_CONFIG_KEY, True)
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll_via(
            issued["code"], CLOUDFLARE_HEADERS, DOCKER_GATEWAY_IP
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")


class LanSourceTests(unittest.TestCase):
    def _is_lan(self, remote: str, headers: dict | None = None) -> bool:
        from casasmart.auth_api import is_lan_request

        return is_lan_request(H.FakeRequest(headers=headers or {}, remote=remote))

    def test_private_hop_without_proxy_headers_is_lan(self) -> None:
        self.assertTrue(self._is_lan(DOCKER_GATEWAY_IP))
        self.assertTrue(self._is_lan(LAN_IP))

    def test_any_cloudflare_header_makes_it_remote(self) -> None:
        for headers in (
            {"CF-Connecting-IP": "203.0.113.9"},
            {"CF-Ray": "8c1f2e3d4a5b-AMS"},
            {"CDN-Loop": "cloudflare; loops=1"},
        ):
            self.assertFalse(self._is_lan(LAN_IP, headers), headers)

    def test_loopback_and_public_stay_remote(self) -> None:
        self.assertFalse(self._is_lan(TUNNEL_IP))
        # A routable address (PUBLIC_IP above is a documentation range, which
        # Python's ipaddress classifies as private).
        self.assertFalse(self._is_lan("8.8.8.8"))


# Docker Desktop hands the hub synthetic, unstable source addresses — after a
# container restart they can be arbitrary PUBLIC addresses. Through the trusted
# TLS listener (published to 127.0.0.1 only, behind the LAN-only relay) pairing
# must still work.
SYNTHETIC_PUBLIC_IP = "8.8.4.4"


class TrustedLanIngressTests(EnrollGateTests):
    """Since 2.3.0: on a TLS listener trusted as LAN ingress, the listener is the
    LAN proof; anywhere else the source address still decides."""

    def _request(self, body, remote, *, listener=None, headers=None):
        from casasmart.tls import TLS_LISTENER_TRUSTED_LAN

        request = H.FakeRequest(headers=headers or {}, body=body, remote=remote)
        if listener is not None:  # None = served by HA's own HTTP app
            request.app = {TLS_LISTENER_TRUSTED_LAN: listener}
        return request

    async def _enroll_on(self, code, remote, **kw):
        body = {"pairing_code": code, "public_key": make_public_pem(), "name": "Phone"}
        return H.read_response(await self.view.post(self._request(body, remote, **kw)))

    async def test_member_code_pairs_through_trusted_listener_from_synthetic_address(
        self,
    ) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll_on(
            issued["code"], SYNTHETIC_PUBLIC_IP, listener=True
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")

    async def test_owner_claim_through_trusted_listener(self) -> None:
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll_on(code, SYNTHETIC_PUBLIC_IP, listener=True)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")

    async def test_untrusted_listener_keeps_the_address_rule(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll_on(
            issued["code"], SYNTHETIC_PUBLIC_IP, listener=False
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)
        # ...and a real LAN address still pairs on that listener.
        status, _ = await self._enroll_on(issued["code"], LAN_IP, listener=False)
        self.assertEqual(status, 201)

    async def test_ha_port_is_never_trusted_by_listener(self) -> None:
        # HA's own HTTP app carries no marker: the tunnel enters there.
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, _ = await self._enroll_on(issued["code"], SYNTHETIC_PUBLIC_IP)
        self.assertEqual(status, 403)

    async def test_cloudflare_beats_a_trusted_listener(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        status, body = await self._enroll_on(
            issued["code"], "127.0.0.1", listener=True, headers=CLOUDFLARE_HEADERS
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["message"], LAN_ONLY_MSG)

    async def test_recovery_passes_the_lan_gate_on_trusted_listener(self) -> None:
        from casasmart.auth_api import CasaSmartRecoverView
        from casasmart.recovery import RecoveryManager

        self._claim_hub()
        self.runtime.recovery = RecoveryManager(
            self.storage.table("recovery_codes"), self.auth.has_admin
        )
        self.runtime.recovery.ensure_armed()
        view = CasaSmartRecoverView(self.hass)
        body = {"recovery_code": "WRONGWRONG", "public_key": make_public_pem()}
        # Trusted listener: past the LAN gate, refused on the (wrong) code.
        resp = await view.post(self._request(body, SYNTHETIC_PUBLIC_IP, listener=True))
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)
        # Untrusted listener, same address: refused at the LAN gate.
        resp = await view.post(self._request(body, SYNTHETIC_PUBLIC_IP, listener=False))
        status, body = H.read_response(resp)
        self.assertEqual(status, 403)
        self.assertEqual(
            body["message"], "Recovery is only available on the hub's own network"
        )


class NonAsciiCodeTests(EnrollGateTests):
    """Codes are ASCII. Other text a phone keyboard produces (Arabic-Indic
    digits, full-width or Arabic letters) used to crash the code hash with
    UnicodeEncodeError: HTTP 500, and no throttle failure counted. It is now
    the generic invalid-code answer, and counts like any wrong guess."""

    INPUTS = {
        "arabic-indic digits": "\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
        "full-width letters": "\uff21\uff22\uff23\uff24\uff25\uff26\uff27\uff28",
        "arabic letters": "\u0627\u0628\u062c\u062f\u0647\u0648\u0632\u062d",
        "mixed text": "ABCD \u0662\u0663\u0664\u0665 \uff25\uff26",
    }

    def _arm_recovery(self):
        from casasmart.recovery import RecoveryManager

        self.runtime.recovery = RecoveryManager(
            self.storage.table("recovery_codes"), self.auth.has_admin
        )
        return self.runtime.recovery.ensure_armed()

    async def _recover(self, code: str):
        from casasmart.auth_api import CasaSmartRecoverView

        resp = await CasaSmartRecoverView(self.hass).post(
            H.FakeRequest(
                body={
                    "recovery_code": code,
                    "public_key": make_public_pem(),
                    "name": "New phone",
                },
                remote=LAN_IP,
            )
        )
        return H.read_response(resp)

    async def test_enroll_answers_invalid_code_and_throttles(self) -> None:
        self.pairing.ensure_bootstrap_code()
        for label, text in self.INPUTS.items():
            with self.subTest(label):
                for _ in range(MAX_FAILURES):
                    status, body = await self._enroll(text, LAN_IP)
                    self.assertEqual(status, 401)
                    self.assertEqual(body["message"], "Invalid pairing code")
                status, _ = await self._enroll(text, LAN_IP)
                self.assertEqual(status, 429)
                self.pairing.throttle.clear(f"lan:{LAN_IP}")

    async def test_re_pair_answers_invalid_code_and_throttles(self) -> None:
        self._claim_hub()
        issued = self.pairing.generate_code("user")
        pem = make_public_pem()
        body = {"pairing_code": issued["code"], "public_key": pem, "name": "Phone"}
        resp = await self.view.post(H.FakeRequest(body=body, remote=LAN_IP))
        self.assertEqual(H.read_response(resp)[0], 201)
        for label, text in self.INPUTS.items():
            with self.subTest(label):
                body["pairing_code"] = text
                for _ in range(MAX_FAILURES):
                    resp = await self.view.post(H.FakeRequest(body=body, remote=LAN_IP))
                    status, answer = H.read_response(resp)
                    self.assertEqual(status, 401)
                    self.assertEqual(answer["message"], "Invalid pairing code")
                resp = await self.view.post(H.FakeRequest(body=body, remote=LAN_IP))
                self.assertEqual(H.read_response(resp)[0], 429)
                self.pairing.throttle.clear(f"lan:{LAN_IP}")

    async def test_recover_answers_invalid_code_and_throttles(self) -> None:
        self._claim_hub()
        self._arm_recovery()
        for label, text in self.INPUTS.items():
            with self.subTest(label):
                for _ in range(MAX_FAILURES):
                    status, body = await self._recover(text)
                    self.assertEqual(status, 401)
                    self.assertEqual(body["message"], "Invalid recovery code")
                status, _ = await self._recover(text)
                self.assertEqual(status, 429)
                self.runtime.recovery.throttle.clear(LAN_IP)

    async def test_sloppily_typed_valid_codes_still_work(self) -> None:
        code = self.pairing.ensure_bootstrap_code()
        status, body = await self._enroll(f" {code[:4].lower()}-{code[4:]} ", LAN_IP)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")
        card = self._arm_recovery()
        status, body = await self._recover(f"  {card.lower().replace('-', ' ')} ")
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")


class OwnerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The recovery card at the wire seam, wired like ``__init__.py``: the
    card's hash is installed at every boot, whether or not the hub is claimed,
    and the push-token store is real."""

    CARD = "STARS-23456"

    async def asyncSetUp(self) -> None:
        from casasmart.push import PushTokenStore
        from casasmart.recovery import RecoveryManager
        from casasmart.recovery import hash_code as recovery_hash_code

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(db_path=Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.auth = AuthEngine(self.storage.table("auth_devices"), H.FakeHubConfig())
        self.auth.warm_up()
        self.pairing = PairingManager(
            self.storage.table("pairing_codes"), self.auth.has_admin
        )
        self.recovery = RecoveryManager(
            self.storage.table("recovery_codes"), self.auth.has_admin
        )
        self.recovery.install_recovery_hash(recovery_hash_code(self.CARD))
        self.push = PushTokenStore(self.storage.table("push_tokens"))
        self.hass = H.FakeHass(
            types.SimpleNamespace(
                auth=self.auth,
                pairing=self.pairing,
                recovery=self.recovery,
                hub_config=H.FakeHubConfig(),
                push=self.push,
                push_dispatcher=None,
            )
        )
        # arm_recovery hands a newly minted code to the HA notification on
        # the event loop; record what it would have shown.
        self.announced: list[str] = []
        self.hass.loop = types.SimpleNamespace(
            call_soon_threadsafe=lambda func, *args: self.announced.append(args[-1])
        )

    async def _recover(self, card: str):
        from casasmart.auth_api import CasaSmartRecoverView

        resp = await CasaSmartRecoverView(self.hass).post(
            H.FakeRequest(
                body={
                    "recovery_code": card,
                    "public_key": make_public_pem(),
                    "name": "New phone",
                },
                remote=LAN_IP,
            )
        )
        return H.read_response(resp)

    async def test_card_tried_on_an_unclaimed_hub_stays_valid(self) -> None:
        # No admin to replace yet (never claimed, or the owner handed it back):
        # the card is refused...
        status, body = await self._recover(self.CARD)
        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "This hub has no admin to recover")
        # ...but it stays armed, so claiming the hub announces no new
        # "permanent" code (one the next restart would replace with this card).
        self.assertTrue(self.recovery.is_armed())
        resp = await CasaSmartEnrollView(self.hass).post(
            H.FakeRequest(
                body={
                    "pairing_code": self.pairing.ensure_bootstrap_code(),
                    "public_key": make_public_pem(),
                    "name": "Owner phone",
                },
                remote=LAN_IP,
            )
        )
        self.assertEqual(H.read_response(resp)[0], 201)
        self.assertEqual(self.announced, [])
        status, body = await self._recover(self.CARD)
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "admin")
