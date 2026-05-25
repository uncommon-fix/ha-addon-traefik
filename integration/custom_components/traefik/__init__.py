"""Traefik reachability integration — bundled with the traefik supervisor add-on.

Publishes one binary_sensor per Traefik route showing whether the backend is
reachable. Polls the addon's /traefik-api/http/services proxy every 30s and
keys off Traefik's serverStatus field (populated when the addon renders a
healthCheck block per service).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ISSUE_RESTART_REQUIRED, write_loaded_content_hash
from .coordinator import TraefikCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


@dataclass
class TraefikData:
    coordinator: TraefikCoordinator
    known_slugs: set[str] = field(default_factory=set)
    add_entities_cb: AddEntitiesCallback | None = None


type TraefikConfigEntry = ConfigEntry[TraefikData]


async def async_setup_entry(hass: HomeAssistant, entry: TraefikConfigEntry) -> bool:
    coordinator = TraefikCoordinator(hass, entry)
    # Assign runtime_data BEFORE first_refresh: first_refresh schedules the
    # add-only reconcile task, which reads entry.runtime_data — setting it first
    # avoids an AttributeError race on startup. add_entities_cb stays None until
    # the platform sets it, so reconcile correctly no-ops during first refresh.
    entry.runtime_data = TraefikData(coordinator=coordinator)
    # Snapshot which integration content this process loaded and persist it so
    # the add-on banner can detect a newer deployed version. first_refresh then
    # runs the restart-required check (raises the Repairs issue if they differ).
    await hass.async_add_executor_job(write_loaded_content_hash)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TraefikConfigEntry) -> bool:
    ir.async_delete_issue(hass, DOMAIN, ISSUE_RESTART_REQUIRED)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TraefikConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a route's device from the UI.

    Returns True so HA always permits removal — a stale "Traefik route:
    <slug>" device (e.g. a route deleted while HA was down) can be cleaned up
    by the user. A still-live route re-materialises its device on the next
    coordinator cycle anyway.
    """
    return True
