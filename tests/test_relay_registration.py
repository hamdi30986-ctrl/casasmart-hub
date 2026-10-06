"""Proof contract and non-blocking retry lifecycle for relay enrollment."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from base64 import b64decode
from pathlib import Path

import aiohttp

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package  # noqa: E402

install_casasmart_package()

from casasmart.push_crypto import PushSigner  # noqa: E402
from casasmart.relay_registration import (  # noqa: E402
    RelayRegistrar,
    build_registration_proof,
    canonical_registration_payload,
    is_activation_code_format,
)
from casasmart.tls import ensure_tls_material  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    encode_dss_signature,
)


class _FakeContent:
    def __init__(self, payload, chunk_size: int | None = None) -> None:
        self._raw = json.dumps(payload).encode()
        self._offset = 0
        self._chunk_size = chunk_size

    async def read(self, limit: int) -> bytes:
        size = min(limit, self._chunk_size or limit)
        chunk = self._raw[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeResponse:
    def __init__(self, status: int, payload, headers=None, chunk_size=None) -> None:
        self.status = status
        self.content = _FakeContent(payload, chunk_size)
        self.headers = headers or {}


class _FakeContext:
    def __init__(self, outcome) -> None:
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeContext(self.outcomes.pop(0))


class _FakeEntry:
    def __init__(self) -> None:
        self.tasks: list[dict] = []

    def async_create_background_task(self, hass, coro, *, name) -> None:
        self.tasks.append({"hass": hass, "coro": coro, "name": name})


class RelayRegistrationTests(unittest.IsolatedAsyncioTestCase):
    ACTIVATION_CODE = f"CSACT1.{'a' * 40}.{'b' * 86}"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.material = ensure_tls_material(Path(self._tmp.name))
        self.push_signer = PushSigner(Ed25519PrivateKey.generate())
        self._nonce_value = 0

    def _nonce(self, byte_count: int) -> str:
        self.assertEqual(byte_count, 32)
        self._nonce_value += 1
        return f"{self._nonce_value:064x}"

    def _registrar(self, session, **overrides) -> RelayRegistrar:
        kwargs = {
            "session": session,
            "registration_url": "https://relay.example/register-hub",
            "hub_id": self.material.identity_fingerprint,
            "identity_signer": self.material.identity_signer,
            "push_signer": self.push_signer,
            "clock": lambda: 1_700_000_000,
            "nonce_factory": self._nonce,
            "random_value": lambda: 0.5,
            "activation_code": self.ACTIVATION_CODE,
        }
        kwargs.update(overrides)
        return RelayRegistrar(**kwargs)

    def test_proof_contains_required_fields_and_valid_identity_signature(self) -> None:
        proof = build_registration_proof(
            identity_signer=self.material.identity_signer,
            push_public_key=self.push_signer.public_key_hex,
            hub_id=self.material.identity_fingerprint,
            timestamp=1_700_000_000,
            nonce="ab" * 32,
        )
        self.assertEqual(
            set(proof),
            {
                "version",
                "hub_id",
                "identity_public_key",
                "push_public_key",
                "timestamp",
                "nonce",
                "signature",
            },
        )
        self.assertEqual(len(b64decode(proof["signature"])), 64)
        self.assertEqual(proof["push_public_key"], self.push_signer.public_key_hex)

        raw = b64decode(proof.pop("signature"))
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:], "big")
        identity_public_key = serialization.load_der_public_key(
            b64decode(proof["identity_public_key"])
        )
        identity_public_key.verify(
            encode_dss_signature(r, s),
            canonical_registration_payload(proof),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_activation_code_is_part_of_the_signed_identity_proof(self) -> None:
        proof = build_registration_proof(
            identity_signer=self.material.identity_signer,
            push_public_key=self.push_signer.public_key_hex,
            hub_id=self.material.identity_fingerprint,
            timestamp=1_700_000_000,
            nonce="ab" * 32,
            activation_code=self.ACTIVATION_CODE,
        )
        self.assertEqual(proof["activation_code"], self.ACTIVATION_CODE)
        signature = b64decode(proof.pop("signature"))
        identity_public_key = serialization.load_der_public_key(
            b64decode(proof["identity_public_key"])
        )
        identity_public_key.verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            ),
            canonical_registration_payload(proof),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_activation_envelope_shape_is_bounded(self) -> None:
        self.assertTrue(is_activation_code_format(self.ACTIVATION_CODE))
        self.assertFalse(is_activation_code_format("CSACT1.short.bad"))
        self.assertFalse(is_activation_code_format(self.ACTIVATION_CODE + "="))

    async def test_success_stops_without_sleeping(self) -> None:
        session = _FakeSession([_FakeResponse(201, {"registered": True})])
        sleeps: list[float] = []
        callbacks: list[str] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        registrar = self._registrar(
            session,
            sleep=_sleep,
            on_success=lambda: callbacks.append("success"),
        )
        await registrar.async_run()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(callbacks, ["success"])
        self.assertIsNone(registrar._activation_code)
        sent = session.calls[0]["json"]
        self.assertEqual(sent["activation_code"], self.ACTIVATION_CODE)
        self.assertNotIn("admin_token", sent)
        self.assertNotIn("authorization", session.calls[0])

    async def test_already_registered_is_success(self) -> None:
        session = _FakeSession(
            [_FakeResponse(200, {"registered": True, "status": "already_registered"})]
        )
        await self._registrar(session).async_run()
        self.assertEqual(len(session.calls), 1)

    async def test_existing_binding_can_retry_without_activation_code(self) -> None:
        session = _FakeSession(
            [_FakeResponse(200, {"registered": True, "status": "already_registered"})]
        )
        await self._registrar(session, activation_code=None).async_run()
        self.assertNotIn("activation_code", session.calls[0]["json"])

    async def test_partial_stream_reads_are_consumed_through_eof(self) -> None:
        session = _FakeSession([_FakeResponse(201, {"registered": True}, chunk_size=3)])
        await self._registrar(session).async_run()
        self.assertEqual(len(session.calls), 1)

    async def test_transient_failures_retry_with_exponential_and_retry_after(
        self,
    ) -> None:
        session = _FakeSession(
            [
                aiohttp.ClientConnectionError("offline"),
                _FakeResponse(
                    429, {"error": "registration_rate_limited"}, {"retry-after": "20"}
                ),
                _FakeResponse(200, {"registered": True}),
            ]
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        await self._registrar(session, sleep=_sleep).async_run()
        self.assertEqual(sleeps, [5.0, 20.0])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(
            [call["json"]["nonce"] for call in session.calls],
            [f"{value:064x}" for value in (1, 2, 3)],
        )

    async def test_unsynchronized_clock_rejection_retries_with_fresh_proof(
        self,
    ) -> None:
        session = _FakeSession(
            [
                _FakeResponse(401, {"error": "timestamp_out_of_range"}),
                _FakeResponse(200, {"registered": True}),
            ]
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        await self._registrar(session, sleep=_sleep).async_run()
        self.assertEqual(sleeps, [5.0])
        self.assertNotEqual(
            session.calls[0]["json"]["nonce"], session.calls[1]["json"]["nonce"]
        )

    async def test_retry_delay_is_capped(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse(429, {"error": "limited"}, {"retry-after": "99999"}),
                _FakeResponse(200, {"registered": True}),
            ]
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        await self._registrar(session, sleep=_sleep, max_backoff=30).async_run()
        self.assertEqual(sleeps, [30])

    async def test_binding_conflict_fails_closed_without_retry(self) -> None:
        session = _FakeSession([_FakeResponse(409, {"error": "binding_conflict"})])
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        await self._registrar(session, sleep=_sleep).async_run()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])

    async def test_expired_activation_is_permanent_and_actionable(self) -> None:
        session = _FakeSession([_FakeResponse(403, {"error": "activation_expired"})])
        failures: list[str] = []
        registrar = self._registrar(
            session,
            on_permanent_failure=failures.append,
        )
        with self.assertLogs("casasmart.relay_registration", level="ERROR") as logs:
            await registrar.async_run()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(failures, ["relay activation rejected (activation_expired)"])
        self.assertNotIn(self.ACTIVATION_CODE, failures[0])
        self.assertNotIn(self.ACTIVATION_CODE, "\n".join(logs.output))
        self.assertNotIn(self.ACTIVATION_CODE, session.calls[0]["url"])
        self.assertIsNone(registrar._activation_code)

    def test_registration_endpoint_is_inspectable_without_secret_state(self) -> None:
        registrar = self._registrar(_FakeSession([]))
        self.assertEqual(
            registrar.registration_url, "https://relay.example/register-hub"
        )

    async def test_start_is_non_blocking_and_only_schedules_background_task(
        self,
    ) -> None:
        session = _FakeSession([_FakeResponse(200, {"registered": True})])
        entry = _FakeEntry()
        hass = object()
        registrar = self._registrar(session)

        registrar.start(hass, entry)

        self.assertEqual(session.calls, [])
        self.assertEqual(len(entry.tasks), 1)
        self.assertEqual(entry.tasks[0]["name"], "casasmart-relay-registration")
        entry.tasks[0]["coro"].close()


if __name__ == "__main__":
    unittest.main()
