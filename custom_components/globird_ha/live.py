"""Live tariff runtime: price boundaries and today's cost from grid meters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_RATE_OVERRIDES,
    CONF_SUPER_EXPORT_CAP_KWH,
    CONF_ZEROHERO_KWH_PER_HOUR,
    DOMAIN,
    STORAGE_VERSION,
)
from .tariff import LiveCostTracker, apply_overrides, next_boundary

_LOGGER = logging.getLogger(__name__)

# Energy used while Home Assistant was down cannot be placed in the right
# tariff window, so after a longer gap the tracker re-seeds instead of guessing.
MAX_READING_GAP = timedelta(minutes=15)

# Meters can report every few seconds; publish estimate changes at most this
# often so the recorder is not flooded. Price boundaries publish immediately.
METER_PUBLISH_INTERVAL = timedelta(seconds=30)

_ENERGY_UNIT_FACTORS = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}


def _float_option(options: dict[str, Any], key: str) -> float | None:
    value = options.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GloBirdLiveTariff:
    """Per-service live tariff state shared by the price and estimate sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: Any,
        entry: ConfigEntry,
        service_id: str,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.entry = entry
        self.service_id = service_id
        options = dict(getattr(entry, "options", {}) or {})
        self.options = options
        self.import_entity: str | None = options.get(CONF_GRID_IMPORT_ENTITY) or None
        self.export_entity: str | None = options.get(CONF_GRID_EXPORT_ENTITY) or None
        self.tracker = LiveCostTracker()
        self.reading_gaps = 0
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._boundary_unsub: Callable[[], None] | None = None
        self._publish_unsub: Callable[[], None] | None = None
        self._last_publish: datetime | None = None
        self._tariff_source_id: int | None = None
        self._tariff: dict[str, Any] | None = None
        self._store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.live.{entry.entry_id}.{service_id}"
        )

    @property
    def tracks_import(self) -> bool:
        return bool(self.import_entity)

    @property
    def tracks_export(self) -> bool:
        return bool(self.export_entity)

    @property
    def tariff(self) -> dict[str, Any] | None:
        """Return the inferred tariff with user overrides applied."""
        service_data = (self.coordinator.data or {}).get("service_data") or {}
        detail = service_data.get(self.service_id) or {}
        inferred = detail.get("tariff") if isinstance(detail, dict) else None
        if not isinstance(inferred, dict) or not inferred.get("components"):
            return None
        if id(inferred) != self._tariff_source_id:
            overrides = self.options.get(CONF_RATE_OVERRIDES)
            self._tariff = apply_overrides(
                inferred,
                overrides if isinstance(overrides, dict) else None,
                super_export_cap_kwh=_float_option(
                    self.options, CONF_SUPER_EXPORT_CAP_KWH
                ),
                zerohero_kwh_per_hour=_float_option(
                    self.options, CONF_ZEROHERO_KWH_PER_HOUR
                ),
            )
            self._tariff_source_id = id(inferred)
        return self._tariff

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        if self._publish_unsub is not None:
            self._publish_unsub()
            self._publish_unsub = None
        self._last_publish = dt_util.utcnow()
        for listener in list(self._listeners):
            listener()

    @callback
    def _notify_throttled(self) -> None:
        if self._publish_unsub is not None:
            return
        now = dt_util.utcnow()
        if self._last_publish is None or now - self._last_publish >= METER_PUBLISH_INTERVAL:
            self._notify()
            return
        delay = (METER_PUBLISH_INTERVAL - (now - self._last_publish)).total_seconds()
        self._publish_unsub = async_call_later(self.hass, delay, self._deferred_publish)

    @callback
    def _deferred_publish(self, _now: datetime) -> None:
        self._publish_unsub = None
        self._notify()

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self.tracker = LiveCostTracker(stored.get("tracker"))
            self.reading_gaps = int(stored.get("reading_gaps") or 0)
            saved_at = dt_util.parse_datetime(str(stored.get("saved_at") or ""))
            if saved_at is None or dt_util.now() - saved_at > MAX_READING_GAP:
                if self.tracker.last_import is not None or self.tracker.last_export is not None:
                    self.reading_gaps += 1
                self.tracker.last_import = None
                self.tracker.last_export = None
        entities = [e for e in (self.import_entity, self.export_entity) if e]
        if entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, entities, self._handle_meter_event
                )
            )
            self._read_meters(dt_util.now())
        self._unsubs.append(self.coordinator.async_add_listener(self._on_coordinator))
        self._schedule_boundary()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None
        if self._publish_unsub is not None:
            self._publish_unsub()
            self._publish_unsub = None
        await self._store.async_save(self._store_data())

    def _store_data(self) -> dict[str, Any]:
        return {
            "tracker": self.tracker.as_dict(),
            "reading_gaps": self.reading_gaps,
            "saved_at": dt_util.now().isoformat(),
        }

    @callback
    def _on_coordinator(self) -> None:
        self._schedule_boundary()

    @callback
    def _schedule_boundary(self) -> None:
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None
        tariff = self.tariff
        now = dt_util.now()
        target = (
            next_boundary(tariff, now)
            if tariff
            else (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        )
        self._boundary_unsub = async_track_point_in_time(
            self.hass, self._handle_boundary, target
        )

    @callback
    def _handle_boundary(self, now: datetime) -> None:
        self._boundary_unsub = None
        tariff = self.tariff
        if tariff is not None:
            # Rolls the day at midnight even if no meter reading arrives.
            self.tracker.update(tariff, dt_util.now())
        self._notify()
        self._schedule_boundary()

    @staticmethod
    def _reading(state: Any) -> float | None:
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement") or "kWh"
        return value * _ENERGY_UNIT_FACTORS.get(unit, 1.0)

    @callback
    def _handle_meter_event(self, _event: Any) -> None:
        self._read_meters(dt_util.now())

    @callback
    def _read_meters(self, now: datetime) -> None:
        tariff = self.tariff
        if tariff is None:
            return
        import_reading = (
            self._reading(self.hass.states.get(self.import_entity))
            if self.import_entity
            else None
        )
        export_reading = (
            self._reading(self.hass.states.get(self.export_entity))
            if self.export_entity
            else None
        )
        if import_reading is None and export_reading is None:
            return
        self.tracker.update(tariff, now, import_reading, export_reading)
        self._store.async_delay_save(self._store_data, 60)
        self._notify_throttled()
