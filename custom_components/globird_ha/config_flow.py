"""Config flow for GloBird HA."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import (
    GloBirdAuthError,
    GloBirdCaptchaRequired,
    GloBirdClient,
    parse_gas_rate_schedule,
    parse_tou_rate_schedule,
)
from .const import (
    CONF_DAILY_POLL_START_TIME,
    CONF_EMAIL,
    CONF_GAS_RATE_SCHEDULE,
    CONF_PASSWORD,
    CONF_TOU_RATE_SCHEDULE,
    DEFAULT_DAILY_POLL_START_TIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class GloBirdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GloBird HA."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> GloBirdOptionsFlow:
        """Create the options flow."""
        return GloBirdOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            password = str(user_input[CONF_PASSWORD])

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            client = GloBirdClient()
            try:
                await client.authenticate(email, password)
            except GloBirdCaptchaRequired:
                errors["base"] = "captcha_required"
            except GloBirdAuthError:
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001 - HA config flow maps this.
                _LOGGER.exception("Unexpected GloBird setup failure: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    },
                )
            finally:
                await client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )


class GloBirdOptionsFlow(config_entries.OptionsFlow):
    """Handle GloBird HA options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage integration options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parse_tou_rate_schedule(user_input.get(CONF_TOU_RATE_SCHEDULE))
            except (ValueError, TypeError):
                errors["base"] = "invalid_tou_rate_schedule"

            if not errors:
                try:
                    parse_gas_rate_schedule(user_input.get(CONF_GAS_RATE_SCHEDULE))
                except (ValueError, TypeError):
                    errors["base"] = "invalid_gas_rate_schedule"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current_time = self.config_entry.options.get(
            CONF_DAILY_POLL_START_TIME,
            DEFAULT_DAILY_POLL_START_TIME,
        )
        current_tou_schedule = self.config_entry.options.get(CONF_TOU_RATE_SCHEDULE, "")
        current_gas_schedule = self.config_entry.options.get(CONF_GAS_RATE_SCHEDULE, "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DAILY_POLL_START_TIME,
                        default=current_time,
                    ): selector.TimeSelector(),
                    vol.Optional(
                        CONF_TOU_RATE_SCHEDULE,
                        default=current_tou_schedule,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                            multiline=True,
                        )
                    ),
                    vol.Optional(
                        CONF_GAS_RATE_SCHEDULE,
                        default=current_gas_schedule,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                            multiline=True,
                        )
                    ),
                }
            ),
            errors=errors,
        )
