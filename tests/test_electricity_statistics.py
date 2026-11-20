"""Tests for hourly GloBird electricity statistics.

Fixtures are synthetic. They reconcile to the published reference totals without
storing a real account's interval profile.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components"
INTEGRATION_PATH = COMPONENT_PATH / "globird_ha"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENT_PATH)]  # type: ignore[attr-defined]
globird_package = types.ModuleType("custom_components.globird_ha")
globird_package.__path__ = [str(INTEGRATION_PATH)]  # type: ignore[attr-defined]
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.globird_ha", globird_package)

stats = importlib.import_module("custom_components.globird_ha.electricity_statistics")

SYDNEY = ZoneInfo("Australia/Sydney")
UTC = timezone.utc
SCALE = 1_000_000
E1_TOTAL = 620.469
E2_TOTAL = 264.326
E1_NOV8 = 20.3986
E2_NOV8 = 5.4498
WINDOW_START = date(2026, 10, 9)
WINDOW_DAYS = [WINDOW_START + timedelta(days=offset) for offset in range(31)]


def _units(total_kwh: float) -> int:
    return round(total_kwh * SCALE)


def _array_from_units(total_units: int, head: list[float] | None = None) -> list[float]:
    """Return 48 interval values whose scaled sum is exact."""
    head_units = [round(value * SCALE) for value in head or []]
    remain = total_units - sum(head_units)
    slots = 48 - len(head_units)
    base, extra = divmod(remain, slots)
    tail = [base + (1 if index < extra else 0) for index in range(slots)]
    units = head_units + tail
    units[-1] += total_units - sum(units)
    return [unit / SCALE for unit in units]


def _row(
    day: date,
    suffix: str,
    values: list[float],
    *,
    category: str,
    charge_type: str,
    quality: str = "A",
    serial: str = "meter",
) -> dict[str, Any]:
    return {
        "readDate": day.strftime("%Y/%m/%d"),
        "suffix": suffix,
        "chargeCategoryCode": category,
        "chargeType": charge_type,
        "minQualityMethod": quality,
        "serial": serial,
        "usageArray": values,
        "usage": round(sum(values), 6),
    }


def _reference_rows() -> list[dict[str, Any]]:
    """Build 31 synthetic days that reconcile to the reference totals."""
    e1_other = _units(E1_TOTAL) - _units(E1_NOV8)
    e2_other = _units(E2_TOTAL) - _units(E2_NOV8)
    e1_base, e1_extra = divmod(e1_other, 30)
    e2_base, e2_extra = divmod(e2_other, 30)
    rows: list[dict[str, Any]] = []
    ordinary_index = 0
    for day in WINDOW_DAYS:
        if day == date(2026, 11, 8):
            e1_values = _array_from_units(_units(E1_NOV8), [1.0499, 1.4124])
            e2_values = _array_from_units(_units(E2_NOV8), [0.5, 0.5])
            quality = "A"
        else:
            e1_values = _array_from_units(e1_base + (1 if ordinary_index < e1_extra else 0))
            e2_values = _array_from_units(e2_base + (1 if ordinary_index < e2_extra else 0))
            quality = "S" if day == date(2026, 10, 19) else "A"
            ordinary_index += 1
        rows.append(
            _row(
                day,
                "E1",
                e1_values,
                category="USAGE",
                charge_type="Peak Usage",
                quality=quality,
            )
        )
        rows.append(
            _row(
                day,
                "E2",
                e2_values,
                category="CONTROL",
                charge_type="Controlled Load",
                quality=quality,
            )
        )
    return rows


def _build(rows: list[dict[str, Any]], *, today: date | None = None) -> Any:
    return stats.build_hourly_consumption(rows, SYDNEY, today=today or date(2026, 11, 9))


def _kwh(points: list[Any]) -> float:
    return sum(point.kwh for point in points)


def _on(points: list[Any], day: date) -> list[Any]:
    return [point for point in points if point.start.astimezone(SYDNEY).date() == day]


def test_reference_window_reconciles_hourly_streams() -> None:
    """Thirty-one ordinary days become 744 hours with the published totals."""
    built = _build(_reference_rows())
    grid = built.streams[stats.STREAM_GRID]
    general = built.streams[stats.STREAM_GENERAL]
    controlled = built.streams[stats.STREAM_CONTROLLED]

    assert len(grid) == 744
    assert len(general) == 744
    assert len(controlled) == 744
    assert abs(_kwh(general) - E1_TOTAL) < 1e-6
    assert abs(_kwh(controlled) - E2_TOTAL) < 1e-6
    assert abs(_kwh(grid) - (E1_TOTAL + E2_TOTAL)) < 1e-6

    nov8 = date(2026, 11, 8)
    assert abs(_kwh(_on(general, nov8)) - E1_NOV8) < 1e-6
    assert abs(_kwh(_on(controlled, nov8)) - E2_NOV8) < 1e-6
    assert abs(_kwh(_on(grid, nov8)) - (E1_NOV8 + E2_NOV8)) < 1e-6
    first_hour = datetime(2026, 11, 8, tzinfo=SYDNEY).astimezone(UTC)
    assert abs(next(point.kwh for point in grid if point.start == first_hour) - 3.4623) < 1e-6
    assert len(_on(grid, date(2026, 10, 19))) == 24
    assert all("account" not in skip.lower() for skip in built.skips)


def test_substituted_reading_can_be_corrected_without_new_hours() -> None:
    """A substituted day is imported, then a later actual reading rewrites later sums."""
    day = date(2026, 10, 19)
    original = [
        _row(day, "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage", quality="S"),
        _row(day, "E2", [0.5] * 48, category="CONTROL", charge_type="Controlled Load", quality="S"),
    ]
    corrected = [
        _row(day, "E1", [2.0] * 48, category="USAGE", charge_type="Peak Usage", quality="A"),
        _row(day, "E2", [0.5] * 48, category="CONTROL", charge_type="Controlled Load", quality="A"),
    ]
    first = _build(original)
    second = _build(corrected)
    first_sum = stats.plan_statistic_update(0.0, first.streams[stats.STREAM_GRID], [])
    assert first_sum is not None
    assert len(first_sum) == 24
    revised = stats.plan_statistic_update(0.0, second.streams[stats.STREAM_GRID], first_sum)
    assert revised is not None
    assert len(revised) == len(first_sum)
    assert abs(revised[-1][1] - first_sum[-1][1] - 48.0) < 1e-6
    assert stats.plan_statistic_update(0.0, second.streams[stats.STREAM_GRID], revised) is None


def test_repeated_plan_and_restart_do_not_double_count() -> None:
    """The cumulative sum lives in the stored statistics, not in process memory."""
    built = _build(
        [
            _row(
                date(2026, 11, 1),
                "E1",
                [1.0] * 48,
                category="USAGE",
                charge_type="Peak Usage",
            )
        ]
    )
    desired = built.streams[stats.STREAM_GRID]
    stored: list[tuple[datetime, float]] = []
    first = stats.plan_statistic_update(0.0, desired, stored)
    assert first is not None
    assert abs(first[-1][1] - 48.0) < 1e-6
    assert stats.plan_statistic_update(0.0, desired, first) is None
    restarted = stats.plan_statistic_update(0.0, desired, list(first))
    assert restarted is None


def test_missing_middle_day_arrives_later_and_adjusts_later_sums() -> None:
    """A hole stays open until the portal publishes it, then later sums move."""
    days = [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]
    rows = [
        _row(day, "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage")
        for day in days
    ]
    partial = _build([row for row in rows if row["readDate"] != "2026/11/02"])
    complete = _build(rows)
    partial_sum = stats.plan_statistic_update(0.0, partial.streams[stats.STREAM_GRID], [])
    assert partial_sum is not None
    assert len(partial_sum) == 48
    assert abs(partial_sum[-1][1] - 96.0) < 1e-6
    filled = stats.plan_statistic_update(
        0.0,
        complete.streams[stats.STREAM_GRID],
        partial_sum,
    )
    assert filled is not None
    assert len(filled) == 72
    assert abs(filled[-1][1] - 144.0) < 1e-6
    assert len({start for start, _total in filled}) == 72


def test_history_outside_the_portal_window_is_retained() -> None:
    """Days that have scrolled out of the cache keep their last imported sum."""
    old_start = datetime(2026, 9, 1, tzinfo=SYDNEY).astimezone(UTC)
    stored = [(old_start, 100.0)]
    built = _build(
        [
            _row(
                date(2026, 11, 1),
                "E1",
                [1.0] * 48,
                category="USAGE",
                charge_type="Peak Usage",
            )
        ]
    )
    planned = stats.plan_statistic_update(100.0, built.streams[stats.STREAM_GRID], [])
    assert planned is not None
    assert all(start > old_start for start, _total in planned)
    assert abs(planned[0][1] - 102.0) < 1e-6
    assert abs(planned[-1][1] - 148.0) < 1e-6
    retained = stats.plan_statistic_update(100.0, built.streams[stats.STREAM_GRID], planned)
    assert retained is None
    assert stored == [(old_start, 100.0)]


def test_spring_forward_has_23_unique_utc_hours_and_keeps_the_daily_sum() -> None:
    """The missing 02:00 hour is omitted instead of colliding with 03:00."""
    day = date(2026, 10, 4)
    assert stats.expected_interval_count(day, SYDNEY) == 46
    built = _build(
        [
            _row(
                day,
                "E1",
                [1.0] * 46,
                category="USAGE",
                charge_type="Peak Usage",
            )
        ]
    )
    points = built.streams[stats.STREAM_GRID]
    local_hours = [point.start.astimezone(SYDNEY).hour for point in points]
    assert len(points) == 23
    assert len({point.start for point in points}) == 23
    assert abs(_kwh(points) - 46.0) < 1e-9
    assert 2 not in local_hours
    assert 1 in local_hours and 3 in local_hours


def test_autumn_fallback_has_25_unique_utc_hours_and_keeps_the_daily_sum() -> None:
    """The repeated 02:00 hour is stored as two different UTC hours."""
    day = date(2026, 4, 5)
    assert stats.expected_interval_count(day, SYDNEY) == 50
    built = _build(
        [
            _row(
                day,
                "E1",
                [1.0] * 50,
                category="USAGE",
                charge_type="Peak Usage",
            )
        ]
    )
    points = built.streams[stats.STREAM_GRID]
    repeated = [point for point in points if point.start.astimezone(SYDNEY).hour == 2]
    assert len(points) == 25
    assert len({point.start for point in points}) == 25
    assert len(repeated) == 2
    assert repeated[0].start != repeated[1].start
    assert abs(_kwh(points) - 50.0) < 1e-9
    assert abs(repeated[0].kwh - 2.0) < 1e-9
    assert abs(repeated[1].kwh - 2.0) < 1e-9


def test_duplicate_full_day_arrays_are_counted_once_and_conflicts_are_skipped() -> None:
    """Time-of-use rows repeat one register array. Disagreeing copies are not summed."""
    day = date(2026, 11, 1)
    values = [1.0] * 48
    duplicate = _build(
        [
            _row(day, "E1", values, category="USAGE", charge_type="Peak Usage"),
            _row(day, "E1", list(values), category="USAGE", charge_type="Off Peak"),
            _row(day, "E3", [0.25] * 48, category="USAGE", charge_type="Shoulder", serial="other"),
        ]
    )
    assert abs(_kwh(duplicate.streams[stats.STREAM_GENERAL]) - 60.0) < 1e-6
    assert stats.STREAM_CONTROLLED not in duplicate.streams

    conflict = _build(
        [
            _row(day, "E1", values, category="USAGE", charge_type="Peak Usage"),
            _row(day, "E1", [2.0] * 48, category="USAGE", charge_type="Off Peak"),
        ]
    )
    assert conflict.streams == {}
    assert any("different interval arrays" in skip for skip in conflict.skips)


def test_export_negative_short_and_current_days_are_not_invented() -> None:
    """Missing or unusable intervals are skipped rather than padded."""
    day = date(2026, 11, 1)
    built = _build(
        [
            _row(day, "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage"),
            _row(day, "B1", [4.0] * 48, category="SOLAR", charge_type="Solar Feed In"),
            _row(date(2026, 11, 2), "E1", [1.0] * 47, category="USAGE", charge_type="Peak Usage"),
            _row(date(2026, 11, 3), "E1", [-1.0] + [1.0] * 47, category="USAGE", charge_type="Peak Usage"),
            _row(date(2026, 11, 9), "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage"),
        ],
        today=date(2026, 11, 9),
    )
    assert abs(_kwh(built.streams[stats.STREAM_GRID]) - 48.0) < 1e-6
    assert len(built.streams[stats.STREAM_GRID]) == 24
    assert any("47 intervals" in skip for skip in built.skips)
    assert any("negative interval" in skip for skip in built.skips)
    assert any("not complete" in skip for skip in built.skips)


def test_estimated_reading_is_deferred_until_it_is_replaced() -> None:
    """An estimated day is not frozen; a later actual array can take its place."""
    day = date(2026, 11, 1)
    estimated = _build(
        [_row(day, "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage", quality="E")]
    )
    assert estimated.streams == {}
    actual = _build(
        [_row(day, "E1", [1.0] * 48, category="USAGE", charge_type="Peak Usage", quality="A")]
    )
    assert len(actual.streams[stats.STREAM_GRID]) == 24


def test_metadata_matches_the_2026_11_external_statistics_contract() -> None:
    """Metadata and timestamps follow the recorder rules current in Home Assistant 2026.11."""
    statistic_id = stats.statistic_id_for("entry-1", "100", stats.STREAM_GRID)
    metadata = stats.statistic_metadata(statistic_id, "GloBird Grid import", 0)
    start = datetime(2026, 11, 1, 13, tzinfo=UTC)
    stats.validate_external_statistic(metadata, [(start, 1.0)])
    assert metadata["source"] == "globird_ha"
    assert metadata["unit_class"] == "energy"
    assert metadata["unit_of_measurement"] == "kWh"
    assert metadata["has_sum"] is True
    assert statistic_id.startswith("globird_ha:")
    assert "__" not in statistic_id

    try:
        stats.validate_external_statistic(metadata, [(start.replace(minute=30), 1.0)])
    except ValueError as err:
        assert "top of the hour" in str(err)
    else:
        raise AssertionError("half-hour timestamps must be rejected")

    arithmetic = dict(metadata)
    arithmetic["mean_type"] = 1
    try:
        stats.validate_external_statistic(arithmetic, [(start, 1.0)])
    except ValueError as err:
        assert "mean_type" in str(err)
    else:
        raise AssertionError("a mean statistic must be rejected")


def test_service_walker_ignores_gas_and_keeps_multiple_electricity_services() -> None:
    """Gas history stays with the gas importer. Each electricity service is separate."""
    data = {
        "service_data": {
            "gas": {
                "service": {"serviceType": "Gas"},
                "usage": {"data": {"reads": [{"readIndex": 10}]}},
            },
            "power-b": {
                "service": {"serviceType": "Power"},
                "usage": {
                    "data": [
                        _row(
                            date(2026, 11, 1),
                            "E2",
                            [1.0] * 48,
                            category="CONTROL",
                            charge_type="Controlled Load",
                        )
                    ]
                },
            },
            "power-a": {
                "service": {"serviceType": "Power"},
                "usage": {
                    "data": [
                        _row(
                            date(2026, 11, 1),
                            "E1",
                            [1.0] * 48,
                            category="USAGE",
                            charge_type="Peak Usage",
                        )
                    ]
                },
            },
        }
    }
    services = stats.iter_electricity_services(data)
    assert [service_id for service_id, _rows in services] == ["power-a", "power-b"]
    assert stats.electricity_statistics_enabled({}) is True
    assert stats.electricity_statistics_enabled({"import_electricity_statistics": False}) is False
    assert stats.statistic_name(stats.STREAM_GRID, service_count=1, service_index=0) == (
        "GloBird Grid import"
    )
    assert stats.statistic_name(stats.STREAM_CONTROLLED, service_count=2, service_index=1) == (
        "GloBird Controlled load (2)"
    )
