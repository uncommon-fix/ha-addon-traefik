"""Constants for the Traefik reachability integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "traefik"

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
