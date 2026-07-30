"""View-layer tests for the mint endpoint — Phase 3 of the pairing redesign.

Pins the pairing payload v2 at the WIRE seam (``CasaSmartPairingCodesView``):

* Every v1 mint field is still present and unchanged — the current app keeps
  working against a Phase-3 hub with zero changes.
* The response gains ``payload_version`` / ``identity_fingerprint`` /
  ``tunnel_url`` / ``qr_payload``; the fingerprint is EXACTLY the TLS
  identity the handshake serves (the value the app pins at TOFU), and the
  tunnel URL honors the manual ``tunnel_enabled`` OFF toggle (absent = off,
  same fail-closed read as the reconciler).
* The v2 deep link still parses under the CURRENT app's scanner contract
  (``casasmart://<type>?code=...`` — only ``code`` is read, extras ignored).

The engines are REAL (AuthEngine + PairingManager over a temp HubStorage,
wired like ``__init__.py``); tokens are REAL HMAC JWTs through the real
``authenticate_request`` gate. Only ``hass`` + the request are the
``view_harness`` fakes; ``homeassistant`` comes from the shared stub
package, so this suite runs locally AND in the container.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.auth_api import CasaSmartPairingCodesView  # noqa: E402
from casasmart.auth_engine import AuthEngine  # noqa: E402
from casasmart.const import CONF_TUNNEL_ENABLED, DOMAIN  # noqa: E402
from casasmart.pairing import PairingManager  # noqa: E402
from casasmart.storage import HubStorage  # noqa: E402
from casasmart.tunnel import TUNNEL_URL_CONFIG_KEY  # noqa: E402

LAN_IP = "192.168.8.50"
# A realistic pinned identity: SHA-256 hex, exactly what tls.py produces.
FINGERPRINT = "0123456789abcdef" * 4
TUNNEL_URL = "https://nx7k.example.com"
# The exact v1 mint contract (pairing.generate_code) — must never shrink.
V1_KEYS = {
    "code_id",
    "code",
    "role",
    "rooms",
    "member_id",
    "expires_at",
    "code_class",
}
V2_KEYS = {"payload_version", "identity_fingerprint", "tunnel_url", "qr_payload"}


class PairingCodesViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(db_path=Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.hub_config = H.FakeHubConfig()
        self.auth = AuthEngine(self.storage.table("auth_devices"), self.hub_config)
        self.auth.warm_up()
        self.pairing = PairingManager(
            self.storage.table("pairing_codes"), self.auth.has_admin
        )
        runtime = types.SimpleNamespace(
            auth=self.auth,
            pairing=self.pairing,
            hub_config=self.hub_config,
            recovery=None,
            # The TLS identity seam the view reads — same shape as
            # CasaSmartTlsServer(.material = TlsMaterial).
            tls=types.SimpleNamespace(
                port=8443,
                material=types.SimpleNamespace(identity_fingerprint=FINGERPRINT),
            ),
        )
        self.hass = H.FakeHass(runtime)
        self.view = CasaSmartPairingCodesView(self.hass)
        # A real admin session through the real JWT gate.
        _, self.headers = H.session(self.auth, role="admin")

    def _entry(self):
        return self.hass.config_entries.async_loaded_entries(DOMAIN)[0]

    def _tunnel_on(self) -> None:
        self._entry().options[CONF_TUNNEL_ENABLED] = True
        self.hub_config.set(TUNNEL_URL_CONFIG_KEY, TUNNEL_URL)

    async def _mint(self, body=None, headers=None):
        resp = await self.view.post(
            H.FakeRequest(
                body={"role": "user"} if body is None else body,
                headers=self.headers if headers is None else headers,
                remote=LAN_IP,
            )
        )
        return H.read_response(resp)

    # -- v1 contract: present and unchanged ------------------------------------

    async def test_v1_fields_present_and_unchanged(self) -> None:
        self._tunnel_on()
        status, body = await self._mint({"role": "user", "rooms": ["area_living"]})
        self.assertEqual(status, 201)
        self.assertTrue(V1_KEYS.issubset(body), sorted(V1_KEYS - set(body)))
        self.assertTrue(body["code_id"].startswith("pair-"))
        self.assertRegex(body["code"], r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$")
        self.assertEqual(body["role"], "user")
        self.assertEqual(body["rooms"], ["area_living"])
        self.assertIsNone(body["member_id"])
        self.assertIsInstance(body["expires_at"], int)
        self.assertGreater(body["expires_at"], time.time())
        self.assertEqual(body["code_class"], "member")
        # Exact key set: v1 + v2, nothing renamed, nothing dropped.
        self.assertEqual(set(body), V1_KEYS | V2_KEYS)

    # -- v2 fields --------------------------------------------------------------

    async def test_v2_fields_present_and_correct(self) -> None:
        self._tunnel_on()
        status, body = await self._mint()
        self.assertEqual(status, 201)
        self.assertEqual(body["payload_version"], 2)
        # The EXACT identity the handshake serves / the app pins — no
        # re-derivation, no new scheme.
        self.assertEqual(body["identity_fingerprint"], FINGERPRINT)
        self.assertEqual(body["tunnel_url"], TUNNEL_URL)
        self.assertEqual(
            body["qr_payload"],
            f"casasmart://family?code={body['code']}&v=2"
            f"&fp={FINGERPRINT}&tunnel={quote(TUNNEL_URL, safe='')}",
        )

    async def test_v1_scanner_still_reads_the_code(self) -> None:
        # The current app parses casasmart://<type>?code=... and ignores
        # unknown params (qr_scanner_screen.dart) — pin that a v2 QR keeps
        # satisfying that contract.
        self._tunnel_on()
        _, body = await self._mint()
        parts = urlsplit(body["qr_payload"])
        self.assertEqual(parts.scheme, "casasmart")
        self.assertEqual(parts.netloc, "family")
        params = parse_qs(parts.query)
        self.assertEqual(params["code"], [body["code"]])
        # And the v2 params round-trip for the Phase-4 parser.
        self.assertEqual(params["v"], ["2"])
        self.assertEqual(params["fp"], [FINGERPRINT])
        self.assertEqual(params["tunnel"], [TUNNEL_URL])

    # -- tunnel gate: the manual OFF toggle is honored --------------------------

    async def test_manual_off_omits_tunnel_url(self) -> None:
        self._entry().options[CONF_TUNNEL_ENABLED] = False
        self.hub_config.set(TUNNEL_URL_CONFIG_KEY, TUNNEL_URL)
        _, body = await self._mint()
        self.assertNotIn("tunnel_url", body)
        self.assertNotIn("tunnel=", body["qr_payload"])
        # The identity anchor is independent of the tunnel.
        self.assertEqual(body["identity_fingerprint"], FINGERPRINT)
        self.assertEqual(
            body["qr_payload"],
            f"casasmart://family?code={body['code']}&v=2&fp={FINGERPRINT}",
        )

    async def test_toggle_absent_means_off(self) -> None:
        # Same fail-closed default as the reconciler's options read.
        self.hub_config.set(TUNNEL_URL_CONFIG_KEY, TUNNEL_URL)
        _, body = await self._mint()
        self.assertNotIn("tunnel_url", body)

    async def test_unusable_tunnel_url_omitted(self) -> None:
        # normalize_tunnel_url fails closed — http:// never reaches a phone.
        self._entry().options[CONF_TUNNEL_ENABLED] = True
        self.hub_config.set(TUNNEL_URL_CONFIG_KEY, "http://insecure.example.com")
        _, body = await self._mint()
        self.assertNotIn("tunnel_url", body)

    async def test_no_tls_identity_omits_fingerprint(self) -> None:
        self._tunnel_on()
        self._entry().runtime_data.tls = None
        _, body = await self._mint()
        self.assertNotIn("identity_fingerprint", body)
        self.assertEqual(body["payload_version"], 2)
        self.assertEqual(
            body["qr_payload"],
            f"casasmart://family?code={body['code']}&v=2"
            f"&tunnel={quote(TUNNEL_URL, safe='')}",
        )

    # -- the JWT gate + the list shape are untouched ----------------------------

    async def test_mint_still_requires_admin(self) -> None:
        status, body = await self._mint(headers={})
        self.assertEqual(status, 401)
        _, user_headers = H.session(self.auth, role="user")
        status, body = await self._mint(headers=user_headers)
        self.assertEqual(status, 403)

    async def test_list_codes_unchanged(self) -> None:
        self._tunnel_on()
        await self._mint()
        resp = await self.view.get(H.FakeRequest(headers=self.headers, remote=LAN_IP))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        listed = [c for c in body["codes"] if not c["bootstrap"]]
        self.assertEqual(len(listed), 1)
        # List stays metadata-only: no plaintext code, no v2 payload fields.
        for key in ("code", "qr_payload", "payload_version", "tunnel_url"):
            self.assertNotIn(key, listed[0])


if __name__ == "__main__":
    unittest.main()
