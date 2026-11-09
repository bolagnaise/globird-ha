"""Tests for tariff inference and live pricing."""

from __future__ import annotations

import importlib.util
import random
from datetime import date, datetime, timedelta
from pathlib import Path

TARIFF_PATH = (
    Path(__file__).parents[1] / "custom_components" / "globird_ha" / "tariff.py"
)
_spec = importlib.util.spec_from_file_location("globird_tariff", TARIFF_PATH)
tariff = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(tariff)

# A ZEROHERO-style plan, in $ (the GloBird app shows these in cents).
PEAK = 0.473
SHOULDER = 0.33
OFFPEAK = 0.0
FIT_PEAK = 0.05
FIT_OTHER = 0.0
SUPER_EXPORT = 0.10
SUPER_EXPORT_CAP = 15.0
SUPPLY = 1.474
ZEROHERO = 1.0

COMPONENTS = [
    # name, code, suffix, periods
    ("Peak Usage", "USAGE", "E1", [("16:00:00", "23:00:00")]),
    ("Offpeak Usage", "USAGE", "E1", [("11:00:00", "14:00:00")]),
    (
        "Shoulder Usage",
        "USAGE",
        "E1",
        [("00:00:00", "11:00:00"), ("14:00:00", "16:00:00"), ("23:00:00", "1.00:00:00")],
    ),
    ("Controlled Load", "CONTROL", "E2", [("00:00:00", "1.00:00:00")]),
    ("ZeroHero", "LOW_USAGE_REWARD_PER_DAY", "E1", [("18:00:00", "21:00:00")]),
    ("Solar/Generation Feed in (4pm-11pm)", "SOLAR", "B1", [("16:00:00", "23:00:00")]),
    (
        "Solar/Generation Feed in (11pm-4pm)",
        "SOLAR",
        "B1",
        [("00:00:00", "16:00:00"), ("23:00:00", "1.00:00:00")],
    ),
    ("Super Export top up", "ADDON_FEED_IN", "B1", [("18:00:00", "21:00:00")]),
]


def _usage_array(windows_kwh: dict[tuple[int, int], float]) -> list[float]:
    """Spread kWh evenly over 5-minute intervals inside each window."""
    arr = [0.0] * 288
    for (start, end), kwh in windows_kwh.items():
        slots = list(range(start // 5, end // 5))
        for slot in slots:
            arr[slot] += kwh / len(slots)
    return arr


def make_history(days: int = 20, seed: int = 7, zerohero_miss_day: int | None = 5):
    """Return synthetic (usage_payload, cost_payload) billed at the plan rates."""
    rng = random.Random(seed)
    usage_rows: list[dict] = []
    cost_rows: list[dict] = []
    start = date(2026, 10, 1)
    for index in range(days):
        day = (start + timedelta(days=index)).strftime("%Y/%m/%d")
        peak = rng.uniform(0.0, 3.0)
        offpeak = rng.uniform(5.0, 40.0)
        shoulder = rng.uniform(0.2, 12.0)
        window_import = 0.3 if index == zerohero_miss_day else 0.0
        fit_peak = rng.uniform(4.0, 25.0)
        fit_other = rng.uniform(0.0, 20.0)
        super_export = min(fit_peak, rng.uniform(3.0, 22.0))
        e1 = _usage_array(
            {
                (0, 660): shoulder,
                (660, 840): offpeak,
                (960, 1080): peak - window_import,
                (1080, 1260): window_import,
            }
        )
        b1 = _usage_array({(960, 1380): fit_peak, (0, 960): fit_other})
        kwh = {
            "Peak Usage": peak,
            "Offpeak Usage": offpeak,
            "Shoulder Usage": shoulder,
            "Controlled Load": 0.0,
            "ZeroHero": window_import,
            "Solar/Generation Feed in (4pm-11pm)": fit_peak,
            "Solar/Generation Feed in (11pm-4pm)": fit_other,
            "Super Export top up": super_export,
        }
        for name, code, suffix, periods in COMPONENTS:
            usage_rows.append(
                {
                    "readDate": day,
                    "chargeType": name,
                    "chargeCategoryCode": code,
                    "suffix": suffix,
                    "usage": round(kwh[name], 4),
                    "usageArray": b1 if suffix == "B1" else (e1 if suffix == "E1" else [0.0] * 288),
                    "timePeriods": [
                        {"startTime": s, "endTime": e} for s, e in periods
                    ],
                }
            )
        usage_cost = peak * PEAK + shoulder * SHOULDER + offpeak * OFFPEAK
        solar = fit_peak * FIT_PEAK + fit_other * FIT_OTHER
        top_up = min(super_export, SUPER_EXPORT_CAP) * SUPER_EXPORT
        credit = 0.0 if index == zerohero_miss_day else ZEROHERO
        for category, amount, qty in (
            ("USAGE", usage_cost, peak + shoulder + offpeak),
            ("SOLAR", -solar, fit_peak + fit_other),
            ("Super Export top up", -top_up, super_export),
            ("ZEROHERO Credit", -credit, 1 if credit else 0),
            ("SUPPLY", SUPPLY, 0),
        ):
            cost_rows.append(
                {
                    "date": day,
                    "chargeCategory": category,
                    "amount": round(amount, 2),
                    "quantity": round(qty, 2),
                }
            )
    # The portal publishes an early supply-only row for the newest day.
    cost_rows.append(
        {
            "date": (start + timedelta(days=days)).strftime("%Y/%m/%d"),
            "chargeCategory": "SUPPLY",
            "amount": round(SUPPLY, 2),
            "quantity": 0,
        }
    )
    return {"data": usage_rows}, {"data": cost_rows}


def _component(result: dict, name: str) -> dict:
    return next(c for c in result["components"] if c["name"] == name)


def test_parse_portal_time_handles_end_of_day() -> None:
    assert tariff.parse_portal_time("16:00:00") == 960
    assert tariff.parse_portal_time("1.00:00:00") == 1440
    assert tariff.parse_portal_time("bad") is None


def test_structure_reads_components_and_windows_from_latest_day() -> None:
    usage, _cost = make_history(days=3)
    structure = tariff.build_tariff_structure(usage)

    assert structure["day"] == "2026/10/03"
    shoulder = next(c for c in structure["components"] if c["name"] == "Shoulder Usage")
    assert shoulder["windows"] == [[0, 660], [840, 960], [1380, 1440]]
    assert shoulder["direction"] == "import"
    feed = next(c for c in structure["components"] if c["category"] == "ADDON_FEED_IN")
    assert feed["direction"] == "export"


def test_infer_tariff_recovers_plan_rates() -> None:
    usage, cost = make_history()
    result = tariff.infer_tariff(usage, cost)

    # Well-observed rates snap exactly to the published 0.1 c values.
    assert _component(result, "Shoulder Usage")["rate"] == SHOULDER
    assert abs(_component(result, "Shoulder Usage")["fitted_rate"] - SHOULDER) < 0.002
    assert abs(_component(result, "Peak Usage")["rate"] - PEAK) < 0.01
    assert _component(result, "Offpeak Usage")["rate"] == 0.0
    assert _component(result, "Solar/Generation Feed in (4pm-11pm)")["rate"] == FIT_PEAK
    assert _component(result, "Solar/Generation Feed in (11pm-4pm)")["rate"] == 0.0
    assert _component(result, "Shoulder Usage")["confidence"] == "high"
    assert result["supply"]["rate"] == 1.47
    assert result["fit_days"] == 20
    assert result["unmodelled_categories"] == []


def test_controlled_load_without_usage_is_unobserved() -> None:
    usage, cost = make_history()
    load = _component(tariff.infer_tariff(usage, cost), "Controlled Load")

    assert load["rate"] is None
    assert load["confidence"] == "unobserved"


def test_super_export_rate_and_cap_are_inferred() -> None:
    usage, cost = make_history(days=30)
    info = tariff.infer_tariff(usage, cost)["super_export"]

    assert info["rate"] == SUPER_EXPORT
    assert info["capped_days"] > 0
    assert info["cap_kwh"] == SUPER_EXPORT_CAP
    assert info["windows"] == [[1080, 1260]]


def test_zerohero_credit_and_window_import() -> None:
    usage, cost = make_history(zerohero_miss_day=5)
    info = tariff.infer_tariff(usage, cost)["zerohero"]

    assert info["credit"] == 1.0
    assert info["missed_days"] == 1
    assert info["achieved_days"] == 19
    assert info["threshold_kwh"] == 0.09
    assert info["min_window_import_kwh_missed"] >= 0.29
    assert info["max_window_import_kwh_achieved"] < 0.01


def test_overrides_use_app_names_and_cents() -> None:
    usage, cost = make_history()
    inferred = tariff.infer_tariff(usage, cost)
    result = tariff.apply_overrides(
        inferred,
        {
            "Peak Usage": 47.3,
            "Daily Charge": 147.4,
            "Super Export top up - Step1": 10,
            "Super Export top up - Step2": 0,
            "Controlled Load": 29.7,
            "Not A Charge": 5,
        },
        super_export_cap_kwh=15,
        zerohero_kwh_per_hour=0.02,
    )

    assert _component(result, "Peak Usage")["rate"] == 0.473
    assert _component(result, "Peak Usage")["source"] == "override"
    assert _component(result, "Controlled Load")["rate"] == 0.297
    assert result["supply"]["rate"] == 1.474
    assert result["super_export"]["rate"] == 0.1
    assert result["super_export"]["rate_after_cap"] == 0.0
    assert result["super_export"]["cap_kwh"] == 15
    assert result["zerohero"]["threshold_kwh"] == 0.06
    assert result["unmatched_overrides"] == ["Not A Charge"]
    # The inferred tariff is not mutated.
    assert _component(inferred, "Peak Usage")["source"] == "portal_inferred"


def _plan() -> dict:
    usage, cost = make_history(days=30)
    return tariff.apply_overrides(
        tariff.infer_tariff(usage, cost),
        {
            "Peak Usage": 47.3,
            "Shoulder Usage": 33,
            "Solar/Generation Feed in (4pm-11pm)": 5,
            "Solar/Generation Feed in (11pm-4pm)": 0,
            "Daily Charge": 147.4,
            "Super Export top up": 10,
        },
    )


def test_prices_follow_windows_and_super_export_cap() -> None:
    plan = _plan()
    day = datetime(2026, 11, 9)

    assert tariff.import_price_at(plan, day.replace(hour=12)) == 0.0
    assert tariff.import_price_at(plan, day.replace(hour=15)) == 0.33
    assert tariff.import_price_at(plan, day.replace(hour=19)) == 0.473
    assert tariff.import_price_at(plan, day.replace(hour=23, minute=30)) == 0.33
    assert tariff.export_price_at(plan, day.replace(hour=12)) == 0.0
    assert tariff.export_price_at(plan, day.replace(hour=17)) == 0.05
    assert tariff.export_price_at(plan, day.replace(hour=19)) == 0.15
    assert tariff.export_price_at(plan, day.replace(hour=19), super_export_used_kwh=15) == 0.05


def test_next_boundary_and_forecast() -> None:
    plan = _plan()
    now = datetime(2026, 11, 9, 12, 7)

    assert tariff.next_boundary(plan, now) == datetime(2026, 11, 9, 14, 0)
    assert tariff.next_boundary(plan, now.replace(hour=23, minute=30)) == datetime(2026, 11, 10)
    forecast = tariff.price_forecast(plan, now)
    assert len(forecast) == 48
    assert forecast[0]["start"] == "2026-11-09T12:00:00"
    assert forecast[0]["import"] == 0.0


def _feed(tracker, plan, start, minutes, import_kw=0.0, export_kw=0.0, readings=(0.0, 0.0)):
    """Feed one reading a minute at constant power; return final readings."""
    imp, exp = readings
    for minute in range(minutes):
        imp += import_kw / 60
        exp += export_kw / 60
        tracker.update(plan, start + timedelta(minutes=minute + 1), imp, exp)
    return imp, exp


def test_live_tracker_estimates_today() -> None:
    plan = _plan()
    tracker = tariff.LiveCostTracker()
    day = datetime(2026, 11, 9)
    tracker.update(plan, day.replace(hour=0, minute=0, second=30), 100.0, 50.0)
    readings = _feed(tracker, plan, day.replace(hour=6), 60, import_kw=1.0, readings=(100.0, 50.0))
    readings = _feed(tracker, plan, day.replace(hour=11), 120, import_kw=5.0, readings=readings)
    readings = _feed(tracker, plan, day.replace(hour=18), 180, export_kw=10.0, readings=readings)

    estimate = tracker.cost(plan, day.replace(hour=21, minute=30))
    assert estimate["import_kwh"]["Shoulder Usage"] == 1.0
    assert estimate["import_kwh"]["Offpeak Usage"] == 10.0
    assert estimate["super_export_kwh"] == 30.0
    assert estimate["super_export_remaining_kwh"] == 0.0
    assert estimate["zerohero"] == "earned"
    expected = 1.474 + 0.33 - 30 * 0.05 - 15 * 0.10 - 1.0
    assert abs(estimate["total"] - round(expected, 2)) < 0.011


def test_live_tracker_zerohero_missed_and_day_roll() -> None:
    plan = _plan()
    tracker = tariff.LiveCostTracker()
    day = datetime(2026, 11, 9)
    tracker.update(plan, day.replace(hour=17, minute=59), 10.0, 0.0)
    assert tracker.zerohero_state(plan, day.replace(hour=17, minute=59)) == "pending"
    _feed(tracker, plan, day.replace(hour=18), 30, import_kw=0.5, readings=(10.0, 0.0))

    assert tracker.zerohero_state(plan, day.replace(hour=18, minute=30)) == "missed"
    assert "ZeroHero" not in tracker.cost(plan, day.replace(hour=22))["lines"]

    tracker.update(plan, day.replace(hour=23, minute=59) + timedelta(minutes=2), 10.5, 0.0)
    assert tracker.day == "2026-11-10"
    assert tracker.zerohero_import_kwh == 0.0


def test_live_tracker_handles_meter_reset() -> None:
    plan = _plan()
    tracker = tariff.LiveCostTracker()
    day = datetime(2026, 11, 9, 9)
    tracker.update(plan, day, 25.0, 0.0)
    tracker.update(plan, day + timedelta(minutes=1), 0.4, 0.0)  # daily counter reset

    assert round(tracker.import_kwh["Shoulder Usage"], 3) == 0.4


def test_live_tracker_round_trips_state() -> None:
    plan = _plan()
    tracker = tariff.LiveCostTracker()
    day = datetime(2026, 11, 9, 9)
    tracker.update(plan, day, 1.0, 2.0)
    tracker.update(plan, day + timedelta(minutes=1), 1.5, 2.0)

    restored = tariff.LiveCostTracker(tracker.as_dict())
    assert restored.as_dict() == tracker.as_dict()


def test_snap_rate_rounds_to_tenth_of_a_cent() -> None:
    assert tariff.snap_rate(0.32994) == 0.33
    assert tariff.snap_rate(0.05012) == 0.05
    assert tariff.snap_rate(0.09987) == 0.1
    assert tariff.snap_rate(0.45936) == 0.459
    assert tariff.snap_rate(0.2974) == 0.297
