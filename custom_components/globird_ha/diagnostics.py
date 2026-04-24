"""Diagnostics support for GloBird HA."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import redact_sensitive
from .const import CONF_PASSWORD, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = coordinator.data if coordinator else None

    config = dict(entry.data)
    if CONF_PASSWORD in config:
        config[CONF_PASSWORD] = "**REDACTED**"

    return {
        "entry": redact_sensitive(config),
        "data": redact_sensitive(data),
        "last_login_debug": coordinator.client.last_login_debug if coordinator else {},
    }
