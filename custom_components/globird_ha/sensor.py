"""Sensor entities for GloBird HA."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from inspect import isawaitable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    build_billing_period_projection,
    build_latest_data_status,
    calculated_cost_attributes,
    cost_attributes,
    gas_cost_attributes,
    service_id,
    usage_attributes,
)
from .const import DOMAIN
from .coordinator import GloBirdCoordinator

CURRENCY_AUD = "AUD"
_LOGGER = logging.getLogger(__name__)
ZEROHERO_STATUS_OPTIONS = (
    "achieved",
    "missed",
    "pending",
    "awaiting_result",
    "unknown",
)
ZEROHERO_RESULT_CUTOFF = dt_time(hour=21)


def _latest_data_status(detail: dict[str, Any]) -> dict[str, Any]:
    """Return cached or computed latest data readiness."""
    status = detail.get("latest_data_status")
    if isinstance(status, dict):
        return status
    return build_latest_data_status(
        detail.get("usage_summary") or {},
        detail.get("cost_summary") or {},
    )


def _payload_data(payload: dict[str, Any] | None) -> Any:
    """Return a standard GloBird payload's data object."""
    if isinstance(payload, dict):
        return payload.get("data")
    return None


def _latest_invoice(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest invoice from the dashboard payload."""
    dashboard_data = _payload_data(data.get("dashboard")) or {}
    invoice = dashboard_data.get("lastestInvoice")
    return invoice if isinstance(invoice, dict) else None


def _recent_transactions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return recent dashboard transactions."""
    dashboard_data = _payload_data(data.get("dashboard")) or {}
    transactions = dashboard_data.get("recentAccountTransactions") or []
    return transactions[:10] if isinstance(transactions, list) else []


def _balance_value(data: dict[str, Any]) -> Any:
    balance = _payload_data(data.get("balance")) or {}
    val = balance.get("balance")
    # GloBird returns positive for credit; negate so credit=negative, debt=positive
    return -val if val is not None else None


def _balance_attrs(data: dict[str, Any]) -> dict[str, Any]:
    balance = _payload_data(data.get("balance")) or {}
    return {
        "max_refundable_amount": balance.get("maxRefundableAmount"),
        "show_refundable_amount": balance.get("showRefundableAmount"),
    }


def _dashboard_balance_value(data: dict[str, Any]) -> Any:
    dashboard = _payload_data(data.get("dashboard")) or {}
    val = dashboard.get("currentBalance")
    return -val if val is not None else None


def _dashboard_attrs(data: dict[str, Any]) -> dict[str, Any]:
    dashboard = _payload_data(data.get("dashboard")) or {}
    return {
        "account_id": dashboard.get("accountId"),
        "account_number": dashboard.get("accountNumber"),
        "latest_correspondence": dashboard.get("lastestCorrespondence"),
        "latest_invoice": dashboard.get("lastestInvoice"),
        "recent_transactions": _recent_transactions(data),
    }


def _latest_invoice_value(data: dict[str, Any]) -> Any:
    invoice = _latest_invoice(data)
    return invoice.get("amount") if invoice else None


def _latest_invoice_attrs(data: dict[str, Any]) -> dict[str, Any]:
    return dict(_latest_invoice(data) or {})


def _signup_services_value(data: dict[str, Any]) -> int:
    signup = _payload_data(data.get("signup_info"))
    return len(signup) if isinstance(signup, list) else 0


def _signup_services_attrs(data: dict[str, Any]) -> dict[str, Any]:
    return {"signup_info": _payload_data(data.get("signup_info")) or []}


def _timestamp_value(value: Any) -> datetime | None:
    """Return a Home Assistant timestamp value from a unix timestamp."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _last_successful_refresh_value(data: dict[str, Any]) -> datetime | None:
    return _timestamp_value(data.get("last_update"))


def _timestamp_attr(value: Any) -> str | None:
    timestamp = _timestamp_value(value)
    return timestamp.isoformat() if timestamp else None


def _local_today() -> date:
    """Return today's date in Home Assistant's configured timezone."""
    return dt_util.now().date()


def _parse_portal_day(value: Any) -> date | None:
    """Parse a portal day string into a date."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for separator in ("T", " "):
        raw = raw.split(separator, 1)[0]
    try:
        return date.fromisoformat(raw.replace("/", "-"))
    except ValueError:
        return None


def _billing_period_completed_days(
    data: dict[str, Any],
    today: date | None = None,
) -> int | None:
    """Return completed billing-period days using the local HA date."""
    start = _billing_period_start(data)
    if start is None:
        return None
    return max(0, ((today or _local_today()) - start).days)


def _service_type(service: dict[str, Any]) -> str:
    """Return a normalized service type."""
    return str(service.get("serviceType") or "").strip().lower()


def _is_gas_service(service: dict[str, Any]) -> bool:
    """Return whether a service is gas."""
    return "gas" in _service_type(service)


def _service_name_suffix(service: dict[str, Any]) -> str:
    """Return a readable label that distinguishes services in sensor names."""
    site_identifier = str(service.get("siteIdentifier") or "").strip()
    account_service_id = str(service.get("accountServiceId") or "").strip()

    if site_identifier:
        return site_identifier
    if account_service_id:
        return account_service_id
    return account_service_id or "unknown"


def _safe_statistic_id(raw_id: Any, fallback: str) -> str:
    """Return a recorder-safe statistic ID suffix.

    Home Assistant recorder rejects statistic IDs with unsupported characters.
    """
    raw = str(raw_id or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not safe:
        safe = re.sub(r"[^a-z0-9_]+", "_", fallback.lower()).strip("_")
    if not safe:
        return "service"
    # Recorder rejects IDs containing double underscores.
    safe = re.sub(r"_+", "_", safe)
    if not safe[0].isalpha():
        safe = f"svc_{safe}"
    return safe.strip("_") or "service"


def _build_gas_statistics(
    history_rows: list[dict[str, Any]],
    *,
    tzinfo: Any,
) -> list[dict[str, Any]]:
    """Build serial-aware cumulative gas statistics.

    A replacement meter may restart at a lower index. Keep an independent
    high-water mark for each serial so the cumulative sum continues without
    discarding usage from the replacement meter.
    """
    rows = sorted(
        (
            row
            for row in history_rows
            if isinstance(row, dict)
            and _parse_portal_day(row.get("date")) is not None
            and isinstance(row.get("read_index"), (int, float))
        ),
        key=lambda row: (
            str(row.get("date") or ""),
            str(row.get("serial") or ""),
            float(row.get("read_index") or 0.0),
        ),
    )
    if not rows:
        return []

    meter_high_water: dict[str, float] = {}
    cumulative_sum: float | None = None
    by_day: dict[date, dict[str, Any]] = {}

    for row in rows:
        parsed_day = _parse_portal_day(row.get("date"))
        if parsed_day is None:
            continue
        reading = float(row["read_index"])
        meter_key = str(row.get("serial") or "unknown")
        previous = meter_high_water.get(meter_key)

        if cumulative_sum is None:
            cumulative_sum = reading
        elif previous is not None and reading > previous:
            cumulative_sum += reading - previous

        meter_high_water[meter_key] = (
            reading if previous is None else max(previous, reading)
        )
        by_day[parsed_day] = {
            "start": datetime.combine(parsed_day, dt_time.min, tzinfo=tzinfo),
            "state": reading,
            "sum": cumulative_sum,
        }

    return [by_day[day] for day in sorted(by_day)]


def _build_usage_half_hourly_statistics(
    intervals_by_day: list[dict[str, Any]],
    *,
    tzinfo: Any,
) -> list[dict[str, Any]]:
    """Build cumulative-sum hourly statistics from per-day usage intervals.

    Home Assistant's recorder rejects external statistics whose `start` is
    not exactly on the hour (minutes/seconds must be 0) -- external
    statistics only support hourly resolution, regardless of how fine the
    source interval data is. So sub-hourly intervals (GloBird reports 5- or
    30-minute intervals depending on the meter) are summed into hourly
    buckets here before being handed to the recorder; the finer-grained data
    is still available raw via calculate_tou_cost and the intervals_by_day
    attribute, just not through this statistics import.
    """
    rows = sorted(
        (
            row
            for row in intervals_by_day
            if isinstance(row, dict)
            and _parse_portal_day(row.get("readDate")) is not None
            and isinstance(row.get("intervals"), list)
            and row.get("intervals")
        ),
        key=lambda row: str(row.get("readDate") or ""),
    )
    if not rows:
        return []

    hourly_usage: dict[datetime, float] = {}
    for row in rows:
        day = _parse_portal_day(row["readDate"])
        intervals = row["intervals"]
        minutes_per_interval = 1440 // len(intervals)
        day_start = datetime.combine(day, dt_time.min, tzinfo=tzinfo)
        for index, value in enumerate(intervals):
            usage = float(value) if isinstance(value, (int, float)) else 0.0
            interval_start = day_start + timedelta(minutes=index * minutes_per_interval)
            hour_start = interval_start.replace(minute=0, second=0, microsecond=0)
            hourly_usage[hour_start] = hourly_usage.get(hour_start, 0.0) + usage

    statistics: list[dict[str, Any]] = []
    cumulative_sum = 0.0
    for hour_start in sorted(hourly_usage):
        usage = hourly_usage[hour_start]
        cumulative_sum += usage
        statistics.append(
            {
                "start": hour_start,
                "state": round(usage, 5),
                "sum": round(cumulative_sum, 5),
            }
        )
    return statistics


def _build_daily_cost_statistics(
    daily_totals: list[dict[str, Any]],
    *,
    tzinfo: Any,
) -> list[dict[str, Any]]:
    """Build cumulative-sum daily cost statistics from net daily cost totals.

    GloBird's cost detail is only published at daily resolution (no
    half-hourly breakdown), so this statistic is daily-granularity, unlike
    the half-hourly usage statistics.
    """
    rows = sorted(
        (
            row
            for row in daily_totals
            if isinstance(row, dict)
            and _parse_portal_day(row.get("date")) is not None
            and isinstance(row.get("amount"), (int, float))
        ),
        key=lambda row: str(row.get("date") or ""),
    )
    if not rows:
        return []

    statistics: list[dict[str, Any]] = []
    cumulative_sum = 0.0
    for row in rows:
        day = _parse_portal_day(row["date"])
        amount = float(row["amount"])
        cumulative_sum += amount
        statistics.append(
            {
                "start": datetime.combine(day, dt_time.min, tzinfo=tzinfo),
                "state": round(amount, 2),
                "sum": round(cumulative_sum, 2),
            }
        )
    return statistics


def _zerohero_last_result(
    summary: dict[str, Any],
) -> tuple[str, date | None, str | None]:
    """Return the latest complete ZEROHERO result regardless of local day."""
    latest_day_raw = summary.get("latest_day")
    latest_day = _parse_portal_day(latest_day_raw)
    credit = summary.get("latest_day_zerohero_credit")
    if latest_day is None or credit is None:
        return "unknown", latest_day, latest_day_raw
    result = "achieved" if summary.get("latest_day_zerohero_achieved") else "missed"
    return result, latest_day, latest_day_raw


def _zerohero_status(
    summary: dict[str, Any],
    now: datetime | None = None,
) -> str:
    """Return the latest complete ZEROHERO portal result."""
    last_result, latest_day, _latest_day_raw = _zerohero_last_result(summary)
    if last_result == "unknown" or latest_day is None:
        return "unknown"
    return last_result


def _next_zerohero_status_boundary(now: datetime) -> datetime:
    """Return the next local boundary where ZEROHERO status can change."""
    today_cutoff = datetime.combine(
        now.date(),
        ZEROHERO_RESULT_CUTOFF,
        tzinfo=now.tzinfo,
    )
    if now < today_cutoff:
        return today_cutoff
    return datetime.combine(
        now.date() + timedelta(days=1),
        dt_time.min,
        tzinfo=now.tzinfo,
    )


def _refresh_status_value(data: dict[str, Any]) -> str:
    return "error" if data.get("refresh_error") else "ok"


def _weather_impacted_days_value(data: dict[str, Any]) -> Any:
    payload = _payload_data(data.get("weather_impacted_days")) or {}
    return payload.get("numberOfImpactedDays")


def _refresh_status_attrs(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_successful_refresh": _timestamp_attr(data.get("last_update")),
        "last_failed_refresh": _timestamp_attr(data.get("last_failed_update")),
        "refresh_error": data.get("refresh_error"),
        "fetch_errors": data.get("_fetch_errors") or {},
    }


@dataclass(frozen=True)
class GloBirdSensorDescription:
    """Description for a GloBird sensor."""

    key: str
    name: str
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    native_unit_of_measurement: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None


GLOBAL_SENSORS: tuple[GloBirdSensorDescription, ...] = (
    GloBirdSensorDescription(
        key="balance",
        name="Balance",
        value_fn=_balance_value,
        attrs_fn=_balance_attrs,
        native_unit_of_measurement=CURRENCY_AUD,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash",
    ),
    GloBirdSensorDescription(
        key="dashboard_balance",
        name="Dashboard Balance",
        value_fn=_dashboard_balance_value,
        attrs_fn=_dashboard_attrs,
        native_unit_of_measurement=CURRENCY_AUD,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:view-dashboard",
    ),
    GloBirdSensorDescription(
        key="latest_invoice",
        name="Latest Invoice",
        value_fn=_latest_invoice_value,
        attrs_fn=_latest_invoice_attrs,
        native_unit_of_measurement=CURRENCY_AUD,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:file-document",
    ),
    GloBirdSensorDescription(
        key="signup_services",
        name="Signup Services",
        value_fn=_signup_services_value,
        attrs_fn=_signup_services_attrs,
        icon="mdi:transmission-tower",
    ),
    GloBirdSensorDescription(
        key="weather_impacted_days",
        name="Weather Impacted Days",
        value_fn=_weather_impacted_days_value,
        icon="mdi:weather-lightning-rainy",
    ),
    GloBirdSensorDescription(
        key="last_successful_refresh",
        name="Last Successful Refresh",
        value_fn=_last_successful_refresh_value,
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:update",
    ),
    GloBirdSensorDescription(
        key="refresh_status",
        name="Refresh Status",
        value_fn=_refresh_status_value,
        attrs_fn=_refresh_status_attrs,
        icon="mdi:cloud-refresh",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GloBird sensors from a config entry."""
    coordinator: GloBirdCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    data = coordinator.data or {}

    entities: list[SensorEntity] = [
        GloBirdGlobalSensor(coordinator, config_entry, description)
        for description in GLOBAL_SENSORS
    ]

    for account in data.get("accounts", []):
        entities.append(GloBirdAccountSummarySensor(coordinator, config_entry, account))

    for service in data.get("services", []):
        service_entities: list[SensorEntity] = [
            GloBirdServiceStatusSensor(coordinator, config_entry, service),
            GloBirdMeterInfoSensor(coordinator, config_entry, service),
        ]

        if _is_gas_service(service):
            service_entities.extend(
                [
                    GloBirdLatestGasReadingSensor(coordinator, config_entry, service),
                    GloBirdLatestGasReadingDateSensor(
                        coordinator,
                        config_entry,
                        service,
                    ),
                    GloBirdCalculatedGasCostSensor(coordinator, config_entry, service),
                ]
            )
        else:
            service_entities.extend(
                [
                    GloBirdLatestDataDateSensor(coordinator, config_entry, service),
                    GloBirdLatestDataStatusSensor(coordinator, config_entry, service),
                    GloBirdUsageTotalSensor(coordinator, config_entry, service),
                    GloBirdLatestDayUsageSensor(coordinator, config_entry, service),
                    GloBirdSolarExportTotalSensor(coordinator, config_entry, service),
                    GloBirdLatestDaySolarExportSensor(
                        coordinator,
                        config_entry,
                        service,
                    ),
                    GloBirdCostTotalSensor(coordinator, config_entry, service),
                    GloBirdLatestDayCostSensor(coordinator, config_entry, service),
                    GloBirdCalculatedCostSensor(coordinator, config_entry, service),
                    GloBirdZeroHeroStatusSensor(coordinator, config_entry, service),
                    GloBirdExpectedMonthlyCostSensor(
                        coordinator,
                        config_entry,
                        service,
                    ),
                    GloBirdBillingPeriodDaysSensor(coordinator, config_entry, service),
                    GloBirdBillingPeriodCostSensor(coordinator, config_entry, service),
                    GloBirdWeatherSummarySensor(coordinator, config_entry, service),
                ]
            )

        entities.extend(service_entities)

    async_add_entities(entities)


class GloBirdBaseSensor(CoordinatorEntity[GloBirdCoordinator], SensorEntity):
    """Base class for GloBird sensors."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: GloBirdCoordinator, config_entry: ConfigEntry
    ) -> None:
        """Initialize the base sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry


class GloBirdGlobalSensor(GloBirdBaseSensor):
    """A config-entry level GloBird sensor."""

    def __init__(
        self,
        coordinator: GloBirdCoordinator,
        config_entry: ConfigEntry,
        description: GloBirdSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry)
        self._description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{config_entry.entry_id}_{description.key}"
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_icon = description.icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": "GloBird Energy",
            "manufacturer": "GloBird Energy",
            "model": "Customer Portal",
        }

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self._description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return sensor attributes."""
        attrs_fn = self._description.attrs_fn
        return attrs_fn(self.coordinator.data or {}) if attrs_fn else {}


class GloBirdAccountSummarySensor(GloBirdBaseSensor):
    """Summary sensor for a GloBird account."""

    def __init__(
        self,
        coordinator: GloBirdCoordinator,
        config_entry: ConfigEntry,
        account: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry)
        self._account_id = str(account.get("accountId") or account.get("accountNumber"))
        self._attr_name = f"Account {account.get('accountNumber') or self._account_id}"
        self._attr_icon = "mdi:account"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_account_{self._account_id}_summary"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": "GloBird Energy",
            "manufacturer": "GloBird Energy",
            "model": "Customer Portal",
        }

    def _account(self) -> dict[str, Any]:
        """Return the latest account row."""
        for account in (self.coordinator.data or {}).get("accounts", []):
            account_id = str(account.get("accountId") or account.get("accountNumber"))
            if account_id == self._account_id:
                return account
        return {}

    @property
    def native_value(self) -> Any:
        """Return account service count."""
        return self._account().get("service_count")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return account attributes."""
        return dict(self._account())


class GloBirdServiceBaseSensor(GloBirdBaseSensor):
    """Base class for service-level sensors."""

    sensor_key = "service"
    sensor_name = "Service"
    icon = "mdi:flash"
    native_unit_of_measurement: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None

    def __init__(
        self,
        coordinator: GloBirdCoordinator,
        config_entry: ConfigEntry,
        service: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry)
        self._service_id = service_id(service)
        if _is_gas_service(service):
            self._attr_name = f"{self.sensor_name} ({_service_name_suffix(service)})"
        else:
            self._attr_name = self.sensor_name
        self._attr_unique_id = (
            f"{config_entry.entry_id}_service_{self._service_id}_{self.sensor_key}"
        )
        self._attr_icon = self.icon
        self._attr_native_unit_of_measurement = self.native_unit_of_measurement
        self._attr_device_class = self.device_class
        self._attr_state_class = self.state_class
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": "GloBird Energy",
            "manufacturer": "GloBird Energy",
            "model": "Customer Portal",
        }

    def _service_detail(self) -> dict[str, Any]:
        """Return the latest service detail."""
        service_data = (self.coordinator.data or {}).get("service_data", {})
        detail = service_data.get(self._service_id)
        return detail if isinstance(detail, dict) else {}

    def _service_attrs(self) -> dict[str, Any]:
        """Return service metadata attributes."""
        detail = self._service_detail()
        service = detail.get("service") or {}
        return {
            "account_service_id": service.get("accountServiceId"),
            "site_identifier": service.get("siteIdentifier"),
            "site_address": service.get("siteAddress"),
            "post_code": service.get("postCode"),
            "service_type": service.get("serviceType"),
            "account_id": service.get("accountId"),
            "account_number": service.get("accountNumber"),
        }


class GloBirdServiceStatusSensor(GloBirdServiceBaseSensor):
    """Service status sensor."""

    sensor_key = "service_status"
    sensor_name = "Service Status"
    icon = "mdi:transmission-tower"

    @property
    def native_value(self) -> Any:
        """Return service status."""
        detail = self._service_detail()
        status = detail.get("status") or {}
        service = detail.get("service") or {}
        return status.get("status") or service.get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return service status attributes."""
        attrs = self._service_attrs()
        attrs["status_detail"] = self._service_detail().get("status")
        return attrs


class GloBirdMeterInfoSensor(GloBirdServiceBaseSensor):
    """Meter info sensor."""

    sensor_key = "meter_info"
    sensor_name = "Meter Info"
    icon = "mdi:counter"

    @property
    def native_value(self) -> Any:
        """Return meter read type."""
        meter = self._service_detail().get("meter") or {}
        return meter.get("meterReadType") or meter.get("serialStatus")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return meter attributes."""
        attrs = self._service_attrs()
        detail = self._service_detail()
        attrs["meter"] = detail.get("meter")
        attrs["meter_type_description"] = detail.get("meter_type_description")
        return attrs


class GloBirdLatestDataDateSensor(GloBirdServiceBaseSensor):
    """Latest service data date ready for daily automations."""

    sensor_key = "latest_data_date"
    sensor_name = "Latest Data Date"
    icon = "mdi:calendar-check"

    @property
    def native_value(self) -> Any:
        """Return the latest date with aligned usage and cost data."""
        return _latest_data_status(self._service_detail()).get("latest_ready_day")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest data status attributes."""
        attrs = self._service_attrs()
        detail = self._service_detail()
        latest_status = _latest_data_status(detail)
        attrs.update(
            {
                "status": latest_status.get("status"),
                "latest_usage_day": latest_status.get("latest_usage_day"),
                "latest_cost_day": latest_status.get("latest_cost_day"),
                "latest_available_cost_day": latest_status.get(
                    "latest_available_cost_day"
                ),
                "latest_available_cost_day_complete": latest_status.get(
                    "latest_available_cost_day_complete"
                ),
                "incomplete_cost_days": latest_status.get("incomplete_cost_days", []),
                "last_successful_refresh": _timestamp_attr(
                    (self.coordinator.data or {}).get("last_update")
                ),
            }
        )
        return attrs


class GloBirdLatestDataStatusSensor(GloBirdServiceBaseSensor):
    """Latest service data readiness status."""

    sensor_key = "latest_data_status"
    sensor_name = "Latest Data Status"
    icon = "mdi:calendar-sync"

    @property
    def native_value(self) -> Any:
        """Return whether the latest service data is ready."""
        return _latest_data_status(self._service_detail()).get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest data readiness attributes."""
        attrs = self._service_attrs()
        latest_status = _latest_data_status(self._service_detail())
        attrs.update(
            {
                "latest_ready_day": latest_status.get("latest_ready_day"),
                "latest_usage_day": latest_status.get("latest_usage_day"),
                "latest_cost_day": latest_status.get("latest_cost_day"),
                "latest_available_cost_day": latest_status.get(
                    "latest_available_cost_day"
                ),
                "latest_available_cost_day_complete": latest_status.get(
                    "latest_available_cost_day_complete"
                ),
                "incomplete_cost_days": latest_status.get("incomplete_cost_days", []),
            }
        )
        return attrs


class GloBirdLatestGasReadingSensor(GloBirdServiceBaseSensor):
    """Latest gas meter index reading."""

    sensor_key = "latest_gas_reading"
    sensor_name = "Latest Gas Reading"
    icon = "mdi:meter-gas"
    native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    device_class = SensorDeviceClass.GAS
    state_class = SensorStateClass.TOTAL_INCREASING

    async def async_added_to_hass(self) -> None:
        """Upload historical gas readings to recorder long-term statistics."""
        await super().async_added_to_hass()
        await self._async_upload_historical_statistics()

    def _handle_coordinator_update(self) -> None:
        """Refresh the entity and import any newly published gas reads."""
        super()._handle_coordinator_update()
        self.hass.async_create_task(self._async_upload_historical_statistics())

    async def _async_upload_historical_statistics(self) -> None:
        """Import all historical gas meter reads as external statistics."""
        summary = self._service_detail().get("gas_reading_summary") or {}
        history_rows = summary.get("history")
        if not isinstance(history_rows, list) or not history_rows:
            _LOGGER.debug(
                "GloBird gas statistics import skipped for %s (%s): no gas history rows available",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        try:
            from homeassistant.components.recorder.statistics import (
                StatisticData,
                StatisticMetaData,
                async_add_external_statistics,
            )
        except ImportError:
            return

        statistics = [
            StatisticData(**row)
            for row in _build_gas_statistics(
                history_rows,
                tzinfo=dt_util.now().tzinfo or timezone.utc,
            )
        ]

        if not statistics:
            _LOGGER.debug(
                "GloBird gas statistics import skipped for %s (%s): no valid gas statistics rows after parsing",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        statistic_suffix = _safe_statistic_id(
            getattr(self, "_attr_unique_id", self._service_id),
            self._service_id,
        )
        statistic_id = f"{DOMAIN}:{statistic_suffix}"
        metadata = StatisticMetaData(
            has_mean=False,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._attr_name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=self.native_unit_of_measurement,
        )
        _LOGGER.debug(
            "GloBird gas statistics prepared for %s (%s): %d rows from %s to %s",
            self._service_id,
            statistic_id,
            len(statistics),
            statistics[0]["start"].isoformat(),
            statistics[-1]["start"].isoformat(),
        )
        try:
            add_result = async_add_external_statistics(self.hass, metadata, statistics)
            if isawaitable(add_result):
                await add_result
        except Exception as err:  # noqa: BLE001 - statistics import is best-effort.
            _LOGGER.warning(
                "GloBird gas statistics import skipped for %s (%s): %s",
                self._service_id,
                statistic_id,
                err,
            )

    @property
    def native_value(self) -> Any:
        """Return latest gas meter read index."""
        return (self._service_detail().get("gas_reading_summary") or {}).get(
            "latest_reading"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest gas reading metadata and compact history."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("gas_reading_summary") or {}
        attrs.update(
            {
                "latest_reading_date": summary.get("latest_reading_date"),
                "latest_reading_source": summary.get("latest_reading_source"),
                "latest_reading_serial": summary.get("latest_reading_serial"),
                "latest_reading_quality_method": summary.get(
                    "latest_reading_quality_method"
                ),
                "history": summary.get("history_recent", []),
                "history_count": summary.get("history_count", 0),
                "history_truncated": summary.get("history_truncated", False),
            }
        )
        return attrs


class GloBirdLatestGasReadingDateSensor(GloBirdServiceBaseSensor):
    """Latest gas meter reading date."""

    sensor_key = "latest_gas_reading_date"
    sensor_name = "Latest Gas Reading Date"
    icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> Any:
        """Return latest gas read date."""
        return (self._service_detail().get("gas_reading_summary") or {}).get(
            "latest_reading_date"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest gas read metadata."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("gas_reading_summary") or {}
        attrs.update(
            {
                "latest_reading": summary.get("latest_reading"),
                "latest_reading_source": summary.get("latest_reading_source"),
                "latest_reading_serial": summary.get("latest_reading_serial"),
                "latest_reading_quality_method": summary.get(
                    "latest_reading_quality_method"
                ),
            }
        )
        return attrs


class GloBirdCalculatedGasCostSensor(GloBirdServiceBaseSensor):
    """Estimated gas cost calculated from meter reads and a user-configured
    seasonal, tiered rate schedule.

    GloBird's API exposes no usable gas rate data either, so this is
    calculated locally from a rate schedule entered in integration options
    (daily charge + seasonal $/MJ tiers) applied to average daily usage
    across each meter-read period. Stays unavailable until a schedule is
    configured.
    """

    sensor_key = "calculated_gas_cost"
    sensor_name = "Calculated Gas Cost"
    icon = "mdi:calculator-variant"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    @property
    def available(self) -> bool:
        """Only available once a gas rate schedule is configured and valid."""
        summary = self._service_detail().get("calculated_gas_cost_summary") or {}
        return bool(summary.get("periods"))

    @property
    def native_value(self) -> Any:
        """Return the most recent billed period's total cost."""
        summary = self._service_detail().get("calculated_gas_cost_summary") or {}
        return summary.get("latest_period_cost")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return calculated gas cost breakdown attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("calculated_gas_cost_summary") or {}
        attrs.update(gas_cost_attributes(summary))
        attrs["schedule_error"] = (self.coordinator.data or {}).get(
            "gas_schedule_error"
        )
        return attrs


class GloBirdUsageTotalSensor(GloBirdServiceBaseSensor):
    """Recent usage total sensor."""

    sensor_key = "usage_total"
    sensor_name = "Recent Usage Total"
    icon = "mdi:lightning-bolt"
    native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL

    async def async_added_to_hass(self) -> None:
        """Upload historical half-hourly usage to recorder long-term statistics."""
        await super().async_added_to_hass()
        await self._async_upload_historical_statistics()

    def _handle_coordinator_update(self) -> None:
        """Refresh the entity and import any newly published usage intervals."""
        super()._handle_coordinator_update()
        self.hass.async_create_task(self._async_upload_historical_statistics())

    async def _async_upload_historical_statistics(self) -> None:
        """Import all cached half-hourly usage as external statistics."""
        summary = self._service_detail().get("usage_summary") or {}
        intervals_by_day = summary.get("intervals_by_day")
        if not isinstance(intervals_by_day, list) or not intervals_by_day:
            _LOGGER.debug(
                "GloBird usage statistics import skipped for %s (%s): "
                "no interval data available",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        try:
            from homeassistant.components.recorder.statistics import (
                StatisticData,
                StatisticMetaData,
                async_add_external_statistics,
            )
        except ImportError:
            return

        statistics = [
            StatisticData(**row)
            for row in _build_usage_half_hourly_statistics(
                intervals_by_day,
                tzinfo=dt_util.now().tzinfo or timezone.utc,
            )
        ]

        if not statistics:
            _LOGGER.debug(
                "GloBird usage statistics import skipped for %s (%s): "
                "no valid interval rows after parsing",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        statistic_suffix = _safe_statistic_id(
            getattr(self, "_attr_unique_id", self._service_id),
            self._service_id,
        )
        statistic_id = f"{DOMAIN}:{statistic_suffix}"
        metadata = StatisticMetaData(
            has_mean=False,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._attr_name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=self.native_unit_of_measurement,
        )
        _LOGGER.debug(
            "GloBird usage statistics prepared for %s (%s): %d rows from %s to %s",
            self._service_id,
            statistic_id,
            len(statistics),
            statistics[0]["start"].isoformat(),
            statistics[-1]["start"].isoformat(),
        )
        try:
            add_result = async_add_external_statistics(self.hass, metadata, statistics)
            if isawaitable(add_result):
                await add_result
        except Exception as err:  # noqa: BLE001 - statistics import is best-effort.
            _LOGGER.warning(
                "GloBird usage statistics import skipped for %s (%s): %s",
                self._service_id,
                statistic_id,
                err,
            )

    @property
    def native_value(self) -> Any:
        """Return total recent usage."""
        return (self._service_detail().get("usage_summary") or {}).get("total_usage")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return usage summary attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("usage_summary") or {}
        attrs.update(
            usage_attributes(
                summary,
                direction="import",
                include_intervals_by_day=True,
            )
        )
        return attrs


class GloBirdLatestDayUsageSensor(GloBirdServiceBaseSensor):
    """Latest day usage sensor."""

    sensor_key = "latest_day_usage"
    sensor_name = "Latest Day Usage"
    icon = "mdi:calendar-today"
    native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> Any:
        """Return latest day usage."""
        return (self._service_detail().get("usage_summary") or {}).get(
            "latest_day_usage"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest interval attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("usage_summary") or {}
        attrs.update(
            usage_attributes(
                summary,
                direction="import",
                include_latest_intervals=True,
            )
        )
        return attrs


class GloBirdSolarExportTotalSensor(GloBirdServiceBaseSensor):
    """Recent solar export total sensor (B1 register)."""

    sensor_key = "solar_export_total"
    sensor_name = "Recent Solar Export Total"
    icon = "mdi:solar-power"
    native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> Any:
        """Return total recent solar export (feed-in)."""
        return (self._service_detail().get("usage_summary") or {}).get("total_export")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return solar export summary attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("usage_summary") or {}
        attrs.update(usage_attributes(summary, direction="export"))
        return attrs


class GloBirdLatestDaySolarExportSensor(GloBirdServiceBaseSensor):
    """Latest day solar export sensor (B1 register)."""

    sensor_key = "latest_day_solar_export"
    sensor_name = "Latest Day Solar Export"
    icon = "mdi:solar-power-variant"
    native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> Any:
        """Return latest day solar export."""
        return (self._service_detail().get("usage_summary") or {}).get(
            "latest_day_export"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest day attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("usage_summary") or {}
        attrs.update(usage_attributes(summary, direction="export"))
        return attrs


class GloBirdCostTotalSensor(GloBirdServiceBaseSensor):
    """Recent cost total sensor."""

    sensor_key = "cost_total"
    sensor_name = "Recent Cost Total"
    icon = "mdi:cash-multiple"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    async def async_added_to_hass(self) -> None:
        """Upload historical daily cost to recorder long-term statistics."""
        await super().async_added_to_hass()
        await self._async_upload_historical_statistics()

    def _handle_coordinator_update(self) -> None:
        """Refresh the entity and import any newly published daily cost."""
        super()._handle_coordinator_update()
        self.hass.async_create_task(self._async_upload_historical_statistics())

    async def _async_upload_historical_statistics(self) -> None:
        """Import all cached net daily cost totals as external statistics.

        Usable as the Energy Dashboard's cost statistic for the matching
        usage statistic on the Recent Usage Total sensor. Daily resolution
        only, since GloBird's cost detail is not published half-hourly.
        """
        summary = self._service_detail().get("cost_summary") or {}
        daily_totals = summary.get("daily_totals")
        if not isinstance(daily_totals, list) or not daily_totals:
            _LOGGER.debug(
                "GloBird cost statistics import skipped for %s (%s): "
                "no daily cost totals available",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        try:
            from homeassistant.components.recorder.statistics import (
                StatisticData,
                StatisticMetaData,
                async_add_external_statistics,
            )
        except ImportError:
            return

        statistics = [
            StatisticData(**row)
            for row in _build_daily_cost_statistics(
                daily_totals,
                tzinfo=dt_util.now().tzinfo or timezone.utc,
            )
        ]

        if not statistics:
            _LOGGER.debug(
                "GloBird cost statistics import skipped for %s (%s): "
                "no valid daily cost rows after parsing",
                self._service_id,
                getattr(self, "_attr_unique_id", self._service_id),
            )
            return

        statistic_suffix = _safe_statistic_id(
            getattr(self, "_attr_unique_id", self._service_id),
            self._service_id,
        )
        statistic_id = f"{DOMAIN}:{statistic_suffix}"
        metadata = StatisticMetaData(
            has_mean=False,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._attr_name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=self.native_unit_of_measurement,
        )
        _LOGGER.debug(
            "GloBird cost statistics prepared for %s (%s): %d rows from %s to %s",
            self._service_id,
            statistic_id,
            len(statistics),
            statistics[0]["start"].isoformat(),
            statistics[-1]["start"].isoformat(),
        )
        try:
            add_result = async_add_external_statistics(self.hass, metadata, statistics)
            if isawaitable(add_result):
                await add_result
        except Exception as err:  # noqa: BLE001 - statistics import is best-effort.
            _LOGGER.warning(
                "GloBird cost statistics import skipped for %s (%s): %s",
                self._service_id,
                statistic_id,
                err,
            )

    @property
    def native_value(self) -> Any:
        """Return total recent cost."""
        return (self._service_detail().get("cost_summary") or {}).get("total_amount")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return cost attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("cost_summary") or {}
        attrs.update(cost_attributes(summary))
        return attrs


class GloBirdLatestDayCostSensor(GloBirdServiceBaseSensor):
    """Latest daily cost sensor."""

    sensor_key = "latest_day_cost"
    sensor_name = "Latest Daily Cost"
    icon = "mdi:calendar-today"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    @property
    def native_value(self) -> Any:
        """Return latest day cost."""
        return (self._service_detail().get("cost_summary") or {}).get(
            "latest_day_amount"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest daily cost attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("cost_summary") or {}
        attrs.update(
            {
                "latest_day": summary.get("latest_day"),
                "latest_available_day": summary.get("latest_available_day"),
                "latest_available_day_complete": summary.get(
                    "latest_available_day_complete"
                ),
                "zerohero_credit": summary.get("latest_day_zerohero_credit"),
            }
        )
        return attrs


class GloBirdCalculatedCostSensor(GloBirdServiceBaseSensor):
    """Estimated cost calculated from usage intervals and a user-configured
    time-of-use rate schedule.

    GloBird's API does not expose usable $/kWh rate data (verified against
    both getProductsByAccountId and getAllProductHistoriesByAccountId, which
    only return plan name/dates/flags, no rate figures), so this is
    calculated locally from a rate schedule entered in integration options
    and the already-deduplicated per-interval usage. Stays unavailable until
    a schedule is configured.
    """

    sensor_key = "calculated_cost"
    sensor_name = "Calculated TOU Cost"
    icon = "mdi:calculator-variant"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    @property
    def available(self) -> bool:
        """Only available once a TOU rate schedule is configured and valid.

        Matches this integration's existing pattern elsewhere of trusting
        cached/stale data rather than gating on coordinator update success.
        """
        summary = self._service_detail().get("calculated_cost_summary") or {}
        return bool(summary.get("days"))

    @property
    def native_value(self) -> Any:
        """Return the latest calculated day's total cost."""
        summary = self._service_detail().get("calculated_cost_summary") or {}
        return summary.get("latest_day_cost")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return calculated cost breakdown attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("calculated_cost_summary") or {}
        attrs.update(calculated_cost_attributes(summary))
        attrs["schedule_error"] = (self.coordinator.data or {}).get(
            "tou_schedule_error"
        )
        return attrs


class GloBirdZeroHeroStatusSensor(GloBirdServiceBaseSensor):
    """Local-day ZEROHERO credit status."""

    sensor_key = "zerohero_status"
    sensor_name = "ZeroHero Status"
    icon = "mdi:check-decagram"
    device_class = SensorDeviceClass.ENUM
    _attr_options = list(ZEROHERO_STATUS_OPTIONS)

    def __init__(
        self,
        coordinator: GloBirdCoordinator,
        config_entry: ConfigEntry,
        service: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, service)
        self._zerohero_boundary_unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Schedule state refreshes at local ZEROHERO day boundaries."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_zerohero_boundary_update)
        self._schedule_zerohero_boundary_update()

    def _cancel_zerohero_boundary_update(self) -> None:
        """Cancel the pending ZEROHERO boundary update."""
        if self._zerohero_boundary_unsub is not None:
            self._zerohero_boundary_unsub()
            self._zerohero_boundary_unsub = None

    def _schedule_zerohero_boundary_update(self) -> None:
        """Schedule the next local time boundary update."""
        self._cancel_zerohero_boundary_update()
        self._zerohero_boundary_unsub = async_track_point_in_time(
            self.hass,
            self._handle_zerohero_boundary_update,
            _next_zerohero_status_boundary(dt_util.now()),
        )

    @callback
    def _handle_zerohero_boundary_update(self, _now: datetime) -> None:
        """Refresh the entity when the local day/window state changes."""
        self.async_write_ha_state()
        self._schedule_zerohero_boundary_update()

    @property
    def native_value(self) -> Any:
        """Return the latest complete ZEROHERO status."""
        summary = self._service_detail().get("cost_summary") or {}
        return _zerohero_status(summary)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return ZEROHERO detail attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("cost_summary") or {}
        now = dt_util.now()
        last_result, latest_day, latest_day_raw = _zerohero_last_result(summary)
        attrs.update(
            {
                "latest_day": summary.get("latest_day"),
                "zerohero_credit": summary.get("latest_day_zerohero_credit"),
                "latest_day_cost": summary.get("latest_day_amount"),
                "result_is_for_today": (
                    latest_day is not None and latest_day == now.date()
                ),
                "last_result": last_result,
                "last_result_day": latest_day_raw,
                "latest_available_day": summary.get("latest_available_day"),
                "latest_available_day_complete": summary.get(
                    "latest_available_day_complete"
                ),
            }
        )
        return attrs


class GloBirdExpectedMonthlyCostSensor(GloBirdServiceBaseSensor):
    """Projected cost for the current billing period."""

    sensor_key = "expected_month_cost"
    sensor_name = "Expected Monthly Cost"
    icon = "mdi:cash-clock"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    @property
    def native_value(self) -> Any:
        """Return projected billing-period cost."""
        cost_summary = self._service_detail().get("cost_summary") or {}
        projected = build_billing_period_projection(
            cost_summary.get("daily_totals"),
            _billing_period_start(self.coordinator.data or {}),
        )
        return projected.get("projected_cost")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return projected billing-period cost calculation inputs."""
        attrs = self._service_attrs()
        cost_summary = self._service_detail().get("cost_summary") or {}
        projected = build_billing_period_projection(
            cost_summary.get("daily_totals"),
            _billing_period_start(self.coordinator.data or {}),
        )
        attrs.update(projected)
        attrs["calculation"] = (
            "billing_period_cost_to_date / completed_days * period_days"
        )
        return attrs


def _billing_period_start(data: dict[str, Any]) -> date | None:
    """Return the start date of the current billing period (latest invoice issue date)."""
    dashboard = _payload_data(data.get("dashboard")) or {}
    invoice = dashboard.get("lastestInvoice") or {}
    issued = invoice.get("issuedDate")
    if not issued:
        return None
    try:
        return date.fromisoformat(str(issued).split("T")[0])
    except ValueError:
        return None


class GloBirdBillingPeriodDaysSensor(GloBirdServiceBaseSensor):
    """Number of days elapsed in the current billing period."""

    sensor_key = "billing_period_days"
    sensor_name = "Billing Period Days"
    icon = "mdi:calendar-range"

    @property
    def native_value(self) -> Any:
        """Return days of completed data since billing period start.

        Excludes today because GloBird's usage/cost data only covers
        through end of yesterday — keeps this sensor consistent with
        Billing Period Cost and the daily usage/cost sensors.
        """
        start = _billing_period_start(self.coordinator.data or {})
        if start is None:
            return None
        return _billing_period_completed_days(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return billing period attributes."""
        attrs = self._service_attrs()
        start = _billing_period_start(self.coordinator.data or {})
        attrs["billing_period_start"] = start.isoformat() if start else None
        return attrs


class GloBirdBillingPeriodCostSensor(GloBirdServiceBaseSensor):
    """Cost so far in the current billing period."""

    sensor_key = "billing_period_cost"
    sensor_name = "Billing Period Cost"
    icon = "mdi:cash-clock"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY

    @property
    def native_value(self) -> Any:
        """Return net cost since billing period start."""
        start = _billing_period_start(self.coordinator.data or {})
        cost_summary = self._service_detail().get("cost_summary") or {}
        daily_totals = cost_summary.get("daily_totals", [])
        if not daily_totals:
            return None
        if start is None:
            return cost_summary.get("total_amount")
        start_slash = start.strftime("%Y/%m/%d")
        total = sum(
            (row.get("amount") or 0.0)
            for row in daily_totals
            if str(row.get("date") or "") >= start_slash
        )
        return round(total, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return billing period cost attributes."""
        attrs = self._service_attrs()
        start = _billing_period_start(self.coordinator.data or {})
        attrs["billing_period_start"] = start.isoformat() if start else None
        return attrs


class GloBirdWeatherSummarySensor(GloBirdServiceBaseSensor):
    """Weather summary sensor."""

    sensor_key = "weather_summary"
    sensor_name = "Weather Summary"
    icon = "mdi:weather-partly-cloudy"
    native_unit_of_measurement = UnitOfTemperature.CELSIUS
    device_class = SensorDeviceClass.TEMPERATURE
    state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        """Return latest max temperature."""
        return (self._service_detail().get("weather_summary") or {}).get(
            "latest_max_temp"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return weather attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("weather_summary") or {}
        attrs.update(
            {
                "days": summary.get("days"),
                "latest_date": summary.get("latest_date"),
                "latest_min_temp": summary.get("latest_min_temp"),
                "latest_max_temp": summary.get("latest_max_temp"),
                "daily": summary.get("daily", []),
            }
        )
        return attrs
