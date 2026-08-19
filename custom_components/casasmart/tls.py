"""CasaSmart runtime component."""

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
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)





IDENTITY_KEY_FILENAME = "identity_key.pem"

TLS_CERT_FILENAME = "tls_cert.pem"
TLS_KEY_FILENAME = "tls_key.pem"

TLS_CERT_VALIDITY_DAYS = 365
TLS_CERT_RENEW_MARGIN_DAYS = 30



_ISSUER_CN = "CasaSmart Hub Identity"
_SUBJECT_CN = "casasmart-hub"
_SAN_DNS = "casasmart-hub.local"



_BACKDATE = timedelta(hours=1)


class IdentityError(Exception):
    """CasaSmart runtime component."""


class TlsIdentitySigner:
    """CasaSmart runtime component."""

    _SCALAR_BYTES = 32

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._private_key = private_key
        self._public_spki_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @property
    def public_spki_der(self) -> bytes:
        """CasaSmart runtime component."""
        return self._public_spki_der

    def sign(self, message: bytes) -> bytes:
        """CasaSmart runtime component."""
        der_signature = self._private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        return r.to_bytes(self._SCALAR_BYTES, "big") + s.to_bytes(
            self._SCALAR_BYTES, "big"
        )


@dataclass(frozen=True)
class TlsMaterial:
    """CasaSmart runtime component."""

    identity_public_pem: str
    identity_fingerprint: str
    identity_signer: TlsIdentitySigner
    cert_path: Path
    key_path: Path
    cert_not_after: datetime
    leaf_rotated: bool





def _load_or_create_identity(data_dir: Path) -> ec.EllipticCurvePrivateKey:
    key_path = data_dir / IDENTITY_KEY_FILENAME
    if key_path.exists():
        try:
            key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
        except (ValueError, TypeError) as err:

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





def _leaf_is_valid(
    cert_path: Path,
    key_path: Path,
    identity: ec.EllipticCurvePrivateKey,
) -> datetime | None:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)


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
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""

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
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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
