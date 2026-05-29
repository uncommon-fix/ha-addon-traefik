"""binary_sensor.traefik_route_<slug>_reachable per Traefik service."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TraefikConfigEntry
from .const import DOMAIN
from .coordinator import TraefikCoordinator

# Coordinator owns polling; entities don't fetch.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TraefikConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = entry.runtime_data
    initial_slugs = sorted(data.coordinator.data or {})
    # Populate known_slugs BEFORE wiring add_entities_cb. The coordinator's
    # add-only reconcile reads (known_slugs, add_entities_cb) together — if a
    # poll lands between assigning the callback and updating known_slugs, it
    # would re-add the same slugs and HA logs duplicate-unique_id warnings.
    data.known_slugs.update(initial_slugs)
    data.add_entities_cb = async_add_entities
    if initial_slugs:
        async_add_entities(
            TraefikRouteReachable(data.coordinator, slug) for slug in initial_slugs
        )


class TraefikRouteReachable(CoordinatorEntity[TraefikCoordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_name = "Reachable"

    def __init__(self, coordinator: TraefikCoordinator, slug: str) -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._attr_unique_id = f"traefik_route_{slug}_reachable"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, slug)},
            name=f"Traefik route: {slug}",
            manufacturer="Traefik",
            model="HTTP route",
        )

    @property
    def _state(self):
        return (self.coordinator.data or {}).get(self._slug)

    @property
    def available(self) -> bool:
        state = self._state
        return super().available and state is not None and state.reachable is not None

    @property
    def is_on(self) -> bool | None:
        state = self._state
        return None if state is None else state.reachable

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        state = self._state
        if state is None:
            return None
        return {
            "traefik_status": state.status,
            "server_status": state.server_status,
        }
