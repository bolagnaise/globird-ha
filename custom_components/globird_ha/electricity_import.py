"""Import cached GloBird electricity intervals into recorder statistics.

Gas statistics stay in ``sensor.py``. A failure here is logged and does not
affect sensor updates or the gas importer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .electricity_statistics import (
    BASELINE_LOOKBACK,
    STREAM_LABELS,
    ElectricityBuild,
    HourlyEnergy,
    build_hourly_consumption,
    electricity_statistics_enabled,
    iter_electricity_services,
    plan_statistic_update,
    statistic_id_for,
    statistic_metadata,
    statistic_name,
    validate_external_statistic,
)

_LOGGER = logging.getLogger(__name__)


def async_setup_electricity_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Any,
) -> Any:
    """Subscribe to cached coordinator data and return an unsubscribe callback."""
    lock = asyncio.Lock()

    async def _import() -> None:
        if not electricity_statistics_enabled(getattr(entry, "options", None)):
            _LOGGER.debug("Electricity statistics import is disabled")
            return
        async with lock:
            try:
                await async_import_coordinator_data(hass, entry, coordinator.data)
            except Exception:  # noqa: BLE001 - keep sensor and gas updates alive.
                _LOGGER.exception("Electricity statistics import failed")

    def _schedule(*_args: Any) -> None:
        hass.async_create_task(_import())

    unsubscribe = coordinator.async_add_listener(_schedule)
    _schedule()
    return unsubscribe


async def async_import_coordinator_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: dict[str, Any] | None,
) -> None:
    """Publish electricity statistics for every cached electricity service."""
    tz = dt_util.get_time_zone(hass.config.time_zone)
    if tz is None:
        _LOGGER.warning(
            "Electricity statistics skipped because the Home Assistant timezone is unavailable"
        )
        return

    services = iter_electricity_services(data)
    today = datetime.now(tz).date()
    for index, (service_id, rows) in enumerate(services):
        try:
            built = build_hourly_consumption(rows, tz, today=today)
        except Exception:  # noqa: BLE001 - one service must not block another.
            _LOGGER.exception("Electricity statistics could not be converted")
            continue
        for skip in built.skips:
            _LOGGER.warning("Skipped electricity intervals: %s", skip)
        await _async_publish_service(
            hass,
            entry_id=entry.entry_id,
            service_id=service_id,
            service_index=index,
            service_count=len(services),
            built=built,
        )


async def _async_publish_service(
    hass: HomeAssistant,
    *,
    entry_id: str,
    service_id: str,
    service_index: int,
    service_count: int,
    built: ElectricityBuild,
) -> None:
    for stream in STREAM_LABELS:
        desired = built.streams.get(stream) or []
        if not desired:
            continue
        statistic_id = statistic_id_for(entry_id, service_id, stream)
        metadata = statistic_metadata(
            statistic_id,
            statistic_name(
                stream,
                service_count=service_count,
                service_index=service_index,
            ),
            _mean_type_none(),
        )
        try:
            wrote = await _async_publish_stream(hass, metadata, desired)
        except Exception:  # noqa: BLE001 - leave the other streams and gas untouched.
            _LOGGER.exception(
                "Electricity statistics import failed for %s",
                stream,
            )
            continue
        if wrote:
            _LOGGER.info(
                "Imported %s electricity statistics: %d hourly points from %s through %s",
                stream,
                len(desired),
                desired[0].start.isoformat(),
                desired[-1].start.isoformat(),
            )
        else:
            _LOGGER.debug("Electricity statistics for %s are unchanged", stream)


async def _async_publish_stream(
    hass: HomeAssistant,
    metadata: dict[str, Any],
    desired: list[HourlyEnergy],
) -> bool:
    statistic_id = str(metadata["statistic_id"])
    window_start = desired[0].start
    window_end = desired[-1].start + timedelta(hours=1)
    stored = await _async_read_sums(hass, statistic_id, window_start, window_end)
    prior = await _async_read_sums(
        hass,
        statistic_id,
        window_start - BASELINE_LOOKBACK,
        window_start,
    )
    baseline = prior[-1][1] if prior else 0.0
    planned = plan_statistic_update(baseline, desired, stored)
    if planned is None:
        return False
    validate_external_statistic(metadata, planned)
    _write_statistics(hass, metadata, planned)
    return True


async def _async_read_sums(
    hass: HomeAssistant,
    statistic_id: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    """Read hourly sums for ``start <= timestamp < end``.

    The recorder instance executor is required. Calling
    ``statistics_during_period`` directly makes Home Assistant report that the
    integration accessed the database outside that executor.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = result.get(statistic_id, [])
    points: list[tuple[datetime, float]] = []
    for row in rows:
        start_at = row["start"]
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        total = row.get("sum")
        if total is None:
            continue
        points.append((start_at.astimezone(timezone.utc), float(total)))
    return points


def _write_statistics(
    hass: HomeAssistant,
    metadata: dict[str, Any],
    planned: list[tuple[datetime, float]],
) -> None:
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    async_add_external_statistics(
        hass,
        metadata,
        [
            StatisticData(start=start, state=total, sum=total)
            for start, total in planned
        ],
    )


def _mean_type_none() -> Any:
    from homeassistant.components.recorder.models import StatisticMeanType

    return StatisticMeanType.NONE


__all__ = [
    "async_import_coordinator_data",
    "async_setup_electricity_statistics",
]
