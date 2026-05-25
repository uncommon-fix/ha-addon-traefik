"""Single-step config flow for the Traefik reachability integration."""

from __future__ import annotations

import os
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_API_URL, DEFAULT_API_URL, DOMAIN, VERSION_PATH

# The add-on's cont-init step drops a `.api_url` file next to this component
# carrying the add-on's own resolvable hostname (correct for both local and
# store installs). Use it as the default when present; otherwise fall back.
_API_URL_HINT = os.path.join(os.path.dirname(__file__), ".api_url")


def _read_api_url_default() -> str:
    try:
        with open(_API_URL_HINT, encoding="utf-8") as fh:
            url = fh.read().strip()
        if url:
            return url
    except OSError:
        pass
    return DEFAULT_API_URL


class TraefikConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_API_URL].rstrip("/")
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    url + VERSION_PATH,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        errors["base"] = "cannot_connect"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(
                    title="Traefik", data={CONF_API_URL: url}
                )

        default_url = await self.hass.async_add_executor_job(_read_api_url_default)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_URL, default=default_url): str}
            ),
            errors=errors,
        )
