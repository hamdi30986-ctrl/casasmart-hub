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
    CODE_CLASS_BOOTSTRAP,
    CODE_CLASS_MEMBER,
    CodeInvalidError,
    HubAlreadyClaimedError,
    LanOnlyCodeError,
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
                "code_class": CODE_CLASS_MEMBER,  # admin-minted = member class
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

    # -- Phase 1: code classes + the remote-source policy ----------------------

    def test_minted_code_carries_member_class(self):
        issued = self.manager.generate_code("user")
        self.assertEqual(issued["code_class"], CODE_CLASS_MEMBER)
        [entry] = self.manager.list_codes()
        self.assertEqual(entry["code_class"], CODE_CLASS_MEMBER)

    def test_bootstrap_code_carries_bootstrap_class(self):
        self.manager.ensure_bootstrap_code()
        [entry] = self.manager.list_codes()
        self.assertEqual(entry["code_class"], CODE_CLASS_BOOTSTRAP)

    def test_member_code_redeems_from_remote_source(self):
        issued = self.manager.generate_code("user", rooms=["area_living"])
        grant = self.manager.redeem(issued["code"], "tunnel-ip", remote_source=True)
        self.assertEqual(grant["role"], "user")
        self.assertEqual(grant["code_class"], CODE_CLASS_MEMBER)

    def test_bootstrap_remote_redeem_refused_and_not_consumed(self):
        code = self.manager.ensure_bootstrap_code()
        # More attempts than the throttle allows: LanOnlyCodeError every time,
        # never ThrottledError — a code that was actually right burns no slot
        # (HubAlreadyClaimedError posture).
        for _ in range(throttle_mod.MAX_FAILURES + 1):
            with self.assertRaises(LanOnlyCodeError):
                self.manager.redeem(code, "tunnel-ip", remote_source=True)
        # NOT consumed: the legitimate on-LAN claim still works afterwards.
        grant = self.manager.redeem(code, "tunnel-ip")
        self.assertEqual(grant["role"], "admin")
        self.assertEqual(grant["code_class"], CODE_CLASS_BOOTSTRAP)

    def test_remote_policy_wins_over_claimed_answer(self):
        # A remote caller must never learn the hub's claim state: the owner
        # code on a CLAIMED hub gets the LAN-only refusal, not "already
        # paired" (which only the on-LAN path may see).
        code = self.manager.ensure_bootstrap_code()
        self._admin = True
        with self.assertRaises(LanOnlyCodeError):
            self.manager.redeem(code, "tunnel-ip", remote_source=True)

    def test_legacy_records_without_class_fail_closed(self):
        # Pre-Phase-1 rows have no code_class field: user/sub-admin rows
        # classify as member (only generate_code ever minted them); anything
        # admin-role classifies as bootstrap — fail closed.
        table = self.storage.table("pairing_codes")
        issued = self.manager.generate_code("user")
        legacy = dict(table[issued["code_id"]])
        del legacy["code_class"]
        table[issued["code_id"]] = legacy
        grant = self.manager.redeem(issued["code"], "tunnel-ip", remote_source=True)
        self.assertEqual(grant["code_class"], CODE_CLASS_MEMBER)
        code = self.manager.ensure_bootstrap_code()
        legacy = dict(table[BOOTSTRAP_CODE_ID])
        del legacy["code_class"]
        table[BOOTSTRAP_CODE_ID] = legacy
        with self.assertRaises(LanOnlyCodeError):
            self.manager.redeem(code, "tunnel-ip", remote_source=True)

    # -- Phase 5 (D5): (source, purpose) throttle buckets -----------------------

    def _lock_bucket(self, source, *, remote):
        """Burn MAX_FAILURES garbage redemptions into ONE (source, purpose)
        bucket, then prove that bucket's wall is up."""
        for _ in range(throttle_mod.MAX_FAILURES):
            with self.assertRaises(CodeInvalidError):
                self.manager.redeem("WRONGCOD", source, remote_source=remote)
        with self.assertRaises(ThrottledError):
            self.manager.redeem("WRONGCOD", source, remote_source=remote)

    def test_remote_lockout_does_not_block_lan_redemption(self):
        # The dispatch drill: a remote guessing burst (every tunnel caller
        # collapses onto one source string) locks the REMOTE bucket — the
        # owner's on-LAN bootstrap claim from the SAME source string goes
        # straight through.
        code = self.manager.ensure_bootstrap_code()
        self._lock_bucket("ip-shared", remote=True)
        grant = self.manager.redeem(code, "ip-shared")  # LAN purpose
        self.assertEqual(grant["role"], "admin")
        self.assertEqual(grant["code_class"], CODE_CLASS_BOOTSTRAP)

    def test_lan_lockout_does_not_block_remote_member_redemption(self):
        # ...and vice versa: a LAN lockout on the source leaves legitimate
        # remote member redemption from that source untouched.
        self._admin = True
        issued = self.manager.generate_code("user")
        self._lock_bucket("ip-shared", remote=False)
        grant = self.manager.redeem(issued["code"], "ip-shared", remote_source=True)
        self.assertEqual(grant["role"], "user")
        self.assertEqual(grant["code_class"], CODE_CLASS_MEMBER)

    def test_locked_remote_bucket_blocks_even_a_valid_remote_code(self):
        # The wall precedes the code lookup: during a remote lockout even a
        # VALID member code is refused remotely (and NOT consumed) — but the
        # same code still redeems on the LAN, whose bucket is clean.
        self._admin = True
        issued = self.manager.generate_code("user")
        self._lock_bucket("ip-shared", remote=True)
        with self.assertRaises(ThrottledError):
            self.manager.redeem(issued["code"], "ip-shared", remote_source=True)
        grant = self.manager.redeem(issued["code"], "ip-shared")
        self.assertEqual(grant["role"], "user")

    def test_success_clears_only_its_own_bucket(self):
        # Buckets are fully independent: 4 remote failures survive a LAN
        # success on the same source — the 5th remote failure still raises
        # the remote wall (a success must not launder the other purpose's
        # count), while the LAN side stays open throughout.
        self._admin = True
        issued = self.manager.generate_code("user")
        for _ in range(throttle_mod.MAX_FAILURES - 1):
            with self.assertRaises(CodeInvalidError):
                self.manager.redeem("WRONGCOD", "ip-shared", remote_source=True)
        self.manager.redeem(issued["code"], "ip-shared")  # LAN success
        with self.assertRaises(CodeInvalidError):  # remote failure #5
            self.manager.redeem("WRONGCOD", "ip-shared", remote_source=True)
        with self.assertRaises(ThrottledError):  # remote wall is up
            self.manager.redeem("WRONGCOD", "ip-shared", remote_source=True)
        issued2 = self.manager.generate_code("user")
        grant = self.manager.redeem(issued2["code"], "ip-shared")  # LAN open
        self.assertEqual(grant["role"], "user")


if __name__ == "__main__":
    unittest.main()
