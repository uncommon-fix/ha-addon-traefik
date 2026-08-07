# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/errors.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Exception hierarchy for the kit.

Every module raises from this one root so an add-on can put a single
`except KitError` around kit calls without importing each module's own type.
Per the contract these are for programmer/config errors only — a best-effort
probe returns a value meaning "unknown", it never raises.
"""

from __future__ import annotations


class KitError(Exception):
    """Base class for every error the kit raises deliberately."""


class SettingsError(KitError):
    """A settings operation could not be completed.

    Raised for a rejected schema, an unreadable/corrupt store file, a
    rollback with no baseline, and — the important one — an `on_apply`
    callback that failed. In that last case the original exception is
    always chained as `__cause__`.
    """


class SetupError(KitError):
    """Onboarding could not be completed.

    Raised for every refusal and every failure of `setup.complete_setup` —
    the add-on was not in `NEEDS_SETUP`, there was nothing to complete with,
    the completion action raised, or it returned while the add-on still
    reports itself unconfigured. The cause is always chained when there was
    one. A `SetupError` always means "still onboarding, draft intact".
    """


class GateError(KitError):
    """A single-editor gate operation was refused."""
