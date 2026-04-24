"""Config flow for GloBird HA."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import GloBirdAuthError, GloBirdCaptchaRequired, GloBirdClient
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)


class GloBirdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GloBird HA."""

    VERSION = 1

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

