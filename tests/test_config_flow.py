"""Tests for GloBird config flow helpers."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components"
INTEGRATION_PATH = COMPONENT_PATH / "globird_ha"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENT_PATH)]  # type: ignore[attr-defined]
globird_package = types.ModuleType("custom_components.globird_ha")
globird_package.__path__ = [str(INTEGRATION_PATH)]  # type: ignore[attr-defined]
sys.modules["custom_components"] = custom_components
sys.modules["custom_components.globird_ha"] = globird_package

voluptuous = types.ModuleType("voluptuous")
homeassistant = types.ModuleType("homeassistant")
config_entries = types.ModuleType("homeassistant.config_entries")
data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
helpers = types.ModuleType("homeassistant.helpers")
selector = types.ModuleType("homeassistant.helpers.selector")


class Schema(dict):
    """Minimal schema object that preserves field keys for assertions."""


class Required:
    """Minimal required field marker."""

    def __init__(self, key: str, default: Any | None = None) -> None:
        self.key = key
        self.default = default

    def __repr__(self) -> str:
        return self.key

    def __hash__(self) -> int:
        return hash((self.key, self.default))


class Optional(Required):
    """Minimal optional field marker."""

    def __init__(
        self,
        key: str,
        default: Any | None = None,
        description: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(key, default)
        self.description = description or {}


class _AnySelector:
    """Minimal selector accepting any config."""

    def __init__(self, _config: Any = None) -> None:
        return None


class ConfigFlow:
    """Minimal stand-in for Home Assistant's config flow base."""

    def __init_subclass__(cls, **_kwargs: Any) -> None:
        return None

    async def async_set_unique_id(self, _unique_id: str) -> None:
        return None

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def async_create_entry(self, **kwargs: Any) -> dict[str, Any]:
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
        return {"type": "form", **kwargs}


class OptionsFlow:
    """Minimal stand-in that mirrors HA-owned config_entry access."""

    def async_create_entry(self, **kwargs: Any) -> dict[str, Any]:
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
        return {"type": "form", **kwargs}


class ConfigEntry:
    """Minimal config entry carrying options for the options flow."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}


class TextSelector:
    """Minimal text selector."""

    def __init__(self, _config: Any) -> None:
        return None


class TimeSelector:
    """Minimal time selector."""


config_entries.ConfigEntry = ConfigEntry
config_entries.ConfigFlow = ConfigFlow
config_entries.OptionsFlow = OptionsFlow
data_entry_flow.FlowResult = dict[str, Any]
voluptuous.Required = Required
voluptuous.Optional = Optional
voluptuous.Schema = Schema
selector.TextSelector = TextSelector
selector.TextSelectorConfig = lambda **kwargs: kwargs
selector.TextSelectorType = types.SimpleNamespace(PASSWORD="password")
selector.TimeSelector = TimeSelector
selector.EntitySelector = _AnySelector
selector.EntitySelectorConfig = lambda **kwargs: kwargs
selector.NumberSelector = _AnySelector
selector.NumberSelectorConfig = lambda **kwargs: kwargs
selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
selector.ObjectSelector = _AnySelector
helpers.selector = selector
homeassistant.config_entries = config_entries
homeassistant.data_entry_flow = data_entry_flow
homeassistant.helpers = helpers

sys.modules["homeassistant"] = homeassistant
sys.modules["voluptuous"] = voluptuous
sys.modules["homeassistant.config_entries"] = config_entries
sys.modules["homeassistant.data_entry_flow"] = data_entry_flow
sys.modules["homeassistant.helpers"] = helpers
sys.modules["homeassistant.helpers.selector"] = selector

config_flow = importlib.import_module("custom_components.globird_ha.config_flow")


def test_options_flow_is_created_without_manual_config_entry_assignment() -> None:
    """Home Assistant owns config_entry on options flows in current core."""
    flow = config_flow.GloBirdConfigFlow.async_get_options_flow(ConfigEntry())

    assert isinstance(flow, config_flow.GloBirdOptionsFlow)
    assert not hasattr(flow, "config_entry")


def test_options_flow_uses_ha_attached_config_entry_for_current_value() -> None:
    """Opening options should render the daily polling start time field."""
    flow = config_flow.GloBirdOptionsFlow()
    flow.config_entry = ConfigEntry({"daily_poll_start_time": "03:00"})

    result = asyncio.run(flow.async_step_init())

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert "daily_poll_start_time" in str(result["data_schema"])


def test_options_flow_saves_daily_poll_start_time() -> None:
    """Submitting options should store the selected daily polling start time."""
    flow = config_flow.GloBirdOptionsFlow()

    result = asyncio.run(flow.async_step_init({"daily_poll_start_time": "03:00"}))

    assert result == {
        "type": "create_entry",
        "title": "",
        "data": {"daily_poll_start_time": "03:00"},
    }


def test_options_flow_shows_tariff_fields() -> None:
    """Options should offer live meters, rate overrides and plan limits."""
    flow = config_flow.GloBirdOptionsFlow()
    flow.config_entry = ConfigEntry()

    result = asyncio.run(flow.async_step_init())
    fields = str(result["data_schema"])

    for key in (
        "grid_import_entity",
        "grid_export_entity",
        "rate_overrides",
        "super_export_cap_kwh",
        "zerohero_kwh_per_hour",
    ):
        assert key in fields


def test_options_flow_saves_rate_overrides_in_cents() -> None:
    """Rate overrides keyed by the GloBird app's charge names are stored as given."""
    flow = config_flow.GloBirdOptionsFlow()
    flow.config_entry = ConfigEntry()
    submitted = {
        "daily_poll_start_time": "00:05",
        "rate_overrides": {"Peak Usage": 47.3, "Daily Charge": 147.4},
        "grid_import_entity": "sensor.grid_import",
    }

    result = asyncio.run(flow.async_step_init(dict(submitted)))

    assert result["type"] == "create_entry"
    assert result["data"] == submitted


def test_options_flow_rejects_non_numeric_rate_overrides() -> None:
    """A rate override that is not a number is reported against the field."""
    flow = config_flow.GloBirdOptionsFlow()
    flow.config_entry = ConfigEntry()

    result = asyncio.run(
        flow.async_step_init(
            {
                "daily_poll_start_time": "00:05",
                "rate_overrides": {"Peak Usage": "about 47"},
            }
        )
    )

    assert result["type"] == "form"
    assert result["errors"] == {"rate_overrides": "invalid_rate_overrides"}


def test_options_flow_drops_empty_rate_overrides() -> None:
    """Clearing the overrides editor removes the option."""
    flow = config_flow.GloBirdOptionsFlow()
    flow.config_entry = ConfigEntry()

    result = asyncio.run(
        flow.async_step_init({"daily_poll_start_time": "00:05", "rate_overrides": {}})
    )

    assert result["data"] == {"daily_poll_start_time": "00:05"}
