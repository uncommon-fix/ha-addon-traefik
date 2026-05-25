"""DataUpdateCoordinator polling the addon's /traefik-api/http/services proxy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import API_PATH, CONF_API_URL, DOMAIN, INTERNAL_SUFFIX, UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import TraefikData

_LOGGER = logging.getLogger(__name__)


@dataclass
class ServiceState:
    """Per-service reachability snapshot."""

    # None = serverStatus missing (healthCheck hasn't run yet → entity unavailable).
    reachable: bool | None
    status: str
    server_status: dict[str, str]


class TraefikCoordinator(DataUpdateCoordinator[dict[str, ServiceState]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self._session = async_get_clientsession(hass)
        self._url = entry.data[CONF_API_URL].rstrip("/") + API_PATH
        self._entry = entry

    async def _async_update_data(self) -> dict[str, ServiceState]:
        try:
            async with self._session.get(
                self._url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status} from {self._url}")
                services: list[dict[str, Any]] = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"connection error: {err}") from err

        if not isinstance(services, list):
            raise UpdateFailed(f"expected list of services, got {type(services).__name__}")

        result: dict[str, ServiceState] = {}
        for svc in services:
            name = svc.get("name", "")
            if not name or name.endswith(INTERNAL_SUFFIX):
                continue
            slug = name.removesuffix("@file") if name.endswith("@file") else name
            status = svc.get("status", "unknown")
            server_status = svc.get("serverStatus") or {}
            if not server_status:
                reachable: bool | None = None
            else:
                reachable = status == "enabled" and all(
                    v == "UP" for v in server_status.values()
                )
            result[slug] = ServiceState(
                reachable=reachable,
                status=status,
                server_status=server_status,
            )

        self.hass.async_create_task(self._reconcile_entities(result))
        return result

    async def _reconcile_entities(self, latest: dict[str, ServiceState]) -> None:
        # Add-only. We materialise a new entity when a route first appears,
        # but we DON'T remove entities (or devices) when a route disappears.
        #
        # Why non-destructive: deleting an entity would break any dashboard
        # card or automation that references it, and a route can come back
        # (re-enabled, renamed back, addon restarted mid-edit). A vanished
        # route instead degrades to `unavailable` via the entity's
        # `available` property — which reads as "not configured / can't
        # determine", distinct from `off` ("target is down / unreachable").
        # We never want a removed route to look like an unreachable backend.
        #
        # Stale entities/devices for routes the user is truly done with are
        # deletable from the UI (see async_remove_config_entry_device). Slugs
        # stay in known_slugs forever within a session so a returning route
        # resumes its existing entity rather than triggering a duplicate add.
        data: TraefikData = self._entry.runtime_data
        new_slugs = set(latest) - data.known_slugs
        if new_slugs and data.add_entities_cb is not None:
            from .binary_sensor import TraefikRouteReachable

            entities = [TraefikRouteReachable(self, slug) for slug in sorted(new_slugs)]
            data.add_entities_cb(entities)
            data.known_slugs |= new_slugs
