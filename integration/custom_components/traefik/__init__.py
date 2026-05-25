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
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = TraefikData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TraefikConfigEntry) -> bool:
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
