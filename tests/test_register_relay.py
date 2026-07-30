"""Tests for tools/register_relay.py — the relay push-identity provisioning CLI.

The script is deliberately standalone (stdlib + optional cryptography, no
homeassistant imports), so no HA stubs are needed. What IS pinned here:

- the duplicated constants (relay default URL, file names, config keys) stay
  byte-equal to their sources in const.py / tls.py / push_crypto.py, so the
  standalone copy can never silently drift from the hub;
- hub_id derivation is byte-identical to tls.py's ``identity_fingerprint``
  (both the cryptography path and the openssl fallback);
- the public key sent to the relay is exactly the one ``PushSigner`` signs
  with, and the stale-mirror cross-check refuses to register a wrong key;
- the wire seam: a real HTTP server receives the exact bearer header + JSON
  body the relay's /admin/register-hub expects, and every failure mode
  (missing token, unreachable relay, rejected token) maps to its documented
  exit code with an operator-readable message.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
_CC = _REPO / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))

# push_crypto imports ``.storage`` relatively, so it needs the package-shaped
# import surface the shared harness provides (same pattern as the dispatcher
# suite). No HA stubs are required — the script under test never imports HA.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package  # noqa: E402

install_casasmart_package()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

import const  # noqa: E402  (flat import, same pattern as the other suites)


def _load_script():
    """Import tools/register_relay.py as a module (it's a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "register_relay", _REPO / "tools" / "register_relay.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = _load_script()


def _write_identity(data_dir: Path) -> ec.EllipticCurvePrivateKey:
    """A real P-256 TLS identity PEM, like tls.py's first boot writes."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (data_dir / rr.IDENTITY_KEY_FILENAME).write_bytes(pem)
    return key


def _write_push_key(data_dir: Path, mirror: bool = True) -> str:
    """A real Ed25519 push key file (+ hub_config mirror), like first boot."""
    key = Ed25519PrivateKey.generate()
    (data_dir / rr.PUSH_IDENTITY_KEY_FILENAME).write_bytes(key.private_bytes_raw())
    public_hex = key.public_key().public_bytes_raw().hex()
    if mirror:
        (data_dir / rr.HUB_CONFIG_FILENAME).write_text(
            json.dumps({rr.PUSH_PUBLIC_KEY_CONFIG_KEY: public_hex})
        )
    return public_hex


def _expected_fingerprint(key: ec.EllipticCurvePrivateKey) -> str:
    """The tls.py fingerprint recipe, computed independently."""
    import hashlib

    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).hexdigest()


def _run_main(argv, env=None):
    """Run main() with captured stdout/stderr and a scrubbed token env."""
    scrubbed = {"RELAY_ADMIN_TOKEN": "", "ADMIN_TOKEN": ""}
    if env:
        scrubbed.update(env)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", scrubbed):
        with redirect_stdout(out), redirect_stderr(err):
            code = rr.main(argv)
    return code, out.getvalue(), err.getvalue()


class AntiDriftTests(unittest.TestCase):
    """The standalone copies must stay equal to their canonical sources."""

    def test_default_relay_matches_const(self):
        self.assertEqual(rr.DEFAULT_RELAY_URL, const.PUSH_RELAY_URL_DEFAULT)

    def test_file_and_key_names_match_sources(self):
        import tls
        from casasmart import push_crypto

        self.assertEqual(rr.DATA_DIR_NAME, const.DATA_DIR_NAME)
        self.assertEqual(rr.HUB_CONFIG_FILENAME, const.HUB_CONFIG_FILENAME)
        self.assertEqual(rr.PUSH_RELAY_URL_CONFIG_KEY, const.PUSH_RELAY_URL_CONFIG_KEY)
        self.assertEqual(rr.IDENTITY_KEY_FILENAME, tls.IDENTITY_KEY_FILENAME)
        self.assertEqual(
            rr.PUSH_IDENTITY_KEY_FILENAME, push_crypto.PUSH_IDENTITY_KEY_FILENAME
        )
        self.assertEqual(
            rr.PUSH_PUBLIC_KEY_CONFIG_KEY, push_crypto.PUSH_PUBLIC_KEY_CONFIG_KEY
        )


class HubIdDerivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_matches_tls_identity_fingerprint(self):
        key = _write_identity(self.data_dir)
        derived = rr.derive_hub_id(self.data_dir / rr.IDENTITY_KEY_FILENAME)
        self.assertEqual(derived, _expected_fingerprint(key))
        # And it is what tls.py itself computes for the same key.
        import tls

        self.assertEqual(derived, tls._identity_fingerprint(key))

    def test_openssl_fallback_matches_cryptography_path(self):
        key = _write_identity(self.data_dir)
        pem_path = self.data_dir / rr.IDENTITY_KEY_FILENAME
        try:
            spki = rr._spki_der_via_openssl(pem_path)
        except rr.ProvisioningError as err:
            self.skipTest(f"openssl unavailable: {err}")
        import hashlib

        self.assertEqual(
            hashlib.sha256(spki).hexdigest(), _expected_fingerprint(key)
        )

    def test_missing_pem_is_a_first_boot_error(self):
        with self.assertRaises(rr.ProvisioningError) as ctx:
            rr.derive_hub_id(self.data_dir / rr.IDENTITY_KEY_FILENAME)
        self.assertIn("first boot", str(ctx.exception))


class PushPublicKeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _config(self):
        return rr._load_hub_config(self.data_dir)

    def test_derived_from_key_file_matches_push_signer(self):
        expected = _write_push_key(self.data_dir, mirror=False)
        got = rr.read_push_public_key(self.data_dir, self._config())
        self.assertEqual(got, expected)
        # Cross-check against the hub's own signer class.
        from casasmart import push_crypto

        raw = (self.data_dir / rr.PUSH_IDENTITY_KEY_FILENAME).read_bytes()
        signer = push_crypto.PushSigner(Ed25519PrivateKey.from_private_bytes(raw))
        self.assertEqual(got, signer.public_key_hex)

    def test_mirror_fallback_when_key_file_absent(self):
        mirror_hex = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        (self.data_dir / rr.HUB_CONFIG_FILENAME).write_text(
            json.dumps({rr.PUSH_PUBLIC_KEY_CONFIG_KEY: mirror_hex})
        )
        self.assertEqual(
            rr.read_push_public_key(self.data_dir, self._config()), mirror_hex
        )

    def test_stale_mirror_refuses_to_register(self):
        _write_push_key(self.data_dir, mirror=False)
        other = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        (self.data_dir / rr.HUB_CONFIG_FILENAME).write_text(
            json.dumps({rr.PUSH_PUBLIC_KEY_CONFIG_KEY: other})
        )
        with self.assertRaises(rr.ProvisioningError) as ctx:
            rr.read_push_public_key(self.data_dir, self._config())
        self.assertIn("disagree", str(ctx.exception))

    def test_no_key_anywhere_is_a_first_boot_error(self):
        with self.assertRaises(rr.ProvisioningError) as ctx:
            rr.read_push_public_key(self.data_dir, self._config())
        self.assertIn("first boot", str(ctx.exception))

    def test_corrupt_key_file_rejected(self):
        (self.data_dir / rr.PUSH_IDENTITY_KEY_FILENAME).write_bytes(b"short")
        with self.assertRaises(rr.ProvisioningError) as ctx:
            rr.read_push_public_key(self.data_dir, self._config())
        self.assertIn("corrupt", str(ctx.exception))


class DataDirAndRelayResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_accepts_config_root_or_casasmart_dir(self):
        nested = self.root / rr.DATA_DIR_NAME
        nested.mkdir()
        _write_identity(nested)
        self.assertEqual(rr.resolve_data_dir(str(self.root)), nested)
        self.assertEqual(rr.resolve_data_dir(str(nested)), nested)

    def test_relay_precedence_arg_over_config_over_default(self):
        config = {rr.PUSH_RELAY_URL_CONFIG_KEY: "https://fleet.example.com/"}
        self.assertEqual(
            rr.resolve_relay_url("https://cli.example.com", config),
            "https://cli.example.com",
        )
        self.assertEqual(
            rr.resolve_relay_url(None, config), "https://fleet.example.com"
        )
        self.assertEqual(rr.resolve_relay_url(None, {}), rr.DEFAULT_RELAY_URL)

    def test_non_http_relay_rejected(self):
        with self.assertRaises(rr.ProvisioningError):
            rr.resolve_relay_url("ftp://nope", {})


class _CapturingHandler(BaseHTTPRequestHandler):
    """Stand-in relay endpoint: records the request, replies as configured."""

    captured: dict = {}
    reply_status = 200
    reply_body = {"registered": True, "hub_id": "x", "registered_at": 1234}

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", 0))
        type(self).captured = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(length)),
        }
        payload = json.dumps(type(self).reply_body).encode()
        self.send_response(type(self).reply_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output clean
        pass


class WireSeamTests(unittest.TestCase):
    """main() against a real local HTTP server — the full operator flow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.identity = _write_identity(self.data_dir)
        self.public_hex = _write_push_key(self.data_dir)

        self.server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _CapturingHandler.captured = {}
        _CapturingHandler.reply_status = 200

    def _argv(self, *extra):
        return [
            "--data-dir",
            str(self.data_dir),
            "--relay",
            f"http://127.0.0.1:{self.port}",
            *extra,
        ]

    def test_successful_registration_wire_contract(self):
        code, out, _ = _run_main(
            self._argv(), env={"RELAY_ADMIN_TOKEN": "test-token-123"}
        )
        self.assertEqual(code, rr.EXIT_OK, out)
        sent = _CapturingHandler.captured
        self.assertEqual(sent["path"], "/admin/register-hub")
        self.assertEqual(sent["authorization"], "Bearer test-token-123")
        self.assertEqual(sent["content_type"], "application/json")
        self.assertEqual(
            sent["body"],
            {
                "hub_id": _expected_fingerprint(self.identity),
                "public_key": self.public_hex,
            },
        )
        self.assertIn("Registered hub", out)

    def test_admin_token_arg_beats_env(self):
        code, _, _ = _run_main(
            self._argv("--admin-token", "arg-token"),
            env={"RELAY_ADMIN_TOKEN": "env-token"},
        )
        self.assertEqual(code, rr.EXIT_OK)
        self.assertEqual(
            _CapturingHandler.captured["authorization"], "Bearer arg-token"
        )

    def test_rejected_token_maps_to_exit_4(self):
        _CapturingHandler.reply_status = 401
        code, _, err = _run_main(self._argv(), env={"ADMIN_TOKEN": "wrong"})
        self.assertEqual(code, rr.EXIT_TOKEN_REJECTED)
        self.assertIn("admin token", err)

    def test_relay_400_maps_to_exit_5(self):
        _CapturingHandler.reply_status = 400
        _CapturingHandler.reply_body = {"error": "validation_failed", "message": "bad"}
        code, _, err = _run_main(self._argv(), env={"ADMIN_TOKEN": "t"})
        self.assertEqual(code, rr.EXIT_RELAY_REJECTED)
        self.assertIn("refused", err)

    def test_missing_token_exit_2_and_never_dials(self):
        _CapturingHandler.captured = {}
        code, _, err = _run_main(self._argv())
        self.assertEqual(code, rr.EXIT_INPUT)
        self.assertIn("RELAY_ADMIN_TOKEN", err)
        self.assertEqual(_CapturingHandler.captured, {})  # no request sent

    def test_unreachable_relay_exit_3(self):
        # A port that was just freed — nothing listens there.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        code, _, err = _run_main(
            [
                "--data-dir",
                str(self.data_dir),
                "--relay",
                f"http://127.0.0.1:{dead_port}",
            ],
            env={"ADMIN_TOKEN": "t"},
        )
        self.assertEqual(code, rr.EXIT_UNREACHABLE)
        self.assertIn("unreachable", err)

    def test_dry_run_needs_no_token_and_never_dials(self):
        _CapturingHandler.captured = {}
        code, out, _ = _run_main(self._argv("--dry-run"))
        self.assertEqual(code, rr.EXIT_OK)
        self.assertIn(_expected_fingerprint(self.identity), out)
        self.assertIn(self.public_hex, out)
        self.assertEqual(_CapturingHandler.captured, {})

    def test_manual_hub_id_and_public_key_override(self):
        hub_id = "ab" * 32
        pubkey = "cd" * 32
        code, _, _ = _run_main(
            [
                "--hub-id",
                hub_id.upper(),  # normalized to lowercase on the wire
                "--public-key",
                pubkey,
                "--relay",
                f"http://127.0.0.1:{self.port}",
            ],
            env={"ADMIN_TOKEN": "t"},
        )
        self.assertEqual(code, rr.EXIT_OK)
        self.assertEqual(
            _CapturingHandler.captured["body"],
            {"hub_id": hub_id, "public_key": pubkey},
        )

    def test_configured_relay_url_used_when_no_arg(self):
        # hub_config carries a push_relay_url override -> registration follows it.
        (self.data_dir / rr.HUB_CONFIG_FILENAME).write_text(
            json.dumps(
                {
                    rr.PUSH_PUBLIC_KEY_CONFIG_KEY: self.public_hex,
                    rr.PUSH_RELAY_URL_CONFIG_KEY: f"http://127.0.0.1:{self.port}",
                }
            )
        )
        code, _, _ = _run_main(
            ["--data-dir", str(self.data_dir)], env={"ADMIN_TOKEN": "t"}
        )
        self.assertEqual(code, rr.EXIT_OK)
        self.assertEqual(
            _CapturingHandler.captured["path"], "/admin/register-hub"
        )


if __name__ == "__main__":
    unittest.main()
