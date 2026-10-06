"""Timer/tracker stand-ins — the one stub with test-facing behavior.

``async_call_later`` is CONTROLLABLE: every schedule is recorded into
``calls`` as ``{"delay", "action", "cancelled"}`` and the returned cancel
handle flips ``cancelled``. A test fires the timer by invoking
``calls[i]["action"](None)`` itself. Isolation hook: call :func:`reset` (or
``calls.clear()``) at the top of a test — ``calls`` is cleared in place and
never rebound, so ``import homeassistant.helpers.event as ev`` aliases stay
live across suites.

Suites patch the *binding in the module under test* — e.g.
``mock.patch.object(alarm_adapter, "async_call_later", fake)``. That is now
the rule rather than the exception: a suite leaning on this default only runs
where this stub won, so against real Home Assistant in the hub container the
genuine scheduler got called with a fake hass and raised. Every timer-driving
suite (alarm adapter, alarm panel, push dispatcher, athan) owns its fake; this
default just keeps an unpatched caller from exploding.

The ``async_track_*`` trackers are inert (return a no-op unsubscribe): the
athan suite patches ``A.async_track_point_in_time`` where it wants to
observe arming, and relies on the inert default elsewhere.
"""

from __future__ import annotations

calls: list[dict] = []


def reset() -> None:
    """Per-test isolation: drop every recorded schedule (in place)."""
    calls.clear()


def async_call_later(hass, delay, action):
    rec = {"delay": delay, "action": action, "cancelled": False}
    calls.append(rec)

    def _cancel() -> None:
        rec["cancelled"] = True

    return _cancel


def async_track_point_in_time(hass, action, when):
    return lambda: None


def async_track_time_change(hass, action, **kwargs):
    return lambda: None


def async_track_time_interval(hass, action, interval):
    return lambda: None


def async_track_utc_time_change(hass, action, **kwargs):
    return lambda: None
