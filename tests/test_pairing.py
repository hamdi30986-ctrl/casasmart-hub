"""Unit tests for B2: pairing codes + the shared failure throttle.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

import throttle as throttle_mod  # noqa: E402
from pairing import (  # noqa: E402
    BOOTSTRAP_CODE_ID,
    CodeInvalidError,
    HubAlreadyClaimedError,
    PairingError,
    PairingManager,
)
from storage import HubStorage  # noqa: E402
from throttle import FailureThrottle, ThrottledError  # noqa: E402


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.throttle = FailureThrottle("test")

    def _trip(self, key):
        for _ in range(throttle_mod.MAX_FAILURES):
            self.throttle.record_failure(key)

    def test_locks_after_max_failures(self):
        self.throttle.check("ip-1")  # clean key passes
        self._trip("ip-1")
        with self.assertRaises(ThrottledError) as ctx:
            self.throttle.check("ip-1")
        # First wall = first escalation step (1 min).
        self.assertLessEqual(ctx.exception.retry_after, throttle_mod.LOCKOUT_STEPS[0])
        self.throttle.check("ip-2")  # other keys unaffected

    def test_escalating_lockouts(self):
        walls = []
        with mock.patch.object(throttle_mod.time, "monotonic") as clock:
            now = 1000.0
            clock.side_effect = lambda: now
            for _ in range(len(throttle_mod.LOCKOUT_STEPS) + 1):
                self._trip("ip-1")
                with self.assertRaises(ThrottledError) as ctx:
                    self.throttle.check("ip-1")
                walls.append(ctx.exception.retry_after)
                now += ctx.exception.retry_after + 1  # wait the wall out
        self.assertEqual(walls[: len(throttle_mod.LOCKOUT_STEPS)],
                         list(throttle_mod.LOCKOUT_STEPS))
        # Repeat offenders stay at the last step, no overflow.
        self.assertEqual(walls[-1], throttle_mod.LOCKOUT_STEPS[-1])

    def test_success_resets_escalation(self):
        self._trip("ip-1")
        self.throttle.clear("ip-1")
        self.throttle.check("ip-1")  # wall gone
        self._trip("ip-1")
        with self.assertRaises(ThrottledError) as ctx:
            self.throttle.check("ip-1")
        # Back to the FIRST step — escalation level reset too.
        self.assertLessEqual(ctx.exception.retry_after, throttle_mod.LOCKOUT_STEPS[0])

    def test_table_stays_bounded(self):
        for i in range(throttle_mod.MAX_ENTRIES + 50):
            self.throttle.record_failure(f"spam-{i}")
        self.assertLessEqual(len(self.throttle._entries), throttle_mod.MAX_ENTRIES)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(db_path=Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self._admin = False
        self.manager = PairingManager(
            self.storage.table("pairing_codes"), lambda: self._admin
        )

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_generate_and_redeem(self):
        issued = self.manager.generate_code("user", rooms=["area_living"])
        self.assertRegex(issued["code"], r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$")
        grant = self.manager.redeem(issued["code"], "ip-1")
        # redeem echoes the consumed code id (used as enrolled_via) alongside
        # the baked-in role + room scope.
        self.assertEqual(
            grant,
            {
                "role": "user",
                "rooms": ["area_living"],
                "code_id": issued["code_id"],
                "member_id": None,  # a new-member code mints one at enroll
            },
        )

    def test_member_id_rides_the_code_for_add_device(self):
        # An "add device to member" code carries the existing member_id through
        # generate -> the redeemed grant, so the new device joins that person.
        issued = self.manager.generate_code(
            "user", rooms=["area_living"], member_id="mem-abc"
        )
        self.assertEqual(issued["member_id"], "mem-abc")
        grant = self.manager.redeem(issued["code"], "ip-1")
        self.assertEqual(grant["member_id"], "mem-abc")

    def test_generate_rejects_blank_member_id(self):
        with self.assertRaises(PairingError):
            self.manager.generate_code("user", member_id="")

    def test_single_use(self):
        issued = self.manager.generate_code("user")
        self.manager.redeem(issued["code"], "ip-1")
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem(issued["code"], "ip-1")

    def test_generate_validation(self):
        with self.assertRaises(PairingError):
            self.manager.generate_code("admin")  # only bootstrap mints admin
        with self.assertRaises(PairingError):
            self.manager.generate_code("user", expires_in="forever")
        with self.assertRaises(PairingError):
            self.manager.generate_code("sub-admin", rooms=["area_x"])  # users only
        with self.assertRaises(PairingError):
            self.manager.generate_code("user", rooms=[1])

    def test_expired_code_rejected(self):
        issued = self.manager.generate_code("user", expires_in="1d")
        real_time = throttle_mod.time.time
        with mock.patch("pairing.time.time", lambda: real_time() + 25 * 3600):
            with self.assertRaises(CodeInvalidError):
                self.manager.redeem(issued["code"], "ip-1")
            self.assertEqual(self.manager.list_codes(), [])

    def test_list_never_leaks_code(self):
        self.manager.generate_code("user")
        codes = self.manager.list_codes()
        self.assertEqual(len(codes), 1)
        self.assertNotIn("code", codes[0])
        self.assertNotIn("code_hash", codes[0])

    def test_revoke(self):
        issued = self.manager.generate_code("user")
        self.assertTrue(self.manager.revoke_code(issued["code_id"]))
        self.assertFalse(self.manager.revoke_code(issued["code_id"]))
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem(issued["code"], "ip-1")

    def test_clear_all_codes_includes_bootstrap(self):
        self.manager.ensure_bootstrap_code()  # unclaimed hub -> bootstrap minted
        self.manager.generate_code("user")
        self.manager.generate_code("sub-admin")
        # The bootstrap admin code is wiped too (full reset).
        self.assertEqual(self.manager.clear_all_codes(), 3)
        self.assertEqual(self.manager.list_codes(), [])

    def test_clear_all_codes_empty_is_noop(self):
        self.assertEqual(self.manager.clear_all_codes(), 0)

    def test_guessing_gets_throttled(self):
        self.manager.generate_code("user")
        for _ in range(throttle_mod.MAX_FAILURES):
            with self.assertRaises(CodeInvalidError):
                self.manager.redeem("000000", "ip-evil")
        with self.assertRaises(ThrottledError):
            self.manager.redeem("000000", "ip-evil")
        # The wall is per-source: a different IP still gets its tries.
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem("111111", "ip-clean")

    def test_bootstrap_lifecycle(self):
        code = self.manager.ensure_bootstrap_code()
        self.assertRegex(code, r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$")
        # Idempotent while outstanding.
        self.assertIsNone(self.manager.ensure_bootstrap_code())
        grant = self.manager.redeem(code, "ip-1")
        self.assertEqual(grant["role"], "admin")
        # Once an admin exists, no new bootstrap code is minted.
        self._admin = True
        self.assertIsNone(self.manager.ensure_bootstrap_code())

    def test_bootstrap_already_paired_on_claimed_hub(self):
        # A DIFFERENT phone presenting the CORRECT owner code on a claimed hub
        # gets the distinct "already paired" signal, NOT the generic "invalid"
        # (the SAME phone is short-circuited earlier by enroll's key idempotency).
        code = self.manager.ensure_bootstrap_code()
        self._admin = True  # admin paired through some other path
        with self.assertRaises(HubAlreadyClaimedError):
            self.manager.redeem(code, "ip-1")
        # It's a real PairingError but distinct from CodeInvalidError.
        self.assertTrue(issubclass(HubAlreadyClaimedError, PairingError))
        self.assertFalse(issubclass(HubAlreadyClaimedError, CodeInvalidError))
        # ensure_bootstrap_code on a claimed hub clears the stale code.
        self.manager.ensure_bootstrap_code()
        self.assertNotIn(
            BOOTSTRAP_CODE_ID,
            [c["code_id"] for c in self.manager.list_codes()],
        )

    def test_codes_persist_across_restart(self):
        issued = self.manager.generate_code("user")
        manager2 = PairingManager(
            self.storage.table("pairing_codes"), lambda: self._admin
        )
        grant = manager2.redeem(issued["code"], "ip-1")
        self.assertEqual(grant["role"], "user")


if __name__ == "__main__":
    unittest.main()
