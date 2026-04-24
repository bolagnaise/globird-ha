"""Sensor entities for GloBird HA."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import service_id
from .const import DOMAIN
from .coordinator import GloBirdCoordinator

CURRENCY_AUD = "AUD"


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
    return balance.get("balance")


def _balance_attrs(data: dict[str, Any]) -> dict[str, Any]:
    balance = _payload_data(data.get("balance")) or {}
    return {
        "max_refundable_amount": balance.get("maxRefundableAmount"),
        "show_refundable_amount": balance.get("showRefundableAmount"),
    }


def _dashboard_balance_value(data: dict[str, Any]) -> Any:
    dashboard = _payload_data(data.get("dashboard")) or {}
    return dashboard.get("currentBalance")


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
        entities.extend(
            [
                GloBirdServiceStatusSensor(coordinator, config_entry, service),
                GloBirdMeterInfoSensor(coordinator, config_entry, service),
                GloBirdUsageTotalSensor(coordinator, config_entry, service),
                GloBirdLatestDayUsageSensor(coordinator, config_entry, service),
                GloBirdSolarExportTotalSensor(coordinator, config_entry, service),
                GloBirdLatestDaySolarExportSensor(coordinator, config_entry, service),
                GloBirdCostTotalSensor(coordinator, config_entry, service),
                GloBirdLatestDayCostSensor(coordinator, config_entry, service),
                GloBirdWeatherSummarySensor(coordinator, config_entry, service),
            ]
        )

    async_add_entities(entities)


class GloBirdBaseSensor(CoordinatorEntity[GloBirdCoordinator], SensorEntity):
    """Base class for GloBird sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GloBirdCoordinator, config_entry: ConfigEntry) -> None:
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
        attrs["meter"] = self._service_detail().get("meter")
        return attrs


class GloBirdUsageTotalSensor(GloBirdServiceBaseSensor):
    """Recent usage total sensor."""

    sensor_key = "usage_total"
    sensor_name = "Recent Usage Total"
    icon = "mdi:lightning-bolt"
    native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL

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
            {
                "days": summary.get("days"),
                "latest_day": summary.get("latest_day"),
                "daily": summary.get("daily", []),
            }
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
            {
                "latest_day": summary.get("latest_day"),
                "latest_intervals": summary.get("latest_intervals", []),
            }
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
        attrs.update(
            {
                "days": summary.get("days"),
                "latest_day": summary.get("latest_day"),
                "daily": summary.get("export_daily", []),
            }
        )
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
        return (self._service_detail().get("usage_summary") or {}).get("latest_day_export")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest day attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("usage_summary") or {}
        attrs.update(
            {
                "latest_day": summary.get("latest_day"),
            }
        )
        return attrs


class GloBirdCostTotalSensor(GloBirdServiceBaseSensor):
    """Recent cost total sensor."""

    sensor_key = "cost_total"
    sensor_name = "Recent Cost Total"
    icon = "mdi:cash-multiple"
    native_unit_of_measurement = CURRENCY_AUD
    device_class = SensorDeviceClass.MONETARY
    state_class = None

    @property
    def native_value(self) -> Any:
        """Return total recent cost."""
        return (self._service_detail().get("cost_summary") or {}).get("total_amount")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return cost attributes."""
        attrs = self._service_attrs()
        summary = self._service_detail().get("cost_summary") or {}
        attrs.update(
            {
                "days": summary.get("days"),
                "total_quantity": summary.get("total_quantity"),
                "latest_day": summary.get("latest_day"),
                "daily": summary.get("daily", []),
            }
        )
        return attrs


class GloBirdLatestDayCostSensor(GloBirdServiceBaseSensor):
    """Latest daily cost sensor."""

    sensor_key = "latest_day_cost"
    sensor_name = "Latest Daily Cost"
    icon = "mdi:calendar-cash"
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
        attrs["latest_day"] = summary.get("latest_day")
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
