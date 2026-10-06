"""Shared Home Assistant stubs for the unit suites (the ONE installer).

Why this exists: the suites used to install per-suite ``types.ModuleType``
shims into ``sys.modules`` additively, each providing only what IT needed.
Cross-suite (``unittest discover``) the first shim won and siblings ran
against a stub missing their symbols — e.g. the push dispatcher's lazy
``from .auth_api import get_engine`` hits
``from homeassistant.components import persistent_notification``, which can
only ever work when ``homeassistant`` is a real *package*, not a bare module.

So ``tests/hastubs/homeassistant/`` is a real stub PACKAGE (directories +
``__init__.py`` files) whose content is the superset every suite needs, and
every suite installs it through :func:`install_homeassistant_stubs`. The
content is identical no matter which suite installs it first, so suites are
order-independent — runnable standalone, in any combination, and under
``python3 -m unittest discover -s tests``.

Deliberately NOT stubbed: ``homeassistant.exceptions``, the helpers
registries (``area_registry`` / ``device_registry`` / ``entity_registry``),
``util.yaml``, recorder, etc. The view-layer suites (``test_registry_api``,
``test_settings_api``, ...) exercise real Home Assistant in the hub
container and must keep SKIPPING in a stubbed environment — their guarded
imports still fail cleanly on those missing submodules.

Usage (top of a suite, before any ``casasmart``/``homeassistant`` import):

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from hastubs import install_casasmart_package, install_homeassistant_stubs

    install_homeassistant_stubs()
    install_casasmart_package()
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # tests/hastubs
_CASASMART_PKG = _HERE.parent.parent / "custom_components" / "casasmart"

# Every stub module, imported eagerly so later imports (including lazy
# in-function ones like the dispatcher's ``async_track_time_interval``)
# resolve from sys.modules without hastubs needing to stay on sys.path.
_HA_MODULES = (
    "homeassistant",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.event",
    "homeassistant.components",
    "homeassistant.components.http",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.alarm_control_panel",
)


def install_homeassistant_stubs() -> None:
    """Idempotent: put the shared ``homeassistant`` stub package in sys.modules.

    First caller wins; since every suite installs the SAME package this is
    order-independent. Where the real Home Assistant was already imported
    (view-layer runs inside the hub container) this is a no-op.
    """
    if "homeassistant" in sys.modules:
        return
    sys.path.insert(0, str(_HERE))
    try:
        for name in _HA_MODULES:
            importlib.import_module(name)
    finally:
        try:
            sys.path.remove(str(_HERE))
        except ValueError:
            pass


def install_casasmart_package() -> None:
    """Register the stub ``casasmart`` package.

    ``__path__`` points at the source dir so submodules load and
    package-relative imports (``from .alarm import ...``) resolve WITHOUT
    running the real ``__init__.py`` (it pulls in the full HA runtime).
    """
    if "casasmart" in sys.modules:
        return
    pkg = types.ModuleType("casasmart")
    pkg.__path__ = [str(_CASASMART_PKG)]
    sys.modules["casasmart"] = pkg
