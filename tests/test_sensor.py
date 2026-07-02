"""Tests for GloBird sensor helpers."""
from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
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

homeassistant = types.ModuleType("homeassistant")
components = types.ModuleType("homeassistant.components")
sensor_component = types.ModuleType("homeassistant.components.sensor")
config_entries = types.ModuleType("homeassistant.config_entries")
const = types.ModuleType("homeassistant.const")
core = types.ModuleType("homeassistant.core")
helpers = types.ModuleType("homeassistant.helpers")
entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
event = types.ModuleType("homeassistant.helpers.event")
storage = types.ModuleType("homeassistant.helpers.storage")
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
util = types.ModuleType("homeassistant.util")
dt = types.ModuleType("homeassistant.util.dt")


class SensorDeviceClass:
    """Minimal sensor device classes used by the integration."""

    ENERGY = "energy"
    ENUM = "enum"
    MONETARY = "monetary"
    TEMPERATURE = "temperature"
    TIMESTAMP = "timestamp"


class SensorEntity:
    """Minimal stand-in for Home Assistant's SensorEntity."""


class SensorStateClass:
    """Minimal sensor state classes used by the integration."""

    MEASUREMENT = "measurement"
    TOTAL = "total"


class CoordinatorEntity:
    """Minimal stand-in for Home Assistant's CoordinatorEntity."""

    def __class_getitem__(cls, _item: Any) -> type["CoordinatorEntity"]:
        return cls

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass = object()
        self._remove_callbacks: list[Any] = []
        self._write_count = 0

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, callback: Any) -> None:
        self._remove_callbacks.append(callback)

    def async_write_ha_state(self) -> None:
        self._write_count += 1


class DataUpdateCoordinator:
    """Minimal stand-in for Home Assistant's DataUpdateCoordinator."""

    def __class_getitem__(cls, _item: Any) -> type["DataUpdateCoordinator"]:
        return cls

    def __init__(
        self,
        *_args: Any,
        update_interval: timedelta | None = None,
        **_kwargs: Any,
    ) -> None:
        self.update_interval = update_interval


class Store:
    """Minimal stand-in for Home Assistant storage."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class UpdateFailed(Exception):
    """Minimal stand-in for Home Assistant's update failure."""


def async_track_point_in_time(*_args: Any, **_kwargs: Any) -> Any:
    """Return an unsubscribe callback."""
    return lambda: None


sensor_component.SensorDeviceClass = SensorDeviceClass
sensor_component.SensorEntity = SensorEntity
sensor_component.SensorStateClass = SensorStateClass
config_entries.ConfigEntry = object
const.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
const.UnitOfTemperature = types.SimpleNamespace(CELSIUS="C")
core.HomeAssistant = object
core.callback = lambda func: func
entity_platform.AddEntitiesCallback = object
event.async_track_point_in_time = async_track_point_in_time
storage.Store = Store
update_coordinator.CoordinatorEntity = CoordinatorEntity
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed
dt.now = lambda: datetime.now(timezone.utc)
util.dt = dt

components.sensor = sensor_component
helpers.entity_platform = entity_platform
helpers.event = event
helpers.storage = storage
helpers.update_coordinator = update_coordinator
homeassistant.components = components
homeassistant.config_entries = config_entries
homeassistant.const = const
homeassistant.core = core
homeassistant.helpers = helpers
homeassistant.util = util

sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.components"] = components
sys.modules["homeassistant.components.sensor"] = sensor_component
sys.modules["homeassistant.config_entries"] = config_entries
sys.modules["homeassistant.const"] = const
sys.modules["homeassistant.core"] = core
sys.modules["homeassistant.helpers"] = helpers
sys.modules["homeassistant.helpers.entity_platform"] = entity_platform
sys.modules["homeassistant.helpers.event"] = event
sys.modules["homeassistant.helpers.storage"] = storage
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
sys.modules["homeassistant.util"] = util
sys.modules["homeassistant.util.dt"] = dt

sensor = importlib.import_module("custom_components.globird_ha.sensor")


def test_expected_monthly_cost_uses_existing_mdi_icon() -> None:
    """Expected Monthly Cost should not point at a non-existent MDI icon."""
    assert sensor.GloBirdExpectedMonthlyCostSensor.icon == "mdi:cash-clock"


def test_billing_period_days_uses_home_assistant_local_date(monkeypatch: Any) -> None:
    """Billing Period Days should follow the configured HA timezone."""
    data = {
        "dashboard": {
            "data": {
                "lastestInvoice": {
                    "issuedDate": "2026-07-02T00:00:00",
                },
            },
        },
    }
    local_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        sensor.dt_util,
        "now",
        lambda: datetime(2026, 7, 2, 0, 30, tzinfo=local_tz),
    )

    assert sensor._billing_period_completed_days(data) == 0

    monkeypatch.setattr(
        sensor.dt_util,
        "now",
        lambda: datetime(2026, 7, 3, 0, 30, tzinfo=local_tz),
    )

    assert sensor._billing_period_completed_days(data) == 1


def test_zerohero_status_reports_today_result_only_when_result_is_for_today() -> None:
    """Achieved/missed should only be used for the current local day."""
    local_tz = timezone(timedelta(hours=10))
    yesterday_summary = {
        "latest_day": "2026/07/01",
        "latest_day_zerohero_credit": 0.0,
        "latest_day_zerohero_achieved": False,
    }

    assert (
        sensor._zerohero_status(
            yesterday_summary,
            datetime(2026, 7, 2, 20, 59, tzinfo=local_tz),
        )
        == "pending"
    )
    assert (
        sensor._zerohero_status(
            yesterday_summary,
            datetime(2026, 7, 2, 21, 0, tzinfo=local_tz),
        )
        == "awaiting_result"
    )

    today_summary = {
        "latest_day": "2026/07/02",
        "latest_day_zerohero_credit": 0.0,
        "latest_day_zerohero_achieved": False,
    }

    assert (
        sensor._zerohero_status(
            today_summary,
            datetime(2026, 7, 2, 21, 30, tzinfo=local_tz),
        )
        == "missed"
    )

    today_summary["latest_day_zerohero_credit"] = -0.3
    today_summary["latest_day_zerohero_achieved"] = True

    assert (
        sensor._zerohero_status(
            today_summary,
            datetime(2026, 7, 2, 21, 30, tzinfo=local_tz),
        )
        == "achieved"
    )


def test_zerohero_status_is_unknown_without_usable_cost_summary() -> None:
    """Missing latest complete cost data should remain unknown."""
    assert sensor._zerohero_status({}) == "unknown"
