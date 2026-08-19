"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import time
from base64 import b64encode
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

REGISTRATION_VERSION = 1
REGISTRATION_NONCE_BYTES = 32
REGISTRATION_TIMEOUT_SECONDS = 10
REGISTRATION_INITIAL_BACKOFF_SECONDS = 5.0
REGISTRATION_MAX_BACKOFF_SECONDS = 6 * 60 * 60.0
REGISTRATION_LOG_EVERY_ATTEMPTS = 8
REGISTRATION_MAX_RESPONSE_BYTES = 4096
_ACTIVATION_CODE_RE = re.compile(
    r"^CSACT1\.[A-Za-z0-9_-]{1,2048}\.[A-Za-z0-9_-]{86}$"
)


def is_activation_code_format(value: object) -> bool:
    """CasaSmart runtime component."""
    return isinstance(value, str) and _ACTIVATION_CODE_RE.fullmatch(value) is not None


class IdentityProofSigner(Protocol):
    """CasaSmart runtime component."""

    @property
    def public_spki_der(self) -> bytes: ...

    def sign(self, message: bytes) -> bytes: ...


class PushPublicKey(Protocol):
    @property
    def public_key_hex(self) -> str: ...


def canonical_registration_payload(payload: dict[str, Any]) -> bytes:
    """CasaSmart runtime component."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_registration_proof(
    *,
    identity_signer: IdentityProofSigner,
    push_public_key: str,
    hub_id: str,
    timestamp: int,
    nonce: str,
    activation_code: str | None = None,
) -> dict[str, Any]:
    """CasaSmart runtime component."""
    unsigned: dict[str, Any] = {
        "version": REGISTRATION_VERSION,
        "hub_id": hub_id,
        "identity_public_key": b64encode(identity_signer.public_spki_der).decode(
            "ascii"
        ),
        "push_public_key": push_public_key,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    if activation_code is not None:
        unsigned["activation_code"] = activation_code
    signature = identity_signer.sign(canonical_registration_payload(unsigned))
    return {**unsigned, "signature": b64encode(signature).decode("ascii")}


@dataclass(frozen=True)
class _AttemptResult:
    success: bool
    permanent: bool = False
    retry_after: float | None = None
    reason: str = ""


class RelayRegistrar:
    """CasaSmart runtime component."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        registration_url: str,
        hub_id: str,
        identity_signer: IdentityProofSigner,
        push_signer: PushPublicKey,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[int], str] = secrets.token_hex,
        sleep: Callable[[float], Any] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        initial_backoff: float = REGISTRATION_INITIAL_BACKOFF_SECONDS,
        max_backoff: float = REGISTRATION_MAX_BACKOFF_SECONDS,
        activation_code: str | None = None,
        on_success: Callable[[], Any] | None = None,
        on_permanent_failure: Callable[[str], Any] | None = None,
    ) -> None:
        self._session = session
        self._registration_url = registration_url
        self._hub_id = hub_id
        self._identity_signer = identity_signer
        self._push_signer = push_signer
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._sleep = sleep
        self._random_value = random_value
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._activation_code = activation_code
        self._on_success = on_success
        self._on_permanent_failure = on_permanent_failure
        self._task: asyncio.Task[Any] | None = None

    @property
    def registration_url(self) -> str:
        """CasaSmart runtime component."""
        return self._registration_url

    def start(self, hass, entry) -> None:
        """CasaSmart runtime component."""
        task = entry.async_create_background_task(
            hass,
            self.async_run(),
            name="casasmart-relay-registration",
        )
        if isinstance(task, asyncio.Task):
            self._task = task

    def stop(self) -> None:
        """CasaSmart runtime component."""
        self._activation_code = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    async def async_run(self) -> None:
        """CasaSmart runtime component."""
        attempt = 0
        while True:
            attempt += 1
            result = await self._async_attempt()
            if result.success:



                self._activation_code = None
                await self._async_callback(self._on_success)
                _LOGGER.info(
                    "Push relay registration ready (hub_id=%s)", self._hub_id
                )
                return
            if result.permanent:



                self._activation_code = None
                await self._async_callback(self._on_permanent_failure, result.reason)
                _LOGGER.error(
                    "Push relay automatic registration stopped: %s; submit a fresh "
                    "Hub activation code from the CasaSmart integration options",
                    result.reason,
                )
                return

            base_delay = min(
                self._max_backoff,
                result.retry_after
                if result.retry_after is not None
                else self._initial_backoff * (2 ** min(attempt - 1, 20)),
            )


            delay = min(
                self._max_backoff,
                max(0.1, base_delay * (0.5 + self._random_value())),
            )
            if attempt == 1 or attempt % REGISTRATION_LOG_EVERY_ATTEMPTS == 0:
                _LOGGER.warning(
                    "Push relay registration deferred (%s); retrying in %.0fs",
                    result.reason,
                    delay,
                )
            else:
                _LOGGER.debug(
                    "Push relay registration retry %d in %.0fs (%s)",
                    attempt,
                    delay,
                    result.reason,
                )
            await self._sleep(delay)

    async def _async_attempt(self) -> _AttemptResult:
        proof = build_registration_proof(
            identity_signer=self._identity_signer,
            push_public_key=self._push_signer.public_key_hex,
            hub_id=self._hub_id,
            timestamp=int(self._clock()),
            nonce=self._nonce_factory(REGISTRATION_NONCE_BYTES),
            activation_code=self._activation_code,
        )
        timeout = aiohttp.ClientTimeout(total=REGISTRATION_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                self._registration_url,
                json=proof,
                timeout=timeout,
            ) as response:
                body = await self._read_response(response)
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            return _AttemptResult(False, reason=type(err).__name__)

        response_state = body.get("status") or body.get("error")
        if status in (200, 409) and response_state == "already_registered":
            return _AttemptResult(True)
        if 200 <= status < 300 and body.get("registered") is True:
            return _AttemptResult(True)
        if status == 409 and response_state == "binding_conflict":
            return _AttemptResult(False, permanent=True, reason="binding conflict")



        if response_state in ("timestamp_out_of_range", "replay_detected"):
            return _AttemptResult(False, reason=f"relay response {status}")
        if response_state == "activation_not_yet_valid":
            return _AttemptResult(False, reason="activation not valid yet")
        if response_state in (
            "activation_required",
            "activation_not_configured",
            "activation_invalid",
            "activation_expired",
            "activation_already_used",
            "activation_wrong_issuer",
            "activation_wrong_audience",
            "activation_wrong_version",
        ):
            return _AttemptResult(
                False,
                permanent=True,
                reason=f"relay activation rejected ({response_state})",
            )
        if status in (400, 403, 413, 422) or response_state in (
            "hub_id_mismatch",
            "invalid_identity_signature",
            "validation_failed",
        ):
            return _AttemptResult(
                False,
                permanent=True,
                reason=f"relay rejected proof ({status} {response_state or 'unknown'})",
            )

        retry_after = None
        if status == 429:
            retry_after = self._parse_retry_after(response.headers.get("retry-after"))
        return _AttemptResult(
            False,
            retry_after=retry_after,
            reason=f"relay response {status}",
        )

    @staticmethod
    async def _read_response(response) -> dict[str, Any]:
        """CasaSmart runtime component."""
        try:
            chunks: list[bytes] = []
            total = 0
            while total <= REGISTRATION_MAX_RESPONSE_BYTES:
                remaining = REGISTRATION_MAX_RESPONSE_BYTES + 1 - total
                chunk = await response.content.read(min(1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            if len(raw) > REGISTRATION_MAX_RESPONSE_BYTES:
                return {}
            decoded = json.loads(raw.decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        except (UnicodeDecodeError, ValueError, aiohttp.ClientError):
            return {}

    @staticmethod
    async def _async_callback(callback: Callable[..., Any] | None, *args: Any) -> None:
        """CasaSmart runtime component."""
        if callback is None:
            return
        result = callback(*args)
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _parse_retry_after(raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None
