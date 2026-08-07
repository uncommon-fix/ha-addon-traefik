# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/__init__.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""addon-kit -- the shared add-on lifecycle wrapper.

Added with `app.py`, for two reasons that only appear once the kit is vendored:

  * **It makes `addonkit` a real package.** Without this file it is a PEP 420
    namespace package, so a second `addonkit` directory anywhere on `sys.path`
    would MERGE with this one and the import would silently be half of each.
    The kit is copied into three images by `tools/sync-shared.ps1`; that is
    exactly the situation where a merge would happen and be baffling.
  * **`addonkit.KitError` is the name house rule 5 uses.** The rule tells an
    add-on to catch one type; it should not have to know which module defines
    it.

`AddonKit` is exposed lazily. Importing it pulls in `aiohttp`, and the one
consumer that has no web app -- the `export`/`restore` CLI running inside a
10-second container shutdown -- should not pay for it or depend on it.
"""

from __future__ import annotations

from typing import Any

from .errors import GateError, KitError, SettingsError, SetupError

__all__ = [
    "AddonKit",
    "TraefikRoute",
    "complete_setup",
    "pending_is_meaningful",
    "KitError",
    "SettingsError",
    "SetupError",
    "GateError",
]


def __getattr__(name: str) -> Any:
    if name in ("AddonKit", "TraefikRoute"):
        from . import app

        return getattr(app, name)
    # Lazy for a smaller reason than aiohttp: `setup` pulls in settings, views
    # and ingress, and the export/restore CLI needs none of them.
    if name in ("complete_setup", "pending_is_meaningful"):
        from . import setup

        return getattr(setup, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
