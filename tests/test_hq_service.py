"""The casasmart.configure_hq_notifications service: who may call it, and how
the trusted key and the optional sender name are stored."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _async_register_services = H.import_integration()._async_register_services
    from casasmart.const import DOMAIN
    from casasmart.hq_notifications import (
        HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY,
        HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY,
    )

    _ERR = None
except Exception as err:
    _ERR = err


class _Auth:
    def __init__(self, users: dict[str, bool]) -> None:
        self._users = users

    async def async_get_user(self, user_id):
        if user_id not in self._users:
            return None
        return types.SimpleNamespace(is_admin=self._users[user_id])


def _public_pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@unittest.skipIf(
    H.IMPORT_ERROR or _ERR, f"CasaSmart unimportable: {H.IMPORT_ERROR or _ERR}"
)
class ConfigureHqNotificationsServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self._tmp.name)
        self.addCleanup(self.runtime.storage.close)
        self.hass.auth = _Auth({"admin": True, "member": False})
        _async_register_services(self.hass)
        self.handler = self.hass.services.handlers[
            (DOMAIN, "configure_hq_notifications")
        ]

    async def _call(self, user_id: str, **data) -> None:
        call = types.SimpleNamespace(
            data=data, context=types.SimpleNamespace(user_id=user_id)
        )
        await self.handler(call)

    def _stored(self, key: str):
        return self.runtime.hub_config.get(key)

    async def test_sender_name_is_stored_with_the_key(self) -> None:
        await self._call("admin", public_key=_public_pem(), sender_name=" Villa HQ ")
        self.assertIn("PUBLIC KEY", self._stored(HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY))
        self.assertEqual(
            self._stored(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY), "Villa HQ"
        )

    async def test_reconfiguring_without_a_name_restores_the_default(self) -> None:
        await self._call("admin", public_key=_public_pem(), sender_name="Villa HQ")
        await self._call("admin", public_key=_public_pem())
        self.assertIsNone(self._stored(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY))

    async def test_invalid_sender_name_changes_nothing(self) -> None:
        with self.assertRaisesRegex(Exception, "sender name"):
            await self._call("admin", public_key=_public_pem(), sender_name="x" * 41)
        self.assertIsNone(self._stored(HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY))
        self.assertIsNone(self._stored(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY))

    async def test_only_home_assistant_admins_may_configure(self) -> None:
        for user_id in ("member", "nobody"):
            with self.assertRaisesRegex(Exception, "admin"):
                await self._call(user_id, public_key=_public_pem(), sender_name="X")
        self.assertIsNone(self._stored(HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY))


if __name__ == "__main__":
    unittest.main()
