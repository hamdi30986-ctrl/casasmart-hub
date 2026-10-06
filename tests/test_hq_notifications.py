"""Security and contract tests for the private HQ reminder push ingress."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "casasmart_hq_notifications",
    ROOT / "custom_components" / "casasmart" / "hq_notifications.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HqNotificationVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = (
            self.private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        self.table: dict = {}
        self.verifier = MODULE.HqNotificationVerifier(self.table, self.public)
        self.now = 1_700_000_000
        self.raw = json.dumps(
            {
                "event_id": "hq-reminder:12345678",
                "source_type": "reminder",
                "target": "today",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def headers(self, *, nonce: str = "n" * 24, raw: bytes | None = None) -> dict:
        body = self.raw if raw is None else raw
        signature = self.private.sign(MODULE.canonical_request(self.now, nonce, body))
        return {
            "X-CasaSmart-HQ-Timestamp": str(self.now),
            "X-CasaSmart-HQ-Nonce": nonce,
            "X-CasaSmart-HQ-Signature": base64.b64encode(signature).decode("ascii"),
        }

    def test_valid_request_is_private_replay_safe_and_idempotent_after_acceptance(
        self,
    ) -> None:
        verified = self.verifier.verify(self.headers(), self.raw, self.now)
        self.assertEqual(verified.event_id, "hq-reminder:12345678")
        self.assertNotIn("title", self.raw.decode("utf-8"))
        self.verifier.reserve_nonce(verified.nonce, self.now)
        with self.assertRaisesRegex(MODULE.HqNotificationError, "replayed_request"):
            self.verifier.reserve_nonce(verified.nonce, self.now)
        self.assertIsNone(self.verifier.previous(verified.event_id))
        self.verifier.record_delivery(verified.event_id, "relay_accepted", now=self.now)
        self.assertEqual(
            self.verifier.previous(verified.event_id)["outcome"], "relay_accepted"
        )

    def test_failed_delivery_is_audited_but_remains_retryable(self) -> None:
        verified = self.verifier.verify(self.headers(), self.raw, self.now)
        self.verifier.reserve_nonce(verified.nonce, self.now)
        self.verifier.record_delivery(
            verified.event_id,
            "failed",
            "relay_unreachable",
            now=self.now,
        )
        self.assertIsNone(self.verifier.previous(verified.event_id))
        audit = [value for key, value in self.table.items() if key.startswith("audit:")]
        self.assertEqual(audit[0]["reason"], "relay_unreachable")

    def test_tamper_stale_wrong_key_schema_and_duplicate_json_are_rejected(
        self,
    ) -> None:
        cases = [
            (self.headers(), self.raw + b" ", self.now),
            (self.headers(), self.raw, self.now + 61),
        ]
        other = Ed25519PrivateKey.generate()
        wrong_signature = other.sign(
            MODULE.canonical_request(self.now, "x" * 24, self.raw)
        )
        cases.append(
            (
                {
                    "X-CasaSmart-HQ-Timestamp": str(self.now),
                    "X-CasaSmart-HQ-Nonce": "x" * 24,
                    "X-CasaSmart-HQ-Signature": base64.b64encode(
                        wrong_signature
                    ).decode("ascii"),
                },
                self.raw,
                self.now,
            )
        )
        duplicate = (
            b'{"event_id":"hq-reminder:12345678","event_id":"hq-reminder:87654321",'
            b'"source_type":"reminder","target":"today"}'
        )
        cases.append((self.headers(raw=duplicate), duplicate, self.now))
        extra = json.dumps(
            {
                "event_id": "hq-reminder:12345678",
                "source_type": "reminder",
                "target": "today",
                "title": "private",
            },
            separators=(",", ":"),
        ).encode()
        cases.append((self.headers(raw=extra), extra, self.now))
        for headers, raw, now in cases:
            with self.subTest(raw=raw), self.assertRaises(MODULE.HqNotificationError):
                self.verifier.verify(headers, raw, now)

    def test_public_key_parser_refuses_private_and_non_ed25519_keys(self) -> None:
        private_pem = self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        with self.assertRaises(MODULE.HqNotificationError):
            MODULE.normalize_public_key(private_pem)
        with self.assertRaises(MODULE.HqNotificationError):
            MODULE.normalize_public_key("not a key")
        canonical, fingerprint = MODULE.normalize_public_key(self.public)
        self.assertEqual(canonical, self.public)
        self.assertRegex(fingerprint, r"^[0-9a-f]{16}$")

    def test_replay_and_audit_storage_are_bounded(self) -> None:
        for index in range(MODULE.HQ_NOTIFICATION_MAX_NONCES + 5):
            nonce = f"nonce-{index:022d}"
            self.verifier.reserve_nonce(nonce, self.now)
        self.assertLessEqual(
            len([key for key in self.table if key.startswith("nonce:")]),
            MODULE.HQ_NOTIFICATION_MAX_NONCES,
        )
        for index in range(MODULE.HQ_NOTIFICATION_MAX_AUDIT_ROWS + 5):
            self.verifier.record_rejection(f"rejected_{index}", self.now)
        self.assertLessEqual(
            len([key for key in self.table if key.startswith("audit:")]),
            MODULE.HQ_NOTIFICATION_MAX_AUDIT_ROWS,
        )


class HqSenderNameTest(unittest.TestCase):
    """The push title is the owner's chosen sender name, or a neutral default."""

    def test_default_when_unset_or_blank(self) -> None:
        for value in (None, "", "   "):
            self.assertIsNone(MODULE.normalize_sender_name(value))
        self.assertEqual(MODULE.hq_push_title(None), "CasaSmart HQ")
        self.assertEqual(MODULE.hq_push_title(""), "CasaSmart HQ")

    def test_custom_name_is_trimmed_and_used(self) -> None:
        self.assertEqual(MODULE.normalize_sender_name("  Villa HQ "), "Villa HQ")
        self.assertEqual(MODULE.normalize_sender_name("مكتب المنزل"), "مكتب المنزل")
        self.assertEqual(MODULE.hq_push_title("Villa HQ"), "Villa HQ")

    def test_invalid_names_are_refused(self) -> None:
        for value in ("x" * 41, "line\nbreak", "tab\there", 42, ["HQ"]):
            with self.assertRaises(MODULE.HqNotificationError):
                MODULE.normalize_sender_name(value)

    def test_unicode_line_breaks_and_bidi_overrides_are_refused(self) -> None:
        # One visible line only, and no direction overrides that could make
        # the title display as something else.
        for char in ("\u2028", "\u2029", "\u0085", "\u202e", "\u2066", "\x00"):
            with self.assertRaises(MODULE.HqNotificationError):
                MODULE.normalize_sender_name(f"Villa{char}HQ")

    def test_joiners_used_by_emoji_and_scripts_are_allowed(self) -> None:
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467 HQ"  # ZWJ emoji
        persian = "\u062e\u0627\u0646\u0647\u200c\u0645\u0627"  # with a ZWNJ
        for name in (family, persian):
            self.assertEqual(MODULE.normalize_sender_name(name), name)

    def test_stored_garbage_falls_back_to_the_default(self) -> None:
        # hub_config.json is hand-editable; a bad value must not break pushes.
        self.assertEqual(MODULE.hq_push_title("x" * 41), "CasaSmart HQ")
        self.assertEqual(MODULE.hq_push_title(42), "CasaSmart HQ")


class HqNotificationSurfaceContractTest(unittest.TestCase):
    def test_route_is_registered_owner_only_and_uses_generic_content(self) -> None:
        push_api = (
            ROOT / "custom_components" / "casasmart" / "push_api.py"
        ).read_text()
        dispatcher = (
            ROOT / "custom_components" / "casasmart" / "push_dispatcher.py"
        ).read_text()
        api = (ROOT / "custom_components" / "casasmart" / "api.py").read_text()
        init = (ROOT / "custom_components" / "casasmart" / "__init__.py").read_text()

        self.assertIn('url = "/api/casasmart/notifications/hq"', push_api)
        self.assertIn("CasaSmartHqNotificationView(hass)", api)
        self.assertIn("PUSH_TYPE_HQ_REMINDER", dispatcher)
        owner_block = dispatcher[dispatcher.index("_OWNER_ONLY_TYPES") :]
        self.assertIn("PUSH_TYPE_HQ_REMINDER", owner_block.split(")", 1)[0])
        self.assertIn('"title": hq_push_title(', push_api)
        self.assertEqual(MODULE.hq_push_title(None), MODULE.HQ_DEFAULT_SENDER_NAME)
        self.assertIn('"body": "You have a private update."', push_api)
        self.assertNotIn('verified.event_id,\n                    "title"', push_api)
        self.assertIn("user is None or not user.is_admin", init)


if __name__ == "__main__":
    unittest.main()
