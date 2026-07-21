"""Core stand-ins: Event / HomeAssistant / State / callback."""

from __future__ import annotations


class Event:
    """Only ``.data`` is touched by the modules under test."""

    def __init__(self, data=None) -> None:
        self.data = {} if data is None else data


class HomeAssistant:
    """Never instantiated — each suite's FakeHass/_Hass duck-types it."""


class State:
    """Minimal ``hass.states`` record (entity_id / state / attributes)."""

    def __init__(self, entity_id: str, state: str, attributes=None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.domain = entity_id.partition(".")[0]


def callback(fn):
    """The @callback decorator is a no-op marker in HA."""
    return fn
