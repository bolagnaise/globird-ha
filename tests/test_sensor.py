"""Tests for GloBird sensor helpers."""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components"
INTEGRATION_PATH = COMPONENT_PATH / "globird_ha"
GAS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "globird_gas_responses.json"

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
recorder = types.ModuleType("homeassistant.components.recorder")
recorder_models = types.ModuleType("homeassistant.components.recorder.models")
helpers = types.ModuleType("homeassistant.helpers")
entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
event = types.ModuleType("homeassistant.helpers.event")
storage = types.ModuleType("homeassistant.helpers.storage")
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
util = types.ModuleType("homeassistant.util")
dt = types.ModuleType("homeassistant.util.dt")
unit_conversion = types.ModuleType("homeassistant.util.unit_conversion")


class SensorDeviceClass:
    """Minimal sensor device classes used by the integration."""

    ENERGY = "energy"
    ENUM = "enum"
    GAS = "gas"
    MONETARY = "monetary"
    TEMPERATURE = "temperature"
    TIMESTAMP = "timestamp"


class SensorEntity:
    """Minimal stand-in for Home Assistant's SensorEntity."""


class SensorStateClass:
    """Minimal sensor state classes used by the integration."""

    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


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

    def _handle_coordinator_update(self) -> None:
        return None


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


class StatisticMeanType:
    """Minimal recorder mean type enum stub."""

    NONE = 0


def async_track_point_in_time(*_args: Any, **_kwargs: Any) -> Any:
    """Return an unsubscribe callback."""
    return lambda: None


sensor_component.SensorDeviceClass = SensorDeviceClass
sensor_component.SensorEntity = SensorEntity
sensor_component.SensorStateClass = SensorStateClass
config_entries.ConfigEntry = object
const.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
const.UnitOfTemperature = types.SimpleNamespace(CELSIUS="C")
const.UnitOfVolume = types.SimpleNamespace(CUBIC_METERS="m3")
core.HomeAssistant = object
core.callback = lambda func: func
entity_platform.AddEntitiesCallback = object
event.async_track_point_in_time = async_track_point_in_time
storage.Store = Store
update_coordinator.CoordinatorEntity = CoordinatorEntity
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed
recorder_models.StatisticMeanType = StatisticMeanType
unit_conversion.VolumeConverter = types.SimpleNamespace(UNIT_CLASS="volume")
unit_conversion.EnergyConverter = types.SimpleNamespace(UNIT_CLASS="energy")
dt.now = lambda: datetime.now(timezone.utc)
util.dt = dt
util.unit_conversion = unit_conversion

components.sensor = sensor_component
components.recorder = recorder
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
sys.modules["homeassistant.components.recorder"] = recorder
sys.modules["homeassistant.components.recorder.models"] = recorder_models
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
sys.modules["homeassistant.util.unit_conversion"] = unit_conversion

sensor = importlib.import_module("custom_components.globird_ha.sensor")


def load_gas_fixtures() -> dict[str, Any]:
    """Load dedicated gas fixture payloads."""
    return json.loads(GAS_FIXTURE_PATH.read_text())


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


def test_zerohero_status_reports_latest_complete_result() -> None:
    """Achieved/missed should follow the latest complete portal result."""
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
        == "missed"
    )
    assert (
        sensor._zerohero_status(
            yesterday_summary,
            datetime(2026, 7, 2, 21, 0, tzinfo=local_tz),
        )
        == "missed"
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


def test_is_gas_service_matches_service_type() -> None:
    """Gas services should be detected from serviceType."""
    assert sensor._is_gas_service({"serviceType": "Gas"}) is True
    assert sensor._is_gas_service({"serviceType": "POWER"}) is False


def test_non_gas_service_sensor_name_uses_original_title() -> None:
    """Non-gas service sensors should keep the original title-only naming."""

    class FakeCoordinator:
        data = {}

    entity = sensor.GloBirdBillingPeriodCostSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {
            "accountServiceId": 810965,
            "serviceType": "Power",
            "siteIdentifier": "NMI00000001",
        },
    )

    assert entity._attr_name == "Billing Period Cost"


def test_gas_service_sensor_name_includes_site_suffix() -> None:
    """Gas service sensors should include identifiers to avoid ambiguous names."""

    class FakeCoordinator:
        data = {
            "service_data": {
                "123456": {
                    "service": {
                        "accountServiceId": 123456,
                        "siteIdentifier": "55104217567",
                        "serviceType": "Gas",
                    },
                    "gas_reading_summary": {},
                }
            }
        }

    reading = sensor.GloBirdLatestGasReadingSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {
            "accountServiceId": 123456,
            "serviceType": "Gas",
            "siteIdentifier": "55104217567",
        },
    )
    reading_date = sensor.GloBirdLatestGasReadingDateSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {
            "accountServiceId": 123456,
            "serviceType": "Gas",
            "siteIdentifier": "55104217567",
        },
    )

    assert reading._attr_name == "Latest Gas Reading (55104217567)"
    assert reading_date._attr_name == "Latest Gas Reading Date (55104217567)"


def test_gas_shared_service_sensors_include_site_suffix() -> None:
    """Shared service sensors should also include suffixes when the service is gas."""

    class FakeCoordinator:
        data = {
            "service_data": {
                "123456": {
                    "service": {
                        "accountServiceId": 123456,
                        "siteIdentifier": "55104217567",
                        "serviceType": "Gas",
                    }
                }
            }
        }

    status = sensor.GloBirdServiceStatusSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {
            "accountServiceId": 123456,
            "serviceType": "Gas",
            "siteIdentifier": "55104217567",
        },
    )
    meter = sensor.GloBirdMeterInfoSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {
            "accountServiceId": 123456,
            "serviceType": "Gas",
            "siteIdentifier": "55104217567",
        },
    )

    assert status._attr_name == "Service Status (55104217567)"
    assert meter._attr_name == "Meter Info (55104217567)"


def test_safe_statistic_id_sanitizes_for_recorder() -> None:
    """Recorder statistic IDs must contain only supported characters."""
    assert (
        sensor._safe_statistic_id(
            "01ABC-service 123/latest-gas-reading",
            fallback="service-1",
        )
        == "svc_01abc_service_123_latest_gas_reading"
    )
    assert sensor._safe_statistic_id("a---b___c", fallback="x") == "a_b_c"


def test_statistic_id_uses_recorder_domain_prefix() -> None:
    """Statistic IDs should use recorder's <domain>:<slug> format."""
    suffix = sensor._safe_statistic_id("entry-1_service_123_latest_gas_reading", "x")
    statistic_id = f"{sensor.DOMAIN}:{suffix}"
    assert statistic_id.startswith(f"{sensor.DOMAIN}:")
    assert "__" not in statistic_id


def test_gas_statistics_continue_across_meter_replacement() -> None:
    """A lower replacement-meter index should continue the cumulative sum."""
    local_tz = timezone(timedelta(hours=10))
    statistics = sensor._build_gas_statistics(
        [
            {"date": "2026-01-01", "read_index": 100.0, "serial": "old"},
            {"date": "2026-02-01", "read_index": 110.0, "serial": "old"},
            {"date": "2026-03-01", "read_index": 5.0, "serial": "new"},
            {"date": "2026-04-01", "read_index": 12.0, "serial": "new"},
        ],
        tzinfo=local_tz,
    )

    assert [row["state"] for row in statistics] == [100.0, 110.0, 5.0, 12.0]
    assert [row["sum"] for row in statistics] == [100.0, 110.0, 110.0, 117.0]
    assert statistics[0]["start"].utcoffset() == timedelta(hours=10)


def test_gas_statistics_ignore_downward_correction_without_double_counting() -> None:
    """A corrected lower read must not be counted again when the index recovers."""
    statistics = sensor._build_gas_statistics(
        [
            {"date": "2026-01-01", "read_index": 100.0, "serial": "meter"},
            {"date": "2026-02-01", "read_index": 95.0, "serial": "meter"},
            {"date": "2026-03-01", "read_index": 102.0, "serial": "meter"},
        ],
        tzinfo=timezone.utc,
    )

    assert [row["sum"] for row in statistics] == [100.0, 100.0, 102.0]


def test_usage_half_hourly_statistics_aggregate_into_hourly_buckets() -> None:
    """Sub-hourly intervals are summed into hourly rows with a running sum.

    Home Assistant's recorder rejects external statistics whose `start` is
    not exactly on the hour, so even though GloBird reports finer-grained
    intervals (5- or 30-minute), the statistics import must bucket them to
    the hour before handing them to async_add_external_statistics.
    """
    local_tz = timezone(timedelta(hours=10))
    statistics = sensor._build_usage_half_hourly_statistics(
        [
            {"readDate": "2026-04-23", "intervals": [0.1] * 48},
            {"readDate": "2026-04-24", "intervals": [0.5, 1.5] + [0.0] * 46},
        ],
        tzinfo=local_tz,
    )

    # 24 hourly rows per day, not 48 half-hourly rows.
    assert len(statistics) == 48
    assert all(row["start"].minute == 0 and row["start"].second == 0 for row in statistics)
    assert [row["state"] for row in statistics[:3]] == [0.2, 0.2, 0.2]
    assert statistics[23]["sum"] == 4.8
    assert statistics[24]["sum"] == 6.8
    assert statistics[25]["sum"] == 6.8
    assert statistics[0]["start"] == datetime(2026, 4, 23, 0, 0, tzinfo=local_tz)
    assert statistics[1]["start"] == datetime(2026, 4, 23, 1, 0, tzinfo=local_tz)
    assert statistics[24]["start"] == datetime(2026, 4, 24, 0, 0, tzinfo=local_tz)


def test_usage_half_hourly_statistics_skip_days_without_intervals() -> None:
    """Rows missing a parseable date or interval array are ignored, not errored."""
    statistics = sensor._build_usage_half_hourly_statistics(
        [
            {"readDate": "not-a-date", "intervals": [1.0]},
            {"readDate": "2026-04-24", "intervals": []},
            {"readDate": "2026-04-25", "intervals": [1.0, 2.0]},
        ],
        tzinfo=timezone.utc,
    )

    assert len(statistics) == 2
    assert statistics[0]["start"] == datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
    assert statistics[0]["sum"] == 1.0
    assert statistics[1]["start"] == datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    assert statistics[1]["sum"] == 3.0


def test_usage_total_sensor_exposes_recent_intervals_by_day() -> None:
    """Recent Usage Total attributes include a truncated intervals_by_day window."""

    class FakeCoordinator:
        data = {
            "service_data": {
                "svc-1": {
                    "usage_summary": {
                        "total_usage": 10.0,
                        "intervals_by_day": [
                            {"readDate": "2026-04-24", "intervals": [1.0, 2.0]}
                        ],
                        "daily": [],
                        "registers": [],
                    },
                }
            }
        }

    sensor_entity = sensor.GloBirdUsageTotalSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-1", "siteIdentifier": "svc-1"},
    )

    attrs = sensor_entity.extra_state_attributes
    assert attrs["intervals_by_day"] == [
        {"readDate": "2026-04-24", "intervals": [1.0, 2.0]}
    ]
    assert attrs["intervals_by_day_count"] == 1
    assert attrs["intervals_by_day_truncated"] is False


def test_daily_cost_statistics_accumulate_across_days() -> None:
    """Each day's net cost total becomes a statistic row with a running sum."""
    local_tz = timezone(timedelta(hours=10))
    statistics = sensor._build_daily_cost_statistics(
        [
            {"date": "2026/09/01", "amount": 2.47},
            {"date": "2026/09/02", "amount": 2.35},
            {"date": "2026/09/03", "amount": 2.55},
        ],
        tzinfo=local_tz,
    )

    assert [row["state"] for row in statistics] == [2.47, 2.35, 2.55]
    assert [row["sum"] for row in statistics] == [2.47, 4.82, 7.37]
    assert statistics[0]["start"] == datetime(2026, 9, 1, 0, 0, tzinfo=local_tz)
    assert statistics[2]["start"] == datetime(2026, 9, 3, 0, 0, tzinfo=local_tz)


def test_daily_cost_statistics_skip_rows_without_a_parseable_date_or_amount() -> None:
    """Malformed rows are ignored rather than raising."""
    statistics = sensor._build_daily_cost_statistics(
        [
            {"date": "not-a-date", "amount": 1.0},
            {"date": "2026/09/01", "amount": None},
            {"date": "2026/09/02", "amount": 3.0},
        ],
        tzinfo=timezone.utc,
    )

    assert len(statistics) == 1
    assert statistics[0]["sum"] == 3.0


def test_cost_total_sensor_statistic_id_matches_usage_pattern() -> None:
    """Cost statistics reuse the same recorder-safe id derivation as usage/gas."""
    suffix = sensor._safe_statistic_id("entry-1_service_svc-1_cost_total", "svc-1")
    statistic_id = f"{sensor.DOMAIN}:{suffix}"
    assert statistic_id.startswith(f"{sensor.DOMAIN}:")
    assert "cost_total" in statistic_id


def test_calculated_cost_sensor_unavailable_without_schedule() -> None:
    """No configured TOU schedule means the sensor is unavailable, not zero."""

    class FakeCoordinator:
        data = {"service_data": {"svc-1": {"calculated_cost_summary": {}}}}

    sensor_entity = sensor.GloBirdCalculatedCostSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-1", "siteIdentifier": "svc-1", "serviceType": "Power"},
    )

    assert sensor_entity.available is False
    assert sensor_entity.native_value is None


def test_calculated_cost_sensor_exposes_latest_day_breakdown() -> None:
    """With a schedule configured, state/attributes reflect the latest calculated day."""

    class FakeCoordinator:
        data = {
            "tou_schedule_error": None,
            "service_data": {
                "svc-1": {
                    "calculated_cost_summary": {
                        "days": 1,
                        "latest_day": "2026-09-01",
                        "latest_day_cost": 3.40,
                        "total_cost": 3.40,
                        "daily": [
                            {
                                "readDate": "2026-09-01",
                                "total_cost": 3.40,
                                "usage_cost": 2.40,
                                "supply_charge": 1.0,
                                "unassigned_kwh": 0.0,
                                "periods": [
                                    {
                                        "name": "Offpeak",
                                        "kwh": 2.0,
                                        "cost": 0.4,
                                        "rate": 0.2,
                                    },
                                    {
                                        "name": "Peak",
                                        "kwh": 4.0,
                                        "cost": 2.0,
                                        "rate": 0.5,
                                    },
                                ],
                            }
                        ],
                    }
                }
            },
        }

    sensor_entity = sensor.GloBirdCalculatedCostSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-1", "siteIdentifier": "svc-1", "serviceType": "Power"},
    )

    assert sensor_entity.available is True
    assert sensor_entity.native_value == 3.40
    attrs = sensor_entity.extra_state_attributes
    assert attrs["total_cost"] == 3.40
    assert attrs["latest_day_unassigned_kwh"] == 0.0
    assert len(attrs["latest_day_periods"]) == 2
    assert attrs["schedule_error"] is None


def test_calculated_gas_cost_sensor_unavailable_without_schedule() -> None:
    """No configured gas rate schedule means the sensor is unavailable."""

    class FakeCoordinator:
        data = {"service_data": {"svc-gas": {"calculated_gas_cost_summary": {}}}}

    sensor_entity = sensor.GloBirdCalculatedGasCostSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-gas", "siteIdentifier": "svc-gas", "serviceType": "Gas"},
    )

    assert sensor_entity.available is False
    assert sensor_entity.native_value is None


def test_calculated_gas_cost_sensor_exposes_latest_period_breakdown() -> None:
    """With a schedule configured, state/attributes reflect the latest billed period."""

    class FakeCoordinator:
        data = {
            "gas_schedule_error": None,
            "service_data": {
                "svc-gas": {
                    "calculated_gas_cost_summary": {
                        "periods": [
                            {
                                "start": "2026-08-01",
                                "end": "2026-08-31",
                                "days": 30,
                                "mj_used": 1158.0,
                                "avg_daily_mj": 38.6,
                                "season": "Winter",
                                "usage_cost": 38.95,
                                "daily_charge_cost": 17.61,
                                "total_cost": 56.56,
                            }
                        ],
                        "latest_period_cost": 56.56,
                        "total_cost": 56.56,
                    }
                }
            },
        }

    sensor_entity = sensor.GloBirdCalculatedGasCostSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-gas", "siteIdentifier": "svc-gas", "serviceType": "Gas"},
    )

    assert sensor_entity.available is True
    assert sensor_entity.native_value == 56.56
    attrs = sensor_entity.extra_state_attributes
    assert attrs["total_cost"] == 56.56
    assert attrs["periods"] == 1
    assert attrs["schedule_error"] is None


def test_meter_info_sensor_exposes_meter_type_description() -> None:
    """Meter Info attributes surface the looked-up meter type description."""

    class FakeCoordinator:
        data = {
            "service_data": {
                "svc-1": {
                    "meter": {"meterReadType": "SMART", "serialStatus": "Energized"},
                    "meter_type_description": "Smart",
                }
            }
        }

    sensor_entity = sensor.GloBirdMeterInfoSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": "svc-1", "siteIdentifier": "svc-1", "serviceType": "Power"},
    )

    assert sensor_entity.extra_state_attributes["meter_type_description"] == "Smart"


def test_weather_impacted_days_sensor_reads_numeric_value() -> None:
    """Weather Impacted Days surfaces numberOfImpactedDays from the account payload."""
    description = next(
        d for d in sensor.GLOBAL_SENSORS if d.key == "weather_impacted_days"
    )

    data = {"weather_impacted_days": {"data": {"numberOfImpactedDays": 3}}}
    assert description.value_fn(data) == 3
    assert description.value_fn({}) is None


def test_latest_gas_reading_sensor_exposes_reading_summary() -> None:
    """Latest Gas Reading sensor should surface parsed basic meter data."""
    gas_fixture = load_gas_fixtures()["gas_service_data"]

    class FakeCoordinator:
        data = {
            "service_data": {
                "123456": gas_fixture,
            }
        }

    sensor_entity = sensor.GloBirdLatestGasReadingSensor(
        FakeCoordinator(),
        types.SimpleNamespace(entry_id="entry-1"),
        {"accountServiceId": 123456, "serviceType": "Gas"},
    )

    assert sensor_entity.native_value == 3050.0
    assert sensor_entity.extra_state_attributes["latest_reading_date"] == "2026-07-12"
    assert sensor_entity.extra_state_attributes["history_count"] == 1
