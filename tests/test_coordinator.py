"""Tests for GloBird coordinator scheduling helpers."""

from __future__ import annotations

import asyncio
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
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.globird_ha", globird_package)

homeassistant = types.ModuleType("homeassistant")
config_entries = types.ModuleType("homeassistant.config_entries")
core = types.ModuleType("homeassistant.core")
helpers = types.ModuleType("homeassistant.helpers")
storage = types.ModuleType("homeassistant.helpers.storage")
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
util = types.ModuleType("homeassistant.util")
dt = types.ModuleType("homeassistant.util.dt")


class DataUpdateCoordinator:
    """Minimal stand-in for Home Assistant's coordinator base."""

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
        pass


class UpdateFailed(Exception):
    """Minimal stand-in for Home Assistant's update failure."""


config_entries.ConfigEntry = object
core.HomeAssistant = object
storage.Store = Store
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed
dt.now = lambda: datetime.now(timezone.utc)
util.dt = dt
helpers.storage = storage
helpers.update_coordinator = update_coordinator
homeassistant.config_entries = config_entries
homeassistant.core = core
homeassistant.helpers = helpers
homeassistant.util = util

sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.config_entries", config_entries)
sys.modules.setdefault("homeassistant.core", core)
sys.modules.setdefault("homeassistant.helpers", helpers)
sys.modules.setdefault("homeassistant.helpers.storage", storage)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", update_coordinator)
sys.modules.setdefault("homeassistant.util", util)
sys.modules.setdefault("homeassistant.util.dt", dt)

coordinator = importlib.import_module("custom_components.globird_ha.coordinator")


def test_next_ready_poll_interval_targets_configured_daily_start() -> None:
    """Ready data should schedule the next automatic check for the next day."""
    now = datetime(2026, 6, 29, 10, 30, tzinfo=timezone.utc)

    assert coordinator._next_ready_poll_interval(now) == timedelta(
        hours=13,
        minutes=35,
    )
    assert coordinator._next_ready_poll_interval(
        now,
        coordinator._parse_daily_poll_start_time("03:00"),
    ) == timedelta(hours=16, minutes=30)


def test_invalid_daily_poll_start_time_falls_back_to_default() -> None:
    """Invalid stored options should keep the midnight-plus-default behavior."""
    now = datetime(2026, 6, 29, 10, 30, tzinfo=timezone.utc)

    assert coordinator._parse_daily_poll_start_time("25:99").isoformat() == "00:05:00"
    assert coordinator._next_ready_poll_interval(
        now,
        coordinator._parse_daily_poll_start_time("25:99"),
    ) == timedelta(hours=13, minutes=35)


def test_update_interval_slows_only_when_daily_data_is_ready(monkeypatch: Any) -> None:
    """Coordinator returns to normal polling until the latest daily data is ready."""
    now = datetime(2026, 6, 29, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(coordinator.dt_util, "now", lambda: now)

    instance = object.__new__(coordinator.GloBirdCoordinator)
    instance.update_interval = coordinator.ACCOUNT_UPDATE_INTERVAL
    instance.entry = types.SimpleNamespace(options={})

    instance._set_update_interval_for_data(
        {
            "service_data": {
                "svc-1": {
                    "latest_data_status": {
                        "status": "ready",
                        "latest_ready_day": "2026/06/28",
                    },
                },
            },
        }
    )

    assert instance.update_interval == timedelta(hours=13, minutes=35)

    instance._set_update_interval_for_data(
        {
            "service_data": {
                "svc-1": {
                    "latest_data_status": {
                        "status": "waiting_for_cost",
                        "latest_ready_day": "2026/06/27",
                    },
                },
            },
        }
    )

    assert instance.update_interval == coordinator.ACCOUNT_UPDATE_INTERVAL


def test_update_interval_ignores_gas_readiness(monkeypatch: Any) -> None:
    """Gas reads are not daily electricity data and must not prevent slow polling."""
    now = datetime(2026, 6, 29, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(coordinator.dt_util, "now", lambda: now)

    instance = object.__new__(coordinator.GloBirdCoordinator)
    instance.update_interval = coordinator.ACCOUNT_UPDATE_INTERVAL
    instance.entry = types.SimpleNamespace(
        options={coordinator.CONF_DAILY_POLL_START_TIME: "03:00"}
    )
    instance._set_update_interval_for_data(
        {
            "service_data": {
                "power": {
                    "service": {"serviceType": "Power"},
                    "latest_data_status": {
                        "status": "ready",
                        "latest_ready_day": "2026/06/28",
                    },
                },
                "gas": {
                    "service": {"serviceType": "Gas"},
                    "latest_data_status": {
                        "status": "no_data",
                        "latest_ready_day": None,
                    },
                },
            }
        }
    )

    assert instance.update_interval == timedelta(hours=16, minutes=30)

    instance.update_interval = coordinator.ACCOUNT_UPDATE_INTERVAL
    instance._set_update_interval_for_data(
        {
            "service_data": {
                "gas": {
                    "service": {"serviceType": "Gas"},
                    "latest_data_status": {
                        "status": "no_data",
                        "latest_ready_day": None,
                    },
                }
            }
        }
    )

    assert instance.update_interval == timedelta(hours=16, minutes=30)


def test_gas_service_fetches_its_own_meter_and_forces_basic_endpoint() -> None:
    """Mixed accounts must not reuse the primary electricity service's meter."""

    class FakeClient:
        def __init__(self) -> None:
            self.read_meter_ids: list[int] = []
            self.usage_calls: list[dict[str, Any]] = []

        async def get_read_meters(
            self, *, account_service_id: int
        ) -> dict[str, Any]:
            self.read_meter_ids.append(account_service_id)
            return {
                "data": [
                    {
                        "siteIdentifier": "MIRN-GAS",
                        "serialNumber": "gas-meter",
                        "meterReadType": "BASIC",
                        "serialStatus": "Active",
                    }
                ]
            }

        async def get_usage(self, **kwargs: Any) -> dict[str, Any]:
            self.usage_calls.append(kwargs)
            return {"data": {}, "success": True}

        async def get_cost_detail(self, **_kwargs: Any) -> dict[str, Any]:
            return {"data": [], "success": True}

    instance = object.__new__(coordinator.GloBirdCoordinator)
    instance.client = FakeClient()
    result = asyncio.run(
        instance._fetch_service_detail(
            {
                "accountServiceId": 11,
                "siteIdentifier": "MIRN-GAS",
                "serviceType": "Gas",
            },
            {
                "data": [
                    {
                        "siteIdentifier": "NMI-POWER",
                        "serialNumber": "power-meter",
                        "meterReadType": "SMART",
                        "serialStatus": "Active",
                    }
                ]
            },
            None,
            {},
        )
    )

    assert instance.client.read_meter_ids == [11]
    assert instance.client.usage_calls[0]["serial_number"] == "gas-meter"
    assert instance.client.usage_calls[0]["is_smart"] is False
    assert result["meter"]["serialNumber"] == "gas-meter"


def test_expected_optional_fetch_failure_classification() -> None:
    """Known AccountServiceStatus endpoint failures should be treated as expected."""
    assert (
        coordinator._is_expected_optional_fetch_failure(
            "service_status",
            RuntimeError("Unable to get AccountServiceStatus."),
        )
        is True
    )

    assert (
        coordinator._is_expected_optional_fetch_failure(
            "service_status",
            RuntimeError("temporary timeout"),
        )
        is False
    )

    assert (
        coordinator._is_expected_optional_fetch_failure(
            "balance",
            RuntimeError("Unable to get AccountServiceStatus."),
        )
        is False
    )
