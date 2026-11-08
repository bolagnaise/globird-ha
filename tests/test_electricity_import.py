"""Recorder adapter tests for electricity statistics.

The recorder stand-in accepts the Home Assistant 2026.11 call shape:
``statistics_during_period(..., "hour", None, {"sum"})`` and
``async_add_external_statistics`` with energy metadata.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components"
INTEGRATION_PATH = COMPONENT_PATH / "globird_ha"
SYDNEY = ZoneInfo("Australia/Sydney")
UTC = timezone.utc

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENT_PATH)]  # type: ignore[attr-defined]
globird_package = types.ModuleType("custom_components.globird_ha")
globird_package.__path__ = [str(INTEGRATION_PATH)]  # type: ignore[attr-defined]
sys.modules["custom_components"] = custom_components
sys.modules["custom_components.globird_ha"] = globird_package

homeassistant = types.ModuleType("homeassistant")
config_entries = types.ModuleType("homeassistant.config_entries")
core = types.ModuleType("homeassistant.core")
util = types.ModuleType("homeassistant.util")
dt = types.ModuleType("homeassistant.util.dt")
components = types.ModuleType("homeassistant.components")
recorder = types.ModuleType("homeassistant.components.recorder")
recorder_models = types.ModuleType("homeassistant.components.recorder.models")
recorder_statistics = types.ModuleType("homeassistant.components.recorder.statistics")


class StatisticMeanType:
    """2026.11 mean types. NONE is the only valid choice for cumulative energy."""

    NONE = 0
    ARITHMETIC = 1


class StatisticData(dict):
    """Dict-shaped statistic row accepted by the recorder."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class Recorder:
    """In-memory hourly statistics table."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[datetime, float]] = {}
        self.queries: list[tuple[Any, ...]] = []
        self.metadata: list[dict[str, Any]] = []
        self.fail_ids_containing = ""

    def statistics_during_period(
        self,
        _hass: Any,
        start: datetime,
        end: datetime,
        statistic_ids: set[str],
        period: str,
        units: Any,
        types: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        self.queries.append((start, end, period, units, set(types), set(statistic_ids)))
        statistic_id = next(iter(statistic_ids))
        rows = [
            {"start": stamp, "sum": total}
            for stamp, total in sorted(self.rows.get(statistic_id, {}).items())
            if start <= stamp < end
        ]
        return {statistic_id: rows}

    def async_add_external_statistics(
        self,
        _hass: Any,
        metadata: dict[str, Any],
        points: list[StatisticData],
    ) -> None:
        statistic_id = str(metadata["statistic_id"])
        if self.fail_ids_containing and self.fail_ids_containing in statistic_id:
            raise RuntimeError("recorder rejected the test statistic")
        self.metadata.append(metadata)
        bucket = self.rows.setdefault(statistic_id, {})
        for point in points:
            bucket[point["start"]] = point["sum"]
            assert point["state"] == point["sum"]


class _RecorderInstance:
    """Stand-in for the recorder executor Home Assistant requires."""

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class Hass:
    """Minimal hass that runs recorder reads inline."""

    def __init__(self) -> None:
        self.config = types.SimpleNamespace(time_zone="Australia/Sydney")

    def async_create_task(self, coro: Any) -> Any:
        return asyncio.get_running_loop().create_task(coro)


def get_instance(_hass: Any) -> _RecorderInstance:
    """Return the test recorder executor."""
    return _RecorderInstance()


RECORDER = Recorder()
recorder.get_instance = get_instance
dt.get_time_zone = lambda name: ZoneInfo(name)
recorder_models.StatisticMeanType = StatisticMeanType
recorder_models.StatisticData = StatisticData
recorder_statistics.statistics_during_period = RECORDER.statistics_during_period
recorder_statistics.async_add_external_statistics = RECORDER.async_add_external_statistics
config_entries.ConfigEntry = object
core.HomeAssistant = object
util.dt = dt
homeassistant.config_entries = config_entries
homeassistant.core = core
homeassistant.util = util
homeassistant.components = components
components.recorder = recorder

sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.config_entries"] = config_entries
sys.modules["homeassistant.core"] = core
sys.modules["homeassistant.util"] = util
sys.modules["homeassistant.util.dt"] = dt
sys.modules["homeassistant.components"] = components
sys.modules["homeassistant.components.recorder"] = recorder
sys.modules["homeassistant.components.recorder.models"] = recorder_models
sys.modules["homeassistant.components.recorder.statistics"] = recorder_statistics

electricity_import = importlib.import_module(
    "custom_components.globird_ha.electricity_import"
)


def _payload(service_id: str) -> dict[str, Any]:
    return {
        "service_data": {
            service_id: {
                "service": {"serviceType": "Power"},
                "usage": {
                    "data": [
                        {
                            "readDate": "2026/11/01",
                            "suffix": "E1",
                            "chargeCategoryCode": "USAGE",
                            "chargeType": "Peak Usage",
                            "minQualityMethod": "A",
                            "serial": "meter",
                            "usageArray": [1.0] * 48,
                        }
                    ]
                },
            }
        }
    }


def _use_recorder_stub() -> None:
    """Put the 2026.11 recorder stub back after other tests install their own."""
    sys.modules["homeassistant.components.recorder"] = recorder
    sys.modules["homeassistant.components.recorder.models"] = recorder_models
    sys.modules["homeassistant.components.recorder.statistics"] = recorder_statistics


def test_import_uses_2026_11_recorder_api_and_is_idempotent() -> None:
    """A second refresh with the same cache must not write the sums again."""
    _use_recorder_stub()
    RECORDER.rows.clear()
    RECORDER.queries.clear()
    RECORDER.metadata.clear()
    RECORDER.fail_ids_containing = ""
    hass = Hass()
    entry = types.SimpleNamespace(entry_id="entry-1", options={})

    asyncio.run(
        electricity_import.async_import_coordinator_data(hass, entry, _payload("power"))
    )
    asyncio.run(
        electricity_import.async_import_coordinator_data(hass, entry, _payload("power"))
    )

    assert len(RECORDER.metadata) == 2
    streams = {metadata["name"] for metadata in RECORDER.metadata}
    assert streams == {"GloBird Grid import", "GloBird General import"}
    for metadata in RECORDER.metadata:
        assert metadata["source"] == "globird_ha"
        assert metadata["unit_class"] == "energy"
        assert metadata["unit_of_measurement"] == "kWh"
        assert metadata["has_sum"] is True
        assert metadata["mean_type"] == 0
        assert metadata["statistic_id"].startswith("globird_ha:")
    assert RECORDER.queries
    for start, end, period, units, queried_types, _ids in RECORDER.queries:
        assert period == "hour"
        assert units is None
        assert queried_types == {"sum"}
        assert end > start
    window_start = datetime(2026, 11, 1, tzinfo=SYDNEY).astimezone(UTC)
    assert any(end == window_start and start == window_start - timedelta(days=400) for start, end, *_rest in RECORDER.queries)
    totals = [max(rows.values()) for rows in RECORDER.rows.values()]
    assert totals == [48.0, 48.0]


def test_one_failing_service_does_not_block_another_or_require_gas() -> None:
    """Electricity import failures stay inside the electricity importer."""
    _use_recorder_stub()
    RECORDER.rows.clear()
    RECORDER.metadata.clear()
    RECORDER.fail_ids_containing = "bad"
    hass = Hass()
    entry = types.SimpleNamespace(entry_id="entry-1", options={})
    data = _payload("bad")
    data["service_data"].update(_payload("good")["service_data"])

    asyncio.run(electricity_import.async_import_coordinator_data(hass, entry, data))

    written = [metadata["statistic_id"] for metadata in RECORDER.metadata]
    assert written
    assert all("good" in statistic_id for statistic_id in written)
    assert all("bad" not in statistic_id for statistic_id in written)


def test_disabled_option_does_not_read_the_recorder() -> None:
    """Turning the option off leaves existing statistics and the recorder alone."""
    _use_recorder_stub()
    RECORDER.queries.clear()

    class Coordinator:
        def __init__(self) -> None:
            self.data = _payload("power")

        def async_add_listener(self, _callback: Any) -> Any:
            return lambda: None

    hass = Hass()
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        options={"import_electricity_statistics": False},
    )

    async def _run() -> None:
        electricity_import.async_setup_electricity_statistics(hass, entry, Coordinator())
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert RECORDER.queries == []
