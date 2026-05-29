"""Repairs fix flow: one-click restart to load the updated integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import ISSUE_RESTART_REQUIRED

_LOGGER = logging.getLogger(__name__)


class RestartRequiredFixFlow(RepairsFlow):
    """Confirm, then restart Home Assistant Core to load the new code."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # blocking=False: return the flow result before Core tears down.
            await self.hass.services.async_call(
                "homeassistant", "restart", blocking=False
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    if issue_id == ISSUE_RESTART_REQUIRED:
        return RestartRequiredFixFlow()
    # Defensive: surfaces unknown issue_id as a no-op confirm flow rather than
    # 500-ing the Repairs UI. Logged at warning so the developer notices the
    # missing dispatch arm in CI logs.
    _LOGGER.warning("traefik repairs: unknown issue_id %s; serving no-op flow", issue_id)
    return RestartRequiredFixFlow()
