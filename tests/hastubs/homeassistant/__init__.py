"""Stub ``homeassistant`` package for the unit suites.

Installed via ``tests/hastubs`` (``install_homeassistant_stubs()``) — a real
package so ``from homeassistant.components import persistent_notification``
and friends resolve. Content is the superset of what every suite's module
under test imports; see ``tests/hastubs/__init__.py`` for what is
deliberately left out (so the container-only view suites keep skipping).
"""
