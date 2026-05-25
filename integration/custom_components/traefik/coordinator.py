"""DataUpdateCoordinator polling the addon's /traefik-api/http/services proxy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_PATH,
    CONF_API_URL,
    DOMAIN,
    INTERNAL_SUFFIX,
    ISSUE_RESTART_REQUIRED,
    UPDATE_INTERVAL,
    get_loaded_content_hash,
    read_content_hash,
)

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
        # Last-known restart-required state; None until first evaluated. Used to
        # log + touch the issue registry only on transitions, not every poll.
        self._restart_required: bool | None = None

    async def async_check_restart_required(self) -> None:
        """Raise/clear the HA Repairs issue when the deployed integration
        content differs from what this process loaded. Independent of Traefik
        reachability so it still runs when the backend is down. Best-effort: a
        failure here must never break the reachability poll.
        """
        try:
            loaded = await self.hass.async_add_executor_job(get_loaded_content_hash)
            deployed = await self.hass.async_add_executor_job(read_content_hash)
        except Exception as err:  # noqa: BLE001 - never let this break the poll
            _LOGGER.debug("restart-required check skipped: %s", err)
            return
        pending = bool(deployed) and deployed != loaded
        if pending == self._restart_required:
            return  # no transition
        self._restart_required = pending
        if pending:
            _LOGGER.info(
                "Traefik integration updated on disk (deployed=%s, loaded=%s); "
                "restart Home Assistant to load it",
                deployed,
                loaded,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_RESTART_REQUIRED,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_RESTART_REQUIRED,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_RESTART_REQUIRED)

    async def _async_update_data(self) -> dict[str, ServiceState]:
        # Runs first, before the Traefik call, so the restart-required signal is
        # evaluated every cycle even when Traefik is unreachable.
        await self.async_check_restart_required()
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

        # Inline-await (NOT fire-and-forget): async_create_task detaches the
        # reconcile so exceptions vanish into the default handler and ordering vs
        # the next poll isn't guaranteed. Awaiting surfaces failures and keeps
        # entity adds ordered with the data they're derived from.
        await self._reconcile_entities(result)
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
