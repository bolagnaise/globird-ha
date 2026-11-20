"""Convert cached GloBird electricity intervals into hourly energy statistics.

The portal keeps about 31 days of half-hour readings. This module turns those
readings into hourly consumption for Home Assistant external statistics. It does
not talk to the portal or the recorder.

A reading older than the cached window cannot be revised from here: once it
scrolls out of the portal payload, later imports leave the stored hour as it
was. Revisions that are still inside the cache are applied by rebuilding the
cumulative sum from the last stored hour before the cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
import re

DOMAIN = "globird_ha"

STREAM_GRID = "grid_import"
STREAM_GENERAL = "general_import"
STREAM_CONTROLLED = "controlled_import"

STREAM_LABELS = {
    STREAM_GRID: "Grid import",
    STREAM_GENERAL: "General import",
    STREAM_CONTROLLED: "Controlled load",
}

BASELINE_LOOKBACK = timedelta(days=400)
SUM_TOLERANCE = 1e-6

_STATISTIC_ID = re.compile(r"^[a-z0-9_]+(?<!_):(?!_)(?!.*__)[a-z0-9_]+(?<!_)$")


@dataclass(frozen=True)
class HourlyEnergy:
    """Consumption during one UTC hour."""

    start: datetime
    kwh: float


@dataclass(frozen=True)
class ElectricityBuild:
    """Hourly streams and non-sensitive reasons for skipped register-days."""

    streams: dict[str, list[HourlyEnergy]]
    skips: tuple[str, ...]


def safe_statistic_suffix(raw_id: Any, fallback: str) -> str:
    """Return a recorder-safe statistic id suffix.

    Same character rules as the gas statistic ids: lowercase, no repeated
    underscores, and a letter as the first character.
    """
    raw = str(raw_id or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not safe:
        safe = re.sub(r"[^a-z0-9_]+", "_", fallback.lower()).strip("_")
    if not safe:
        return "service"
    safe = re.sub(r"_+", "_", safe)
    if not safe[0].isalpha():
        safe = f"svc_{safe}"
    return safe.strip("_") or "service"


def statistic_id_for(entry_id: str, service_id: str, stream: str) -> str:
    """Return a service-scoped external statistic id."""
    suffix = safe_statistic_suffix(
        f"{entry_id}_service_{service_id}_{stream}",
        "service",
    )
    return f"{DOMAIN}:{suffix}"


def statistic_name(stream: str, *, service_count: int, service_index: int) -> str:
    """Return the Energy Dashboard label for one stream."""
    label = f"GloBird {STREAM_LABELS[stream]}"
    if service_count > 1:
        return f"{label} ({service_index + 1})"
    return label


def statistic_metadata(statistic_id: str, name: str, mean_type: Any) -> dict[str, Any]:
    """Return Home Assistant 2026.11 external-statistic metadata for energy."""
    domain, _, _object_id = statistic_id.partition(":")
    return {
        "has_sum": True,
        "mean_type": mean_type,
        "name": name,
        "source": domain,
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


def validate_external_statistic(
    metadata: Mapping[str, Any],
    points: Iterable[tuple[datetime, float]],
) -> None:
    """Reject a batch the 2026.11 recorder external-statistics API would reject.

    ``async_add_external_statistics`` requires a ``domain:object`` id whose
    source matches the domain, an aware top-of-hour timestamp, ``unit_class``,
    and ``mean_type``. Energy statistics use ``kWh`` with a sum and no mean.
    """
    statistic_id = str(metadata.get("statistic_id") or "")
    if _STATISTIC_ID.match(statistic_id) is None:
        raise ValueError(f"Invalid statistic_id: {statistic_id}")
    domain, _, _object_id = statistic_id.partition(":")
    if metadata.get("source") != domain:
        raise ValueError("Statistic source must match the statistic id domain")
    if metadata.get("unit_class") != "energy":
        raise ValueError("Electricity statistics require unit_class 'energy'")
    if metadata.get("unit_of_measurement") != "kWh":
        raise ValueError("Electricity statistics require unit kWh")
    if metadata.get("has_sum") is not True:
        raise ValueError("Electricity statistics require has_sum")
    mean_type = metadata.get("mean_type")
    mean_value = getattr(mean_type, "value", mean_type)
    if mean_value != 0:
        raise ValueError("Electricity statistics require mean_type NONE")

    previous: datetime | None = None
    for start, _total in points:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Naive timestamp: electricity statistics require a timezone")
        if start.minute or start.second or start.microsecond:
            raise ValueError("Electricity statistic timestamps must be the top of the hour")
        utc_start = start.astimezone(timezone.utc)
        if previous is not None and utc_start <= previous:
            raise ValueError("Electricity statistic timestamps must be strictly increasing")
        previous = utc_start


def electricity_statistics_enabled(options: Mapping[str, Any] | None) -> bool:
    """Return whether electricity statistics should be imported.

    Missing options keep the historical default of importing, matching gas.
    An explicit false in the options flow turns the import off.
    """
    if not isinstance(options, Mapping) or "import_electricity_statistics" not in options:
        return True
    return bool(options.get("import_electricity_statistics"))


def iter_electricity_services(
    data: Mapping[str, Any] | None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return ``(service id, usage rows)`` for non-gas services with interval rows."""
    if not isinstance(data, Mapping):
        return []
    service_data = data.get("service_data")
    if not isinstance(service_data, Mapping):
        return []

    services: list[tuple[str, list[dict[str, Any]]]] = []
    for service_id in sorted(service_data, key=str):
        detail = service_data[service_id]
        if not isinstance(detail, Mapping):
            continue
        service = detail.get("service")
        service_type = ""
        if isinstance(service, Mapping):
            service_type = str(service.get("serviceType") or "")
        if "gas" in service_type.lower():
            continue
        usage = detail.get("usage")
        rows = usage.get("data") if isinstance(usage, Mapping) else None
        if not isinstance(rows, list):
            continue
        interval_rows = [row for row in rows if isinstance(row, dict)]
        if interval_rows:
            services.append((str(service_id), interval_rows))
    return services


def build_hourly_consumption(
    rows: Iterable[Mapping[str, Any]],
    tz: Any,
    *,
    today: date | None,
) -> ElectricityBuild:
    """Aggregate portal interval rows into hourly grid, general, and controlled streams."""
    parsed, skips = _parse_register_days(rows, tz, today=today)
    if not parsed:
        return ElectricityBuild(streams={}, skips=tuple(skips))

    required: dict[str, set[tuple[str, str]]] = {
        STREAM_GENERAL: set(),
        STREAM_CONTROLLED: set(),
        STREAM_GRID: set(),
    }
    for (_day, suffix, serial), register in parsed.items():
        required[STREAM_GRID].add((suffix, serial))
        required[_stream_for_role(register.role)].add((suffix, serial))

    buckets: dict[str, dict[datetime, float]] = {
        stream: {} for stream in required if required[stream]
    }
    days = sorted({day for day, _suffix, _serial in parsed})
    for day in days:
        present = {
            (suffix, serial): register
            for (register_day, suffix, serial), register in parsed.items()
            if register_day == day
        }
        for stream, needed in required.items():
            if not needed:
                continue
            if not needed <= set(present):
                missing = sorted(suffix for suffix, _serial in needed - set(present))
                skips.append(
                    f"{day.isoformat()} {stream}: missing register {', '.join(missing)}"
                )
                continue
            for suffix, serial in needed:
                for point in present[(suffix, serial)].hours:
                    bucket = buckets[stream]
                    bucket[point.start] = bucket.get(point.start, 0.0) + point.kwh

    streams = {
        stream: [
            HourlyEnergy(start, round(kwh, 6))
            for start, kwh in sorted(hours.items())
        ]
        for stream, hours in buckets.items()
        if hours
    }
    return ElectricityBuild(streams=streams, skips=tuple(dict.fromkeys(skips)))


def plan_statistic_update(
    baseline: float,
    desired: Iterable[HourlyEnergy],
    stored_sums: Iterable[tuple[datetime, float]],
) -> list[tuple[datetime, float]] | None:
    """Return cumulative ``(start, sum)`` points to write.

    ``None`` means the stored sums already match this snapshot. Hours that are
    already stored, but absent from the current portal window, keep their
    previous consumption so a short payload cannot erase them. A newly arrived
    or corrected hour changes that hour and every later cumulative sum.
    """
    desired_map = {_as_utc(point.start): point.kwh for point in desired}
    stored = sorted((_as_utc(start), total) for start, total in stored_sums)
    stored_consumption: dict[datetime, float] = {}
    previous = baseline
    for start, total in stored:
        stored_consumption[start] = total - previous
        previous = total

    hours = sorted(set(desired_map) | set(stored_consumption))
    if not hours:
        return None

    stored_by_start = dict(stored)
    cumulative = baseline
    planned: list[tuple[datetime, float]] = []
    changed = False
    for start in hours:
        consumption = desired_map[start] if start in desired_map else stored_consumption[start]
        cumulative += consumption
        planned.append((start, cumulative))
        stored_total = stored_by_start.get(start)
        if stored_total is None or abs(stored_total - cumulative) > SUM_TOLERANCE:
            changed = True
    if not changed:
        return None
    return planned


@dataclass(frozen=True)
class _RegisterDay:
    role: str
    hours: tuple[HourlyEnergy, ...]


def _parse_register_days(
    rows: Iterable[Mapping[str, Any]],
    tz: Any,
    *,
    today: date | None,
) -> tuple[dict[tuple[date, str, str], _RegisterDay], list[str]]:
    grouped: dict[tuple[date, str, str, str], list[Mapping[str, Any]]] = {}
    skips: list[str] = []
    for row in rows:
        role = _register_role(row)
        if role is None:
            continue
        parsed_day = _parse_day(row.get("readDate"))
        suffix = str(row.get("suffix") or "").strip().upper()
        if parsed_day is None or not suffix:
            continue
        if today is not None and parsed_day >= today:
            skips.append(f"{parsed_day.isoformat()} {suffix}: local day is not complete")
            continue
        quality = str(row.get("minQualityMethod") or "").strip().upper()
        if quality == "E":
            skips.append(f"{parsed_day.isoformat()} {suffix}: estimated reading")
            continue
        serial = str(row.get("serial") or "").strip()
        grouped.setdefault((parsed_day, suffix, serial, role), []).append(row)

    parsed: dict[tuple[date, str, str], _RegisterDay] = {}
    for (day, suffix, serial, role), group in grouped.items():
        arrays: list[list[float]] = []
        rejected = False
        for row in group:
            values = _usage_array(row.get("usageArray"))
            label = f"{day.isoformat()} {suffix}"
            if values is None:
                skips.append(f"{label}: non-numeric interval")
                rejected = True
                break
            if any(value < 0 for value in values):
                skips.append(f"{label}: negative interval")
                rejected = True
                break
            arrays.append(values)
        if rejected or not arrays:
            continue
        if any(candidate != arrays[0] for candidate in arrays[1:]):
            skips.append(
                f"{day.isoformat()} {suffix}: time-of-use rows have different interval arrays"
            )
            continue
        expected = expected_interval_count(day, tz)
        if len(arrays[0]) != expected:
            skips.append(
                f"{day.isoformat()} {suffix}: {len(arrays[0])} intervals for a {expected}-interval day"
            )
            continue
        hours = _intervals_to_hours(day, arrays[0], tz)
        if hours is None:
            skips.append(f"{day.isoformat()} {suffix}: interval timestamps are ambiguous")
            continue
        register_key = (day, suffix, serial)
        if register_key in parsed and parsed[register_key].role != role:
            skips.append(f"{day.isoformat()} {suffix}: conflicting register classification")
            parsed.pop(register_key)
            continue
        parsed[register_key] = _RegisterDay(role=role, hours=tuple(hours))
    return parsed, skips


def expected_interval_count(day: date, tz: Any) -> int:
    """Return 48, 46, or 50 half-hours for a local civil day."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    next_day = day + timedelta(days=1)
    end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    delta = end.utcoffset() - start.utcoffset()
    if delta == timedelta(0):
        return 48
    if delta == timedelta(hours=1):
        return 46
    if delta == timedelta(hours=-1):
        return 50
    return -1


def _local_half_hours(day: date, tz: Any) -> list[datetime]:
    """Return civil half-hour starts in portal array order.

    Spring-forward days omit the missing 02:00 hour. Autumn fallback days emit
    02:00 twice, first at the pre-transition offset and then at the
    post-transition offset. Adding ``timedelta`` is not used: across the
    spring gap it creates a wall time whose UTC instant collides with 03:00.
    """
    count = expected_interval_count(day, tz)

    def at(hour: int, minute: int, fold: int = 0) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=fold)

    slots: list[datetime] = []
    if count == 48:
        for hour in range(24):
            slots.append(at(hour, 0))
            slots.append(at(hour, 30))
        return slots
    if count == 46:
        for hour in range(24):
            if hour == 2:
                continue
            slots.append(at(hour, 0))
            slots.append(at(hour, 30))
        return slots
    if count == 50:
        for hour in range(24):
            if hour == 2:
                slots.extend((at(2, 0, 0), at(2, 30, 0), at(2, 0, 1), at(2, 30, 1)))
                continue
            slots.append(at(hour, 0))
            slots.append(at(hour, 30))
        return slots
    return []


def _intervals_to_hours(
    day: date,
    values: list[float],
    tz: Any,
) -> list[HourlyEnergy] | None:
    slots = _local_half_hours(day, tz)
    if len(slots) != len(values):
        return None
    totals: dict[datetime, float] = {}
    for slot, value in zip(slots, values):
        hour = slot.replace(minute=0, second=0, microsecond=0)
        utc_hour = hour.astimezone(timezone.utc)
        if utc_hour.minute or utc_hour.second or utc_hour.microsecond:
            return None
        totals[utc_hour] = totals.get(utc_hour, 0.0) + float(value)
    distinct = len(totals)
    if distinct not in {23, 24, 25}:
        return None
    if len(values) == 48 and distinct != 24:
        return None
    if len(values) == 46 and distinct != 23:
        return None
    if len(values) == 50 and distinct != 25:
        return None
    return [HourlyEnergy(start, kwh) for start, kwh in sorted(totals.items())]


def _register_role(row: Mapping[str, Any]) -> str | None:
    """Return ``general``, ``controlled``, or None for export and unknown rows."""
    suffix = str(row.get("suffix") or "").strip().upper()
    if not suffix or suffix.startswith("B"):
        return None
    category = str(row.get("chargeCategoryCode") or "").strip().upper()
    charge_type = str(row.get("chargeType") or "").strip().lower()
    export_markers = ("export", "feed-in", "feed in")
    if any(marker in category.lower() for marker in export_markers) or any(
        marker in charge_type for marker in export_markers
    ):
        return None
    if category in {"CONTROL", "CONTROLLED_LOAD"} or suffix == "E2":
        return "controlled"
    return "general"


def _stream_for_role(role: str) -> str:
    if role == "controlled":
        return STREAM_CONTROLLED
    return STREAM_GENERAL


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    raw = str(value).split("T", 1)[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _usage_array(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Naive timestamp")
    return value.astimezone(timezone.utc)
