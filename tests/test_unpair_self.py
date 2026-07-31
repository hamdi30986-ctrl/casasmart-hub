"""View-layer tests for POST /auth/unpair-self — the device hands the hub back.

The gap this closes: the app's "Remove Hub" never told the hub anything, so
the hub kept the phone enrolled as its one admin. A hub that HAS an admin
refuses to enroll a second one, only ever issues sub-admin/user codes, and
drops the bootstrap owner code — so after removing the hub in the app, NO
phone could administer that hub again without the engraved recovery card or
physically holding the reset button.

The engines are REAL (AuthEngine + PairingManager over a temp HubStorage,
wired like ``__init__.py``); only ``hass`` + the request are ``view_harness``
fakes. So "the sticker code works again" is asserted by actually redeeming it,
not by checking that a function was called.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.auth_api import CasaSmartUnpairSelfView  # noqa: E402
from casasmart.const import (  # noqa: E402
    BOOTSTRAP_CODE_HASH_CONFIG_KEY,
    EVENT_AUTH_CHANGED,
)
from casasmart.pairing import (  # noqa: E402
    BOOTSTRAP_CODE_ID,
    CodeInvalidError,
    PairingManager,
    hash_code,
)

_STICKER_CODE = "TESTCODE"


class UnpairSelfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)

        # Real pairing manager, holding the hub's PERMANENT sticker code the
        # way a shipped hub does (hash persisted in hub_config, re-installed
        # while the hub is unclaimed).
        self.rt.pairing = PairingManager(
            self.rt.storage.table("pairing_codes"), self.rt.auth.has_admin
        )
        self.rt.hub_config.set(BOOTSTRAP_CODE_HASH_CONFIG_KEY, hash_code(_STICKER_CODE))
        self.rt.user_settings = _FakeUserSettings()
        self.view = CasaSmartUnpairSelfView(self.hass)

    def _redeem_sticker(self):
        """Try the printed owner code on the LAN, as a fresh phone would."""
        return self.rt.pairing.redeem(_STICKER_CODE, "192.168.8.50")

    def _boot_while_claimed(self) -> None:
        """What the boot path does on a hub that already has an owner.

        ``install_bootstrap_hash`` re-installs the permanent code only while
        the hub is unclaimed, and DROPS it once an admin exists — so on a
        claimed hub the printed code is not armed at all.
        """
        self.rt.pairing.install_bootstrap_hash(hash_code(_STICKER_CODE))

    async def test_owner_can_hand_the_hub_back_and_re_claim_it(self) -> None:
        device_id, headers = H.session(self.rt.auth, role="admin")
        self._boot_while_claimed()
        # The printed code does nothing while the hub is claimed — the state
        # that made "Remove Hub" unrecoverable.
        with self.assertRaises(CodeInvalidError):
            self._redeem_sticker()

        status, body = H.read_response(
            await self.view.post(H.FakeRequest(headers=headers))
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["unpaired"], device_id)
        self.assertTrue(body["hub_unclaimed"])
        self.assertFalse(self.rt.auth.has_admin())
        self.assertIsNone(self.rt.auth.get_device(device_id))
        # The code printed on the hub works again — re-claim without a site
        # visit, the recovery card, or the reset button.
        grant = self._redeem_sticker()
        self.assertEqual(grant["role"], "admin")

    async def test_a_family_member_leaving_does_not_unclaim_the_hub(self) -> None:
        admin_id, _ = H.session(self.rt.auth, role="admin")
        user_id, headers = H.session(self.rt.auth, role="user")
        self._boot_while_claimed()

        status, body = H.read_response(
            await self.view.post(H.FakeRequest(headers=headers))
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["unpaired"], user_id)
        self.assertFalse(body["hub_unclaimed"])
        self.assertIsNone(self.rt.auth.get_device(user_id))
        self.assertIsNotNone(self.rt.auth.get_device(admin_id))
        self.assertTrue(self.rt.auth.has_admin())
        # Still claimed, so the owner code must NOT have been armed.
        with self.assertRaises(CodeInvalidError):
            self._redeem_sticker()

    async def test_the_token_names_the_device_a_body_cannot(self) -> None:
        # The one thing that must never be possible: using this to evict
        # somebody else. The device id comes from the token subject; a body is
        # not even read.
        victim_id, _ = H.session(self.rt.auth, role="admin")
        caller_id, headers = H.session(self.rt.auth, role="user")

        status, body = H.read_response(
            await self.view.post(
                H.FakeRequest(
                    headers=headers, body={"device_id": victim_id}
                )
            )
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["unpaired"], caller_id)
        self.assertIsNotNone(self.rt.auth.get_device(victim_id))

    async def test_unauthenticated_is_refused(self) -> None:
        status, _ = H.read_response(
            await self.view.post(H.FakeRequest())
        )
        self.assertEqual(status, 401)
        self.assertTrue(self.rt.auth.has_admin() is False)  # nothing enrolled

    async def test_repeating_the_call_is_not_an_error(self) -> None:
        # The app's teardown retries after a dropped response; a second call
        # with the (now dead) token is refused by auth, and a stale-but-valid
        # token for a gone device answers cleanly rather than 500ing.
        device_id, headers = H.session(self.rt.auth, role="user")
        first, _ = H.read_response(
            await self.view.post(H.FakeRequest(headers=headers))
        )
        self.assertEqual(first, 200)

        # Re-seed the cache entry so the token validates but the record is
        # gone — the race the idempotent branch exists for.
        self.rt.auth._device_cache[device_id] = {
            "role": "user",
            "rooms": None,
            "ver": 1,
        }
        status, body = H.read_response(
            await self.view.post(H.FakeRequest(headers=headers))
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["unpaired"], device_id)
        self.assertFalse(body["hub_unclaimed"])

    async def test_departure_nudges_the_hub(self) -> None:
        _, headers = H.session(self.rt.auth, role="user")
        await self.view.post(H.FakeRequest(headers=headers))
        self.assertIn(
            EVENT_AUTH_CHANGED, [event for event, _data in self.hass.bus.fired]
        )

    async def test_no_stored_sticker_hash_still_unpairs(self) -> None:
        # A hub predating the persisted sticker hash: the device must still
        # leave. Minting a random code nobody can read would help no one, so
        # the hub stays unclaimed with no armed code (reset button territory)
        # — and it must not raise.
        self.rt.hub_config.delete(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
        device_id, headers = H.session(self.rt.auth, role="admin")

        status, body = H.read_response(
            await self.view.post(H.FakeRequest(headers=headers))
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["hub_unclaimed"])
        self.assertIsNone(self.rt.auth.get_device(device_id))
        self.assertNotIn(BOOTSTRAP_CODE_ID, self.rt.pairing._codes)


class _FakeUserSettings:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, member_id: str) -> None:
        self.deleted.append(member_id)


if __name__ == "__main__":
    unittest.main()
