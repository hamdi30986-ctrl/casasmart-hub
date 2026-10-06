"""Stub ``homeassistant.components`` package.

Being a real package is the whole point: ``auth_api.py`` does
``from homeassistant.components import persistent_notification`` — against
the old bare-ModuleType stubs that raised (ModuleNotFoundError standalone,
ImportError under discover) and killed every dispatch that lazily imported
``auth_api``. Only ``persistent_notification`` / ``http`` /
``alarm_control_panel`` live here; anything else (zeroconf, recorder,
camera, ...) stays unimportable so container-only suites keep skipping.
"""
