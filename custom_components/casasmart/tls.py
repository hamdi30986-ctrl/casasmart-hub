"""Hub TLS identity and the dedicated HTTPS listener.

A permanent P-256 identity key (what paired phones pin) signs a renewable leaf
certificate; CasaSmartTlsServer serves the CasaSmart views behind it.

Phones pin the identity key's SHA-256 SPKI fingerprint at first contact
instead of trusting a certificate authority, so the leaf can be re-minted near
expiry without affecting any phone. The identity key is never replaced
automatically: losing it unpairs every phone.
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)

# Files in the hub's data dir. The identity key is permanent; the leaf key and
# certificate are disposable and re-minted from it.
IDENTITY_KEY_FILENAME = "identity_key.pem"
TLS_CERT_FILENAME = "tls_cert.pem"
TLS_KEY_FILENAME = "tls_key.pem"

# Leaf lifetime, and how close to expiry the daily check re-mints it.
TLS_CERT_VALIDITY_DAYS = 365
TLS_CERT_RENEW_MARGIN_DAYS = 30

# Names on the leaf. Phones verify the pinned identity, not these.
_ISSUER_CN = "CasaSmart Hub Identity"
_SUBJECT_CN = "casasmart-hub"
_SAN_DNS = "casasmart-hub.local"

# A fresh leaf is valid from an hour back, so a client clock running slightly
# behind the hub's doesn't see a certificate from the future.
_BACKDATE = timedelta(hours=1)


# As on Home Assistant's own HTTP server: on shutdown, wait this long for open
# requests and then cancel them.
_SHUTDOWN_TIMEOUT = 10.0


# Set on the TLS listener's aiohttp app when it is trusted as LAN ingress
# (lan_ingress.py). Requests served by HA's own HTTP server never carry it.
TLS_LISTENER_TRUSTED_LAN = web.AppKey("casasmart_tls_listener_trusted_lan", bool)


def _spki_der(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """The public key as DER SubjectPublicKeyInfo."""
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class IdentityError(Exception):
    """The permanent identity key is unusable; it is never replaced automatically."""


class TlsIdentitySigner:
    """Signs push relay registration requests with the identity key.

    Callers get the public key and a sign operation; the private key stays
    inside this object.
    """

    _SCALAR_BYTES = 32

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._private_key = private_key
        self._public_spki_der = _spki_der(private_key.public_key())

    @property
    def public_spki_der(self) -> bytes:
        return self._public_spki_der

    def sign(self, message: bytes) -> bytes:
        """ECDSA-SHA256 signature as raw r || s (64 bytes, IEEE P1363).

        WebCrypto verifies this format; the cryptography package produces DER.
        """
        der_signature = self._private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        return r.to_bytes(self._SCALAR_BYTES, "big") + s.to_bytes(
            self._SCALAR_BYTES, "big"
        )


@dataclass(frozen=True)
class TlsMaterial:
    """Everything the listener and its callers need from one TLS check.

    identity_fingerprint is the value phones pin; mDNS and the handshake
    publish it too. leaf_rotated means a new leaf was minted and the listener
    must restart to serve it.
    """

    identity_public_pem: str
    identity_fingerprint: str
    identity_signer: TlsIdentitySigner
    cert_path: Path
    key_path: Path
    cert_not_after: datetime
    leaf_rotated: bool


def _load_or_create_identity(data_dir: Path) -> ec.EllipticCurvePrivateKey:
    """Load the permanent identity key, creating it on first start only.

    Raises IdentityError when the file exists but can't be used; it is never
    replaced automatically.
    """
    key_path = data_dir / IDENTITY_KEY_FILENAME
    if key_path.exists():
        try:
            key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
        except (ValueError, TypeError) as err:
            # Never re-key silently: every paired phone pins this key.
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
    # Created with mode 0600 so the key is never readable by others, even briefly.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    _LOGGER.info("Generated permanent TLS identity key at %s", key_path)
    return key


def _identity_public_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def _identity_fingerprint(key: ec.EllipticCurvePrivateKey) -> str:
    """SHA-256 hex of the identity public key's SPKI DER: the pinned value."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(_spki_der(key.public_key()))
    return digest.finalize().hex()


def _leaf_is_valid(
    cert_path: Path,
    key_path: Path,
    identity: ec.EllipticCurvePrivateKey,
) -> datetime | None:
    """The stored leaf's expiry while it can still be served, else None.

    Servable means it parses, its key matches the certificate, our identity
    key signed it and it is outside the renewal margin. On None the caller
    mints a new leaf.
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
    if _spki_der(leaf_key.public_key()) != _spki_der(cert_pub):
        return None

    try:
        identity.public_key().verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        # Signed by another identity (restored from the wrong backup?), so
        # phones pinning ours would reject it.
        return None

    not_after = cert.not_valid_after_utc
    margin = timedelta(days=TLS_CERT_RENEW_MARGIN_DAYS)
    if datetime.now(UTC) >= not_after - margin:
        return None
    return not_after


def _mint_leaf(
    cert_path: Path,
    key_path: Path,
    identity: ec.EllipticCurvePrivateKey,
    validity_days: int,
) -> datetime:
    """Mint a leaf key and certificate signed by the identity key."""
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    # X.509 times have whole-second resolution; truncating here keeps the
    # reported expiry equal to the certificate's.
    not_after = (now + timedelta(days=validity_days)).replace(microsecond=0)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _SUBJECT_CN)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _ISSUER_CN)]))
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(_SAN_DNS)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(identity, hashes.SHA256())
    )

    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # Key first, certificate second: a crash in between leaves a mismatched
    # pair, which the next check rejects and re-mints.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    """Load the identity, re-mint the leaf if needed, and describe both.

    Blocking (file I/O and key generation): run it in the executor. Called at
    setup and on the daily check. Raises IdentityError for an unusable
    identity key.
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
        identity_signer=TlsIdentitySigner(identity),
        cert_path=cert_path,
        key_path=key_path,
        cert_not_after=not_after,
        leaf_rotated=rotated,
    )


class CasaSmartTlsServer:
    """The CasaSmart API on its own HTTPS port.

    Serves the same views as the HA port (both come from api.build_views)
    behind the hub-issued leaf. A rotated leaf goes live by restarting the
    site; the runner is kept.
    """

    def __init__(
        self,
        hass,
        port: int,
        material: TlsMaterial,
        *,
        trusted_lan_ingress: bool = False,
    ) -> None:
        self._hass = hass
        self._port = port
        self._material = material
        self._trusted_lan_ingress = trusted_lan_ingress
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        """The configured listening port."""
        return self._port

    @property
    def material(self) -> TlsMaterial:
        """The TLS material currently served."""
        return self._material

    def _ssl_context(self) -> ssl.SSLContext:
        """Server context for the current leaf (blocking: reads the files)."""
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
            app[TLS_LISTENER_TRUSTED_LAN] = self._trusted_lan_ingress
            for view in views:
                view.register(self._hass, app, app.router)
            # No handler_cancellation: a phone dropping mid-request must not
            # cancel a service call half-way through a device command.
            self._runner = web.AppRunner(app, shutdown_timeout=_SHUTDOWN_TIMEOUT)
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
        """Adopt re-checked material from the daily check.

        A rotated leaf, or a listener that never bound, restarts the TCP site
        with a fresh SSL context. Phones reconnect without noticing because
        the pinned identity is unchanged.
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
        """Close the listener and release the aiohttp runner."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
