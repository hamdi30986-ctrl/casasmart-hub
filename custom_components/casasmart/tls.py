"""LAN TLS — permanent identity key + short-lived self-issued certs (B10).

The plan's pinning model (audit round 5 rework): the app pins the hub's
**permanent identity public key**, never a leaf certificate. The hub:

- generates a P-256 **identity keypair** once, at first setup — it never
  expires and never leaves the box (``identity_key.pem``, 0600);
- issues itself short-lived (~1 year) TLS **leaf certs signed by the
  identity key** (``tls_cert.pem`` / ``tls_key.pem``), re-minted
  automatically when within the renewal margin — rotation is invisible
  to paired phones because the pin (the identity key) never changes;
- serves the whole CasaSmart API (REST + WS) over HTTPS on its own
  dedicated port — which is also what the Cloudflare tunnel will route
  to (plan: tunnel never exposes HA's own port).

The app validates a presented cert by checking its signature against the
pinned identity public key (B11 dual-validation, LAN mode). Standard
chain/hostname validation is deliberately not the contract here.

Failure postures, chosen deliberately:

- **Corrupt identity key** -> hard error (``IdentityError``). Re-keying
  silently would break the pin on every paired phone; a human must
  decide (delete the file to re-key an unclaimed/re-onboarded hub).
- **Bad leaf material** (expired, unparseable, wrong signer, key
  mismatch) -> re-mint. Leaves are disposable by design.
- **TLS port won't bind** -> log loudly, keep the integration up (the
  plain views on HA's port still work in dev); retried on the daily
  rotation tick.
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)

# No relative imports — this module stays flat-importable for the test
# suite, like the other pure modules (pairing, recovery, ws_protocol).
# Permanent identity keypair: generated once, pinned by every paired
# phone, never rotated automatically (see failure postures above).
IDENTITY_KEY_FILENAME = "identity_key.pem"
# Short-lived leaf material, signed by the identity key. Disposable.
TLS_CERT_FILENAME = "tls_cert.pem"
TLS_KEY_FILENAME = "tls_key.pem"
# Plan: "~1 year validity", re-minted inside the renewal margin.
TLS_CERT_VALIDITY_DAYS = 365
TLS_CERT_RENEW_MARGIN_DAYS = 30

# Cert subject/issuer names — cosmetic (the pin is the identity KEY),
# but stable so support can recognize a CasaSmart hub cert on sight.
_ISSUER_CN = "CasaSmart Hub Identity"
_SUBJECT_CN = "casasmart-hub"
_SAN_DNS = "casasmart-hub.local"

# Leaf not_valid_before is backdated so a hub with a slightly slow clock
# (NTP not yet synced at first boot) still presents a valid cert.
_BACKDATE = timedelta(hours=1)


class IdentityError(Exception):
    """The permanent identity key is unusable — never auto-recovered."""


@dataclass(frozen=True)
class TlsMaterial:
    """Everything the TLS listener and the pinning contract need."""

    identity_public_pem: str
    identity_fingerprint: str  # SHA-256 over the SPKI DER, lowercase hex
    cert_path: Path
    key_path: Path
    cert_not_after: datetime
    leaf_rotated: bool


# -- identity (permanent, blocking I/O — executor only) ------------------------


def _load_or_create_identity(data_dir: Path) -> ec.EllipticCurvePrivateKey:
    key_path = data_dir / IDENTITY_KEY_FILENAME
    if key_path.exists():
        try:
            key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
        except (ValueError, TypeError) as err:
            # NEVER silently re-key: every paired phone pins this key.
            raise IdentityError(
                f"Identity key at {key_path} is unreadable ({err}). "
                "Restore it from backup, or delete the file to re-key — "
                "re-keying unpairs every phone."
            ) from err
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise IdentityError(
                f"Identity key at {key_path} is not P-256 — refusing to use "
                "or replace it automatically."
            )
        return key

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # 0600 from the first byte — never world-readable, even briefly.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    _LOGGER.info("Generated permanent TLS identity key at %s", key_path)
    return key


def _identity_public_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _identity_fingerprint(key: ec.EllipticCurvePrivateKey) -> str:
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return digest.finalize().hex()


# -- leaf cert (disposable, blocking I/O — executor only) -----------------------


def _leaf_is_valid(
    cert_path: Path,
    key_path: Path,
    identity: ec.EllipticCurvePrivateKey,
) -> datetime | None:
    """The stored leaf's expiry when it's still good to serve, else None.

    "Good" = parseable, key matches cert, signed by OUR identity key, and
    not inside the renewal margin. Anything off -> None (caller re-mints;
    leaves are disposable).
    """
    if not cert_path.exists() or not key_path.exists():
        return None
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        leaf_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
    except (ValueError, TypeError):
        return None

    cert_pub = cert.public_key()
    if not isinstance(leaf_key, ec.EllipticCurvePrivateKey) or not isinstance(
        cert_pub, ec.EllipticCurvePublicKey
    ):
        return None
    if leaf_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != cert_pub.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ):
        return None

    try:
        identity.public_key().verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        # Signed by some other identity (restored from the wrong backup?)
        # — a phone pinning OUR identity would reject it. Re-mint.
        return None

    not_after = cert.not_valid_after_utc
    margin = timedelta(days=TLS_CERT_RENEW_MARGIN_DAYS)
    if datetime.now(timezone.utc) >= not_after - margin:
        return None
    return not_after


def _mint_leaf(
    cert_path: Path,
    key_path: Path,
    identity: ec.EllipticCurvePrivateKey,
    validity_days: int,
) -> datetime:
    """Mint a fresh leaf keypair + cert signed by the identity key."""
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    # X.509 time has whole-second resolution — truncate up front so the
    # value we report always equals what the cert actually says.
    not_after = (now + timedelta(days=validity_days)).replace(microsecond=0)
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _SUBJECT_CN)])
        )
        .issuer_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _ISSUER_CN)])
        )
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(_SAN_DNS)]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(identity, hashes.SHA256())
    )

    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(
        key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_pem)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _LOGGER.info(
        "Minted TLS leaf cert (valid until %s) signed by the hub identity",
        not_after.date(),
    )
    return not_after


def ensure_tls_material(
    data_dir: Path, validity_days: int = TLS_CERT_VALIDITY_DAYS
) -> TlsMaterial:
    """Load-or-create identity + a serviceable leaf (blocking — executor).

    Raises ``IdentityError`` only for an unusable identity key — leaf
    problems self-heal by re-minting.
    """
    identity = _load_or_create_identity(data_dir)
    cert_path = data_dir / TLS_CERT_FILENAME
    key_path = data_dir / TLS_KEY_FILENAME

    not_after = _leaf_is_valid(cert_path, key_path, identity)
    rotated = not_after is None
    if rotated:
        not_after = _mint_leaf(cert_path, key_path, identity, validity_days)

    return TlsMaterial(
        identity_public_pem=_identity_public_pem(identity),
        identity_fingerprint=_identity_fingerprint(identity),
        cert_path=cert_path,
        key_path=key_path,
        cert_not_after=not_after,
        leaf_rotated=rotated,
    )


# -- the HTTPS listener ---------------------------------------------------------


class CasaSmartTlsServer:
    """The CasaSmart API on its own HTTPS port.

    Serves the exact same view instances/registration path as the plain
    HA-port API (one code path — same auth gates, same filtering), just
    behind TLS with the hub-issued leaf. Restarting the site (not the
    runner) is how a rotated leaf goes live.
    """

    def __init__(self, hass, port: int, material: TlsMaterial) -> None:
        self._hass = hass
        self._port = port
        self._material = material
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._site is not None

    @property
    def material(self) -> TlsMaterial:
        return self._material

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            str(self._material.cert_path), str(self._material.key_path)
        )
        return context

    async def async_start(self, views) -> bool:
        """Bring the listener up. False (logged) when the port won't bind."""
        if self._runner is None:
            app = web.Application()
            for view in views:
                view.register(self._hass, app, app.router)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
        try:
            context = await self._hass.async_add_executor_job(self._ssl_context)
            site = web.TCPSite(self._runner, port=self._port, ssl_context=context)
            await site.start()
        except OSError as err:
            _LOGGER.error(
                "CasaSmart TLS listener failed to bind port %s: %s "
                "(will retry on the daily certificate check)",
                self._port,
                err,
            )
            return False
        self._site = site
        _LOGGER.info("CasaSmart API serving HTTPS on port %s", self._port)
        return True

    async def async_refresh(self, material: TlsMaterial, views) -> None:
        """Adopt re-checked material; restart the site only when needed.

        Called from the daily tick. A rotated leaf (or a listener that
        never bound) restarts the TCP site with a fresh SSL context —
        phones reconnect transparently because the pin didn't change.
        """
        rotated = material.leaf_rotated
        self._material = material
        if self._site is not None and not rotated:
            return
        if self._site is not None:
            await self._site.stop()
            self._site = None
        await self.async_start(views)

    async def async_stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
