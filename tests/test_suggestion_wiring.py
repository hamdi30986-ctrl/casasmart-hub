"""Public wiring and notification privacy contracts without booting HA."""

import ast
import unittest
from types import SimpleNamespace

from test_suggestions import ROOT


class WiringTest(unittest.TestCase):
    def test_all_views_registered_and_capability_available(self):
        source = (ROOT / "api.py").read_text()
        for name in (
            "CasaSmartSuggestionsView",
            "CasaSmartSuggestionRulesView",
            "CasaSmartSuggestionPreviewView",
            "CasaSmartSuggestionActionView",
        ):
            self.assertIn(f"{name}(hass)", source)
        setup = (ROOT / "__init__.py").read_text()
        self.assertIn("suggestion_store.recover", setup)
        self.assertIn("await entry.runtime_data.suggestions.start()", setup)
        self.assertIn(
            "entry.async_on_unload(entry.runtime_data.suggestions.stop)", setup
        )
        self.assertIn('storage.table("suggestions_v1").clear()', setup)

    def test_real_websocket_callback_never_transmits_private_event_data(self):
        tree = ast.parse((ROOT / "ws.py").read_text())
        connection = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WsConnection"
        )
        function = next(
            node
            for node in connection.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_on_suggestions_changed"
        )
        function.decorator_list = []
        namespace = {"Event": object}
        exec(  # noqa: S102 -- executes only the repository method under test.
            compile(ast.Module(body=[function], type_ignores=[]), "ws.py", "exec"),
            namespace,
        )
        frames = []
        instance = SimpleNamespace(_subscribed=False, _offer_or_close=frames.append)
        private = SimpleNamespace(data={"member": "secret", "room": "secret"})
        namespace[function.name](instance, private)
        self.assertEqual(frames, [])
        instance._subscribed = True
        namespace[function.name](instance, private)
        self.assertEqual(frames, [{"type": "suggestions_changed", "version": 1}])

    def test_management_permission_excludes_sub_admin_and_user(self):
        tree = ast.parse((ROOT / "auth_engine.py").read_text())
        permissions = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", None) == "PERMISSIONS"
        )
        for key, value in zip(permissions.keys, permissions.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "suggestions.manage":
                self.assertEqual([item.id for item in value.elts], ["ROLE_ADMIN"])
                break
        else:
            self.fail("Missing management permission")
