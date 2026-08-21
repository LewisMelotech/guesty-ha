"""Config flow for the Guesty integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEBHOOK_BASE_URL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Optional(CONF_WEBHOOK_BASE_URL, default=""): str,
        vol.Optional(
            CONF_UPDATE_INTERVAL_MINUTES, default=DEFAULT_UPDATE_INTERVAL_MINUTES
        ): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)


async def _async_validate_credentials(
    hass: HomeAssistant, client_id: str, client_secret: str
) -> None:
    """Verify the supplied credentials by requesting a real token."""
    session = async_get_clientsession(hass)
    client = GuestyApiClient(session, client_id, client_secret)
    await client.async_validate_credentials()


class GuestyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Guesty."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: collect client_id / client_secret."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client_id = user_input[CONF_CLIENT_ID]
            client_secret = user_input[CONF_CLIENT_SECRET]
            webhook_base_url = user_input.get(CONF_WEBHOOK_BASE_URL, "").strip()
            update_interval_minutes = user_input.get(
                CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
            )

            await self.async_set_unique_id(client_id)
            self._abort_if_unique_id_configured()

            try:
                await _async_validate_credentials(self.hass, client_id, client_secret)
            except GuestyAuthError:
                errors["base"] = "invalid_auth"
            except GuestyApiError:
                errors["base"] = "cannot_connect"
            except HomeAssistantError:
                _LOGGER.exception("Unexpected error validating Guesty credentials")
                errors["base"] = "unknown"
            else:
                options: dict[str, Any] = {
                    CONF_UPDATE_INTERVAL_MINUTES: update_interval_minutes
                }
                if webhook_base_url:
                    options[CONF_WEBHOOK_BASE_URL] = webhook_base_url

                return self.async_create_entry(
                    title="Guesty",
                    data={
                        CONF_CLIENT_ID: client_id,
                        CONF_CLIENT_SECRET: client_secret,
                    },
                    options=options,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> GuestyOptionsFlow:
        """Get the options flow for this handler."""
        return GuestyOptionsFlow()


class GuestyOptionsFlow(config_entries.OptionsFlow):
    """Options for the Guesty integration: webhook base URL and how often

    the reservations/tasks coordinators poll Guesty.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user override the webhook base URL and polling interval.

        The webhook base URL override is needed if Home Assistant sits
        behind a reverse proxy it isn't configured to know about, since
        HA's own external URL detection would otherwise be used and could
        be wrong.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_webhook_base_url = self.config_entry.options.get(
            CONF_WEBHOOK_BASE_URL, ""
        )
        current_update_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WEBHOOK_BASE_URL, default=current_webhook_base_url
                ): str,
                vol.Optional(
                    CONF_UPDATE_INTERVAL_MINUTES, default=current_update_interval
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
