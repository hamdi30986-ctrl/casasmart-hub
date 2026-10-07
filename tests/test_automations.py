"""Unit tests for the pure automation-config logic.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from automations import (
    delete_automation,
    get_automation,
    is_casa_automation_key,
    is_valid_casa_automation_key,
    upsert_automation,
    with_singular_keys,
)

CASA_ID = "casa_automation_20260303_143022015"


def sample_yaml():
    """A realistic automations.yaml: one installer item, one casa item."""
    return [
        {"id": "athan_reminder", "alias": "Athan", "trigger": [], "action": []},
        {
            "id": CASA_ID,
            "alias": "casa_automation Morning",
            "trigger": [{"trigger": "time", "at": "07:00:00"}],
            "action": [{"action": "light.turn_on"}],
            "mode": "single",
        },
    ]


class TestIsCasaAutomationKey(unittest.TestCase):
    def test_accepts_app_generated_ids(self):
        self.assertTrue(is_casa_automation_key(CASA_ID))

    def test_rejects_foreign_and_malformed_keys(self):
        for bad in (
            "athan_reminder",  # installer automation
            "casa_automation_",  # bare prefix, no id
            "casa_automation",  # prefix minus underscore
            "",  # empty
            None,  # not a string
            42,  # not a string
        ):
            with self.subTest(bad=bad):
                self.assertFalse(is_casa_automation_key(bad))


class TestIsValidCasaAutomationKey(unittest.TestCase):
    def test_accepts_well_formed_keys(self):
        for good in (
            CASA_ID,  # the app's timestamp ids
            "casa_automation_20260303_143022015",
            "casa_automation_abc123",
            "casa_automation_ABC_123",
            "casa_automation_x",  # single char suffix
        ):
            with self.subTest(good=good):
                self.assertTrue(is_valid_casa_automation_key(good))

    def test_rejects_illegal_charset_after_prefix(self):
        for bad in (
            "casa_automation_foo-bar",  # dash
            "casa_automation_foo.bar",  # dot
            "casa_automation_foo bar",  # space
            "casa_automation_foo/bar",  # slash
            "casa_automation_../etc",  # path traversal
            "casa_automation_foo:bar",  # colon
            "casa_automation_foo\nbar",  # newline
            "casa_automation_café",  # non-ASCII letter
        ):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_casa_automation_key(bad))

    def test_length_is_bounded_by_the_flag_store(self):
        self.assertTrue(is_valid_casa_automation_key("casa_automation_" + "a" * 239))
        self.assertFalse(is_valid_casa_automation_key("casa_automation_" + "a" * 240))

    def test_rejects_what_ownership_gate_already_rejects(self):
        # A malformed/foreign key is never "valid" either.
        for bad in (
            "athan_reminder",  # installer automation
            "casa_automation_",  # bare prefix, no id
            "casa_automation",  # prefix minus underscore
            "",  # empty
            None,  # not a string
            42,  # not a string
        ):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_casa_automation_key(bad))


class TestGetAutomation(unittest.TestCase):
    def test_found(self):
        item = get_automation(sample_yaml(), CASA_ID)
        self.assertEqual(item["alias"], "casa_automation Morning")

    def test_missing(self):
        self.assertIsNone(get_automation(sample_yaml(), "casa_automation_nope"))

    def test_matches_non_string_stored_ids(self):
        # Hand-edited yaml can hold a bare numeric id — match via str().
        data = [{"id": 1234, "alias": "x"}]
        self.assertEqual(get_automation(data, "1234"), {"id": 1234, "alias": "x"})


class TestUpsertAutomation(unittest.TestCase):
    def test_update_replaces_in_place(self):
        data = sample_yaml()
        upsert_automation(
            data, CASA_ID, {"alias": "casa_automation New", "mode": "single"}
        )
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1]["alias"], "casa_automation New")
        self.assertEqual(data[1]["id"], CASA_ID)
        # The replace dropped keys absent from the new body (HA semantics).
        self.assertNotIn("trigger", data[1])

    def test_create_appends(self):
        data = sample_yaml()
        upsert_automation(data, "casa_automation_new1", {"alias": "casa_automation X"})
        self.assertEqual(len(data), 3)
        self.assertEqual(data[2]["id"], "casa_automation_new1")

    def test_body_id_cannot_overrule_url_key(self):
        data = []
        upsert_automation(
            data, "casa_automation_real", {"id": "athan_reminder", "alias": "sneaky"}
        )
        self.assertEqual(data[0]["id"], "casa_automation_real")

    def test_untouched_items_survive(self):
        data = sample_yaml()
        upsert_automation(data, CASA_ID, {"alias": "casa_automation New"})
        self.assertEqual(data[0]["id"], "athan_reminder")
        self.assertEqual(data[0]["alias"], "Athan")


class TestDeleteAutomation(unittest.TestCase):
    def test_delete_removes_only_the_target(self):
        data = sample_yaml()
        self.assertTrue(delete_automation(data, CASA_ID))
        self.assertEqual([item["id"] for item in data], ["athan_reminder"])

    def test_delete_missing_is_false_and_harmless(self):
        data = sample_yaml()
        self.assertFalse(delete_automation(data, "casa_automation_nope"))
        self.assertEqual(len(data), 2)


class TestWithSingularKeys(unittest.TestCase):
    """HA's editor saves triggers/conditions/actions; the apps read the
    singular keys."""

    def test_plural_keys_are_renamed_in_place_order(self):
        config = {
            "id": CASA_ID,
            "alias": "Evening",
            "triggers": [{"trigger": "sun", "event": "sunset"}],
            "conditions": [{"condition": "state", "entity_id": "binary_sensor.door"}],
            "actions": [{"action": "light.turn_on"}],
            "mode": "single",
        }
        self.assertEqual(
            list(with_singular_keys(config).items()),
            [
                ("id", CASA_ID),
                ("alias", "Evening"),
                ("trigger", [{"trigger": "sun", "event": "sunset"}]),
                (
                    "condition",
                    [{"condition": "state", "entity_id": "binary_sensor.door"}],
                ),
                ("action", [{"action": "light.turn_on"}]),
                ("mode", "single"),
            ],
        )
        self.assertIn("triggers", config)  # the stored config is left alone

    def test_singular_keys_and_a_clashing_plural_are_kept(self):
        config = {"trigger": [], "triggers": [{"trigger": "time"}], "action": []}
        self.assertEqual(with_singular_keys(config), config)


if __name__ == "__main__":
    unittest.main()
