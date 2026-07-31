"""Core stand-ins: Event / HomeAssistant / State / callback."""

from __future__ import annotations


class Event:
    """Only ``.data`` is touched by the modules under test.

    The signature MIRRORS the real ``homeassistant.core.Event``
    (``event_type`` first, ``data`` second) on purpose: the suites that
    build events run against this stub locally AND against real Home
    Assistant in the hub container. When this took ``data`` as its first
    argument, every such suite silently constructed an Event whose
    event_type was the data dict and whose data was None the moment a real
    HA was present — the handler then saw no entity_id and dispatched
    nothing, so the assertions failed with an empty call list.
    """

    def __init__(self, event_type=None, data=None) -> None:
        self.event_type = event_type
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
