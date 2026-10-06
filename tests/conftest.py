"""Pick the strongest Home Assistant available, before any suite imports one.

``hastubs.install_homeassistant_stubs()`` is "first caller wins": it no-ops
when ``homeassistant`` is already in ``sys.modules``. That was meant to let the
view-layer suites run against real HA in the hub container — but nothing ever
did the pre-import, so the stub won everywhere and those suites skipped in
EVERY environment, including the container. The recorded baseline ("906
passed, 150 expected environment-only skips") therefore never exercised the
audio / registry-write / settings HTTP surfaces at all.

So: if a real Home Assistant is importable, import it here first and the whole
suite runs against it — the view-layer suites included. If it isn't (a plain
dev machine), this is a no-op and the suites fall back to ``tests/hastubs``,
skipping the view layer exactly as before.

Both paths must stay green. The container run is the authoritative one; the
stub run is the fast local check.
"""

from __future__ import annotations


def _prefer_real_homeassistant() -> None:
    try:
        import homeassistant  # noqa: F401
        import homeassistant.exceptions  # noqa: F401
        import homeassistant.helpers.area_registry  # noqa: F401
        import homeassistant.helpers.device_registry  # noqa: F401
        import homeassistant.helpers.entity_registry  # noqa: F401
    except ImportError:
        # No real HA here — the stubs take over on first install_*() call.
        return


_prefer_real_homeassistant()
