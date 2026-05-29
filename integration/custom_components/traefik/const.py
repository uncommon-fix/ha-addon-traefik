"""Constants for the Traefik reachability integration."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Final

DOMAIN: Final = "traefik"

# Restart-required signal (drives both the HA Repairs card and the add-on
# banner). The add-on's cont-init writes `.content_hash` (the deployed
# integration content); we snapshot that value once at load and write it to
# `.loaded_content_hash`. Pending == the two differ. Keying on CONTENT (not the
# version string) means add-on-only releases that don't touch the integration
# never falsely demand a restart.
CONTENT_HASH_FILE: Final = Path(__file__).with_name(".content_hash")
LOADED_HASH_FILE: Final = Path(__file__).with_name(".loaded_content_hash")
ISSUE_RESTART_REQUIRED: Final = "restart_required"

_loaded_hash: str | None = None
_loaded_hash_captured: bool = False


def read_content_hash() -> str | None:
    """Read the deployed content hash (blocking; call via the executor)."""
    try:
        value = CONTENT_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def get_loaded_content_hash() -> str | None:
    """Snapshot the on-disk content hash ONCE per process (blocking; executor).

    Frozen after the first call so a config-entry reload (which re-runs setup
    but does NOT re-import this module) keeps the value the running code was
    loaded with; only a real HA restart re-imports and refreshes it.
    """
    global _loaded_hash, _loaded_hash_captured
    if not _loaded_hash_captured:
        _loaded_hash = read_content_hash()
        _loaded_hash_captured = True
    return _loaded_hash


def loaded_content_hash_cached() -> tuple[bool, str | None]:
    """Non-blocking peek at the snapshotted hash.

    Returns (captured, value). When `captured` is True, callers may use
    `value` directly on the event loop without dispatching an executor job
    (saves one per coordinator poll once the first capture has happened).
    When False, the caller must still go through get_loaded_content_hash()
    in an executor to perform the first file read.
    """
    return _loaded_hash_captured, _loaded_hash


def write_loaded_content_hash() -> None:
    """Persist the snapshotted loaded hash so the add-on banner can compare it
    against the deployed `.content_hash` (blocking; call via the executor)."""
    value = get_loaded_content_hash()
    try:
        if value is None:
            LOADED_HASH_FILE.unlink(missing_ok=True)
        else:
            LOADED_HASH_FILE.write_text(value, encoding="utf-8")
    except OSError:
        pass

# Fallback URL for the bundled add-on's backend on the hassio docker bridge.
# This value is only correct for a *locally-installed* add-on (slug
# `local_traefik` → hostname `local-traefik`). A store/repo install gets a
# different hostname, so the add-on's cont-init writes the real resolvable host
# to a `.api_url` file that config_flow prefers over this default. Backend
# listens on 8080 and proxies /traefik-api/* to Traefik's dashboard API.
DEFAULT_API_URL: Final = "http://local-traefik:8080"

# Path on the addon backend that proxies Traefik's services endpoint.
API_PATH: Final = "/traefik-api/http/services"

# Health probe path used by config_flow to verify connectivity.
VERSION_PATH: Final = "/traefik-api/version"

UPDATE_INTERVAL: Final = timedelta(seconds=30)

# Traefik exposes its own internal services (api@internal, dashboard@internal,
# noop@internal) under @internal — those are admin endpoints, not user routes.
INTERNAL_SUFFIX: Final = "@internal"

CONF_API_URL: Final = "api_url"
