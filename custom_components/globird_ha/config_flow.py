"""Config flow for GloBird HA."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import GloBirdAuthError, GloBirdCaptchaRequired, GloBirdClient
from .const import (
    CONF_DAILY_POLL_START_TIME,
    CONF_EMAIL,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_PASSWORD,
    CONF_RATE_OVERRIDES,
    CONF_SUPER_EXPORT_CAP_KWH,
    CONF_ZEROHERO_KWH_PER_HOUR,
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
            overrides = user_input.get(CONF_RATE_OVERRIDES)
            if overrides in (None, "", {}):
                user_input.pop(CONF_RATE_OVERRIDES, None)
            elif not _valid_overrides(overrides):
                errors[CONF_RATE_OVERRIDES] = "invalid_rate_overrides"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        options = dict(self.config_entry.options)
        if user_input is not None:
            options.update(user_input)
        current_time = options.get(
            CONF_DAILY_POLL_START_TIME,
            DEFAULT_DAILY_POLL_START_TIME,
        )
        energy_sensor = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        )
        kwh_box = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=1000,
                step="any",
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="kWh",
            )
        )

        def _suggested(key: str) -> dict[str, Any]:
            value = options.get(key)
            return {"suggested_value": value} if value not in (None, "") else {}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DAILY_POLL_START_TIME,
                        default=current_time,
                    ): selector.TimeSelector(),
                    vol.Optional(
                        CONF_GRID_IMPORT_ENTITY,
                        description=_suggested(CONF_GRID_IMPORT_ENTITY),
                    ): energy_sensor,
                    vol.Optional(
                        CONF_GRID_EXPORT_ENTITY,
                        description=_suggested(CONF_GRID_EXPORT_ENTITY),
                    ): energy_sensor,
                    vol.Optional(
                        CONF_RATE_OVERRIDES,
                        description=_suggested(CONF_RATE_OVERRIDES),
                    ): selector.ObjectSelector(),
                    vol.Optional(
                        CONF_SUPER_EXPORT_CAP_KWH,
                        description=_suggested(CONF_SUPER_EXPORT_CAP_KWH),
                    ): kwh_box,
                    vol.Optional(
                        CONF_ZEROHERO_KWH_PER_HOUR,
                        description=_suggested(CONF_ZEROHERO_KWH_PER_HOUR),
                    ): kwh_box,
                }
            ),
            errors=errors,
        )


def _valid_overrides(value: Any) -> bool:
    """Return whether rate overrides are a mapping of names to numbers (cents)."""
    if not isinstance(value, dict):
        return False
    for key, rate in value.items():
        if not str(key).strip() or isinstance(rate, bool):
            return False
        try:
            float(rate)
        except (TypeError, ValueError):
            return False
    return True
