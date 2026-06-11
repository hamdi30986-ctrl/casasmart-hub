"""Unit tests for the installer-surface helpers (B16 3c-4b).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from installer import (  # noqa: E402
    ALLOWED_FLOW_HANDLERS,
    DEFAULT_PERMIT_JOIN_SECONDS,
    MAX_PERMIT_JOIN_SECONDS,
    MIN_PERMIT_JOIN_SECONDS,
    PERMIT_JOIN_TOPIC,
    InstallerError,
    filter_state_attributes,
    parse_entity_patch,
    parse_permit_join,
    parse_remote_command,
    permit_join_payload,
    serialize_flow_result,
    serialize_progress_flow,
)


class TestPermitJoin(unittest.TestCase):
    def test_topic_matches_what_the_app_published(self):
        self.assertEqual(
            PERMIT_JOIN_TOPIC, "zigbee2mqtt/bridge/request/permit_join"
        )

    def test_enable_with_default_duration(self):
        self.assertEqual(
            parse_permit_join({"enable": True}),
            (True, DEFAULT_PERMIT_JOIN_SECONDS),
        )

    def test_enable_with_explicit_duration(self):
        self.assertEqual(
            parse_permit_join({"enable": True, "duration": 60}), (True, 60)
        )

    def test_disable(self):
        enable, _ = parse_permit_join({"enable": False})
        self.assertFalse(enable)

    def test_enable_must_be_bool(self):
        for bad in ("true", 1, None, {}):
            with self.assertRaises(InstallerError):
                parse_permit_join({"enable": bad})
        with self.assertRaises(InstallerError):
            parse_permit_join({})

    def test_duration_bounds(self):
        for bad in (
            MIN_PERMIT_JOIN_SECONDS - 1,
            MAX_PERMIT_JOIN_SECONDS + 1,
            0,
            -5,
        ):
            with self.assertRaises(InstallerError):
                parse_permit_join({"enable": True, "duration": bad})
        # Boundary values pass.
        parse_permit_join({"enable": True, "duration": MIN_PERMIT_JOIN_SECONDS})
        parse_permit_join({"enable": True, "duration": MAX_PERMIT_JOIN_SECONDS})

    def test_duration_must_be_int(self):
        for bad in ("60", 60.5, True, None, []):
            with self.assertRaises(InstallerError):
                parse_permit_join({"enable": True, "duration": bad})

    def test_payload_shapes_match_z2m_contract(self):
        self.assertEqual(
            json.loads(permit_join_payload(True, 120)),
            {"value": True, "time": 120},
        )
        # Disable never carries a time — exactly what the app sent.
        self.assertEqual(
            json.loads(permit_join_payload(False, 120)), {"value": False}
        )


class TestEntityPatch(unittest.TestCase):
    def test_rename(self):
        self.assertEqual(
            parse_entity_patch({"name": "Kitchen Right"}),
            {"name": "Kitchen Right"},
        )

    def test_rename_to_null_clears(self):
        self.assertEqual(parse_entity_patch({"name": None}), {"name": None})

    def test_domain_swap(self):
        self.assertEqual(
            parse_entity_patch({"options_domain": "light", "options": {}}),
            {"options_domain": "light", "options": {}},
        )

    def test_domain_swap_options_default_to_empty(self):
        self.assertEqual(
            parse_entity_patch({"options_domain": "fan"}),
            {"options_domain": "fan", "options": {}},
        )

    def test_rename_and_swap_together(self):
        changes = parse_entity_patch(
            {"name": "Fan", "options_domain": "fan", "options": {}}
        )
        self.assertEqual(changes["name"], "Fan")
        self.assertEqual(changes["options_domain"], "fan")

    def test_unknown_fields_rejected(self):
        with self.assertRaises(InstallerError):
            parse_entity_patch({"hidden_by": "user"})
        with self.assertRaises(InstallerError):
            parse_entity_patch({"name": "x", "disabled_by": None})

    def test_options_without_domain_rejected(self):
        with self.assertRaises(InstallerError):
            parse_entity_patch({"options": {}})

    def test_bad_values_rejected(self):
        with self.assertRaises(InstallerError):
            parse_entity_patch({"name": 42})
        with self.assertRaises(InstallerError):
            parse_entity_patch({"options_domain": ""})
        with self.assertRaises(InstallerError):
            parse_entity_patch({"options_domain": "light", "options": []})

    def test_empty_patch_rejected(self):
        with self.assertRaises(InstallerError):
            parse_entity_patch({})


class TestRemoteCommand(unittest.TestCase):
    def test_single_string_becomes_list(self):
        self.assertEqual(
            parse_remote_command(
                {"entity_id": "remote.living_room", "command": "b64:JgBQAA=="}
            ),
            ("remote.living_room", ["b64:JgBQAA=="]),
        )

    def test_list_passes_through(self):
        entity_id, commands = parse_remote_command(
            {"entity_id": "remote.rm4", "command": ["b64:AA==", "b64:BB=="]}
        )
        self.assertEqual(commands, ["b64:AA==", "b64:BB=="])

    def test_only_remote_domain(self):
        for bad in ("light.kitchen", "switch.remote", "remote", "", None, 5):
            with self.assertRaises(InstallerError):
                parse_remote_command({"entity_id": bad, "command": "x"})

    def test_bad_commands_rejected(self):
        for bad in (None, "", [], [""], ["ok", 5], 7):
            with self.assertRaises(InstallerError):
                parse_remote_command(
                    {"entity_id": "remote.rm4", "command": bad}
                )


class TestFlowSerialization(unittest.TestCase):
    def test_whitelist_is_exactly_broadlink_and_easy_ir(self):
        self.assertEqual(ALLOWED_FLOW_HANDLERS, {"broadlink", "easy_ir"})

    def test_form_result_keeps_wizard_keys_drops_internals(self):
        result = {
            "type": "form",
            "flow_id": "abc123",
            "handler": "easy_ir",
            "step_id": "user",
            "data_schema": object(),  # voluptuous — must never cross
            "context": {"source": "user"},
            "errors": {"base": "cannot_connect"},
            "description_placeholders": {"name": "RM4"},
            "last_step": False,
            "preview": object(),
        }
        out = serialize_flow_result(result)
        self.assertEqual(out["type"], "form")
        self.assertEqual(out["flow_id"], "abc123")
        self.assertEqual(out["step_id"], "user")
        self.assertEqual(out["errors"], {"base": "cannot_connect"})
        self.assertNotIn("data_schema", out)
        self.assertNotIn("context", out)
        self.assertNotIn("preview", out)

    def test_create_entry_result(self):
        out = serialize_flow_result(
            {
                "type": "create_entry",
                "flow_id": "abc",
                "handler": "broadlink",
                "title": "Living Room RM4",
                "data": {"host": "192.168.8.50"},  # entry internals
                "result": object(),  # ConfigEntry — not serializable
            }
        )
        self.assertEqual(out["type"], "create_entry")
        self.assertEqual(out["title"], "Living Room RM4")
        self.assertNotIn("data", out)
        self.assertNotIn("result", out)

    def test_abort_result(self):
        out = serialize_flow_result(
            {"type": "abort", "flow_id": "abc", "reason": "already_configured"}
        )
        self.assertEqual(out["reason"], "already_configured")

    def test_type_is_always_a_plain_string(self):
        class FakeEnum:
            def __str__(self):
                return "form"

        out = serialize_flow_result({"type": FakeEnum(), "flow_id": "x"})
        self.assertEqual(out["type"], "form")


class TestProgressFlowSerialization(unittest.TestCase):
    def test_dhcp_discovery_shape(self):
        flow = {
            "flow_id": "f1",
            "handler": "broadlink",
            "step_id": "confirm",
            "context": {
                "source": "dhcp",
                "title_placeholders": {
                    "name": "RM4 pro",
                    "model": "RM4",
                    "host": "192.168.8.50",
                },
                "unique_id": "secret-internal",
            },
        }
        out = serialize_progress_flow(flow)
        self.assertEqual(out["flow_id"], "f1")
        self.assertEqual(out["handler"], "broadlink")
        self.assertEqual(out["context"], {"source": "dhcp"})
        # title_placeholders surface under the key the app already parses.
        self.assertEqual(
            out["description_placeholders"],
            {"name": "RM4 pro", "model": "RM4", "host": "192.168.8.50"},
        )

    def test_missing_context_is_safe(self):
        out = serialize_progress_flow({"flow_id": "f1", "handler": "easy_ir"})
        self.assertEqual(out["context"], {"source": None})
        self.assertNotIn("description_placeholders", out)


class TestStateAttributeFilter(unittest.TestCase):
    def test_token_bearing_keys_stripped(self):
        attrs = {
            "friendly_name": "Front Door",
            "device_class": "motion",
            "entity_picture": "/api/camera_proxy/camera.x?token=SECRET",
            "access_token": "SECRET",
            "token": "SECRET",
        }
        out = filter_state_attributes(attrs)
        self.assertEqual(
            out, {"friendly_name": "Front Door", "device_class": "motion"}
        )
        # Source dict untouched.
        self.assertIn("access_token", attrs)


if __name__ == "__main__":
    unittest.main()
