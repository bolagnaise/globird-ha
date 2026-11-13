"""Tariff inference and live pricing for GloBird plans.

GloBird's portal does not publish a plan's rates through the endpoints this
integration reads, but it does publish everything needed to recover them:

* every smart-meter usage row names its charge (``Peak Usage``, ``Solar/
  Generation Feed in (4pm-11pm)``, ``Super Export top up``...), its charge
  category code and the local time windows it applies to; and
* ``CostDetail`` returns the billed amount per day for each cost category.

Rates are recovered per cost category by least squares across the complete
days in the usage window, with a standard error for each rate so consumers can
tell a well-determined rate (lots of kWh in that window) from a guess. Values
can be pinned through the integration options, using the names and units the
GloBird app shows on its Plan Details page (cents).

This module has no Home Assistant imports so it can be unit tested directly.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median
from typing import Any

USAGE_CODE = "USAGE"
CONTROL_CODE = "CONTROL"
SOLAR_CODE = "SOLAR"
ADDON_FEED_IN_CODE = "ADDON_FEED_IN"
LOW_USAGE_REWARD_CODE = "LOW_USAGE_REWARD_PER_DAY"

SUPPLY_COST_CATEGORY = "SUPPLY"
DAILY_CHARGE_NAMES = ("daily charge", "supply", "daily supply charge")

# GloBird product terms for ZEROHERO: imports of 0.03 kWh/hour or less.
DEFAULT_ZEROHERO_KWH_PER_HOUR = 0.03

# Portal cost rows are rounded to the cent; treat that as the noise floor.
_ROUNDING_VARIANCE = (0.01**2) / 12
_HIGH_CONFIDENCE_SE = 0.002  # $/kWh (0.2 c)
_MEDIUM_CONFIDENCE_SE = 0.01  # $/kWh (1 c)
_FREE_RATE = 0.0005  # $/kWh below which a rate is reported as free
# Retail rates are published to 0.1 c/kWh, so fitted rates are snapped to it.
# The unrounded fit is kept as ``fitted_rate``.
RATE_STEP = 0.001  # $/kWh


def snap_rate(value: float) -> float:
    """Round a fitted $/kWh rate to the nearest 0.1 c."""
    return round(round(value / RATE_STEP) * RATE_STEP, 4)

MINUTES_PER_DAY = 24 * 60


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def parse_portal_time(value: Any) -> int | None:
    """Return minutes after midnight for ``HH:MM:SS`` or ``D.HH:MM:SS``."""
    if value is None:
        return None
    raw = str(value).strip()
    days = 0
    if "." in raw.split(":", 1)[0]:
        day_part, raw = raw.split(".", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = raw.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    total = days * MINUTES_PER_DAY + hours * 60 + minutes
    return max(0, min(total, MINUTES_PER_DAY))


def format_minutes(value: int) -> str:
    """Format minutes after midnight as ``HH:MM`` (24:00 for end of day)."""
    return f"{value // 60:02d}:{value % 60:02d}"


def _row_direction(row: dict[str, Any]) -> str:
    suffix = str(row.get("suffix") or "").strip().upper()
    code = str(row.get("chargeCategoryCode") or "").strip().upper()
    if suffix.startswith("B") or code in {SOLAR_CODE, ADDON_FEED_IN_CODE}:
        return "export"
    return "import"


def _windows(row: dict[str, Any]) -> list[list[int]]:
    windows: list[list[int]] = []
    for period in row.get("timePeriods") or []:
        if not isinstance(period, dict):
            continue
        start = parse_portal_time(period.get("startTime"))
        end = parse_portal_time(period.get("endTime"))
        if start is None or end is None or start == end:
            continue
        if end < start:  # wraps midnight
            windows.append([start, MINUTES_PER_DAY])
            windows.append([0, end])
        else:
            windows.append([start, end])
    return sorted(windows)


def in_windows(windows: list[list[int]], minute: int) -> bool:
    """Return whether minute-of-day falls inside any [start, end) window."""
    return any(start <= minute < end for start, end in windows)


def build_tariff_structure(usage_payload: Any) -> dict[str, Any]:
    """Return the plan's charge components from the latest usage day.

    The structure is rebuilt on every refresh, so a plan change on the
    portal (new windows, a new add-on) is picked up automatically.
    """
    rows = _payload_rows(usage_payload)
    days = sorted({str(r.get("readDate")) for r in rows if r.get("readDate")})
    if not days:
        return {"day": None, "components": []}
    latest = days[-1]
    components: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("readDate")) != latest:
            continue
        name = str(row.get("chargeType") or "").strip()
        if not name:
            continue
        component = components.get(name)
        if component is None:
            component = {
                "name": name,
                "category": str(row.get("chargeCategoryCode") or "").strip().upper(),
                "direction": _row_direction(row),
                "registers": [],
                "windows": _windows(row),
            }
            components[name] = component
        suffix = str(row.get("suffix") or "").strip()
        if suffix and suffix not in component["registers"]:
            component["registers"].append(suffix)
    return {
        "day": latest,
        "components": sorted(components.values(), key=lambda c: c["name"]),
    }


def _daily_component_kwh(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_day: dict[str, dict[str, float]] = {}
    for row in rows:
        day = str(row.get("readDate") or "")
        name = str(row.get("chargeType") or "").strip()
        usage = _as_float(row.get("usage"))
        if not day or not name or usage is None:
            continue
        by_day.setdefault(day, {})
        by_day[day][name] = by_day[day].get(name, 0.0) + usage
    return by_day


def _daily_cost(rows: list[dict[str, Any]]) -> dict[str, dict[str, tuple[float, float]]]:
    by_day: dict[str, dict[str, tuple[float, float]]] = {}
    for row in rows:
        day = str(row.get("date") or "")
        category = str(row.get("chargeCategory") or "").strip()
        amount = _as_float(row.get("amount"))
        if not day or not category or amount is None:
            continue
        quantity = _as_float(row.get("quantity")) or 0.0
        prev_amount, prev_qty = by_day.setdefault(day, {}).get(category, (0.0, 0.0))
        by_day[day][category] = (prev_amount + amount, prev_qty + quantity)
    # Drop days that only have the early fixed supply row.
    return {
        day: categories
        for day, categories in by_day.items()
        if any(cat.upper() != SUPPLY_COST_CATEGORY for cat in categories)
    }


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Solve a small dense linear system with partial pivoting."""
    size = len(vector)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(size):
            if r == col:
                continue
            factor = aug[r][col] / aug[col][col]
            for c in range(col, size + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][size] / aug[i][i] for i in range(size)]


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    size = len(matrix)
    columns = []
    for i in range(size):
        unit = [1.0 if j == i else 0.0 for j in range(size)]
        solved = _solve(matrix, unit)
        if solved is None:
            return None
        columns.append(solved)
    return [[columns[j][i] for j in range(size)] for i in range(size)]


def _confidence(standard_error: float | None) -> str:
    if standard_error is None:
        return "unobserved"
    if standard_error <= _HIGH_CONFIDENCE_SE:
        return "high"
    if standard_error <= _MEDIUM_CONFIDENCE_SE:
        return "medium"
    return "low"


def _fit_category(
    names: list[str],
    days: list[str],
    kwh: dict[str, dict[str, float]],
    amounts: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Least-squares fit of per-component $/kWh for one cost category."""
    results: dict[str, dict[str, Any]] = {}
    observed = [n for n in names if sum(kwh.get(d, {}).get(n, 0.0) for d in days) > 0]
    for name in names:
        results[name] = {
            "rate": None,
            "standard_error": None,
            "confidence": "unobserved",
            "observed_kwh": round(sum(kwh.get(d, {}).get(name, 0.0) for d in days), 3),
            "max_daily_kwh": round(
                max((kwh.get(d, {}).get(name, 0.0) for d in days), default=0.0), 3
            ),
            "source": "portal_inferred",
        }
    if not observed or len(days) < len(observed):
        return results

    x_rows = [[kwh.get(d, {}).get(n, 0.0) for n in observed] for d in days]
    y_vals = [amounts[d] for d in days]
    size = len(observed)
    xtx = [
        [sum(row[i] * row[j] for row in x_rows) for j in range(size)]
        for i in range(size)
    ]
    xty = [sum(row[i] * y for row, y in zip(x_rows, y_vals, strict=True)) for i in range(size)]
    beta = _solve(xtx, xty)
    inverse = _invert(xtx)
    if beta is None or inverse is None:
        return results
    residuals = [
        y - sum(b * x for b, x in zip(beta, row, strict=True)) for row, y in zip(x_rows, y_vals, strict=True)
    ]
    dof = max(1, len(days) - size)
    variance = max(sum(r * r for r in residuals) / dof, _ROUNDING_VARIANCE)
    max_residual = max((abs(r) for r in residuals), default=0.0)
    for index, name in enumerate(observed):
        se = math.sqrt(max(0.0, variance * inverse[index][index]))
        rate = beta[index]
        if rate < 0 and rate > -2 * se:
            rate = 0.0
        if abs(rate) < _FREE_RATE:
            rate = 0.0
        results[name].update(
            {
                "rate": snap_rate(rate),
                "fitted_rate": round(rate, 5),
                "standard_error": round(se, 4),
                "confidence": _confidence(se),
                "fit_days": len(days),
                "fit_max_residual": round(max_residual, 3),
            }
        )
    return results


def _matching_cost_category(
    component: dict[str, Any], categories: set[str]
) -> str | None:
    code = component["category"]
    name = component["name"].lower()
    for category in categories:
        upper = category.upper()
        if code == CONTROL_CODE and upper == USAGE_CODE:
            return category
        if upper == code and code in {USAGE_CODE, SOLAR_CODE}:
            return category
    for category in categories:
        lower = category.lower()
        if lower == name or lower.startswith(name) or name.startswith(lower):
            return category
        # "ZeroHero" usage rows bill as "ZEROHERO Credit".
        if code == LOW_USAGE_REWARD_CODE and lower.replace(" ", "").startswith(
            name.replace(" ", "")
        ):
            return category
    return None


def _window_import_by_day(
    rows: list[dict[str, Any]], windows: list[list[int]]
) -> dict[str, float]:
    """Sum interval imports inside windows per day, once per import register."""
    arrays: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        if _row_direction(row) != "import":
            continue
        arr = row.get("usageArray")
        if isinstance(arr, list) and arr:
            key = (str(row.get("readDate") or ""), str(row.get("suffix") or ""))
            arrays.setdefault(key, arr)
    totals: dict[str, float] = {}
    for (day, _suffix), arr in arrays.items():
        step = MINUTES_PER_DAY / len(arr)
        total = sum(
            _as_float(v) or 0.0
            for i, v in enumerate(arr)
            if in_windows(windows, int(i * step))
        )
        totals[day] = totals.get(day, 0.0) + total
    return totals


def infer_tariff(usage_payload: Any, cost_payload: Any) -> dict[str, Any]:
    """Infer the current plan's rates from portal usage and cost history."""
    structure = build_tariff_structure(usage_payload)
    usage_rows = _payload_rows(usage_payload)
    kwh = _daily_component_kwh(usage_rows)
    cost = _daily_cost(_payload_rows(cost_payload))
    categories = {cat for day in cost.values() for cat in day}
    components = [dict(c) for c in structure["components"]]

    by_category: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        matched = _matching_cost_category(component, categories)
        component["cost_category"] = matched
        if matched:
            by_category.setdefault(matched, []).append(component)

    result: dict[str, Any] = {
        "structure_day": structure["day"],
        "fit_days": 0,
        "components": components,
        "supply": None,
        "super_export": None,
        "zerohero": None,
        "unmodelled_categories": [],
    }

    for category, members in by_category.items():
        code = members[0]["category"]
        if code in {USAGE_CODE, CONTROL_CODE, SOLAR_CODE}:
            days = sorted(d for d in cost if d in kwh and category in cost[d])
            sign = -1.0 if code == SOLAR_CODE else 1.0
            amounts = {d: sign * cost[d][category][0] for d in days}
            fitted = _fit_category([m["name"] for m in members], days, kwh, amounts)
            for member in members:
                member.update(fitted[member["name"]])
            result["fit_days"] = max(result["fit_days"], len(days))
        elif code == ADDON_FEED_IN_CODE:
            result["super_export"] = _infer_capped_credit(
                members[0], [cost[d][category] for d in sorted(cost) if category in cost[d]]
            )
        elif code == LOW_USAGE_REWARD_CODE:
            result["zerohero"] = _infer_low_usage_reward(
                members[0], usage_rows, cost, category
            )

    supply = [
        amounts[SUPPLY_COST_CATEGORY][0]
        for amounts in cost.values()
        if SUPPLY_COST_CATEGORY in amounts
    ]
    if supply:
        result["supply"] = {
            "rate": round(median(supply), 4),
            "unit": "AUD/day",
            "confidence": "high" if len(supply) >= 3 else "medium",
            "observed_days": len(supply),
            "note": "Portal rounds daily rows to the cent.",
            "source": "portal_inferred",
        }

    modelled = set(by_category) | {SUPPLY_COST_CATEGORY}
    result["unmodelled_categories"] = sorted(
        cat for cat in categories if cat not in modelled
    )
    return result


def _infer_capped_credit(
    component: dict[str, Any], rows: list[tuple[float, float]]
) -> dict[str, Any]:
    """Infer a per-kWh export credit and its daily kWh cap."""
    pairs = [(-amount, qty) for amount, qty in rows if qty > 0.5 and amount < 0]
    info: dict[str, Any] = {
        "name": component["name"],
        "windows": component["windows"],
        "rate": None,
        "cap_kwh": None,
        "confidence": "unobserved",
        "observed_days": len(pairs),
        "capped_days": 0,
        "source": "portal_inferred",
    }
    if not pairs:
        return info
    # Small days carry the most cent-rounding noise, so seed from the larger
    # days, then take a kWh-weighted rate over the days that were not capped.
    sized = [credit / qty for credit, qty in pairs if qty >= 3] or [
        credit / qty for credit, qty in pairs
    ]
    top = max(sized)
    seed = median([r for r in sized if r >= top * 0.97])
    uncapped = [(c, q) for c, q in pairs if c >= q * seed - 0.011]
    capped = [c for c, q in pairs if c < q * seed - 0.011]
    rate = (
        sum(c for c, _q in uncapped) / sum(q for _c, q in uncapped)
        if uncapped
        else seed
    )
    info["rate"] = snap_rate(rate)
    info["fitted_rate"] = round(rate, 5)
    info["confidence"] = "high" if len(uncapped) >= 3 else "medium"
    info["capped_days"] = len(capped)
    if capped:
        info["cap_kwh"] = round(median(capped) / rate, 1)
    else:
        info["max_uncapped_kwh_observed"] = round(max(q for _c, q in pairs), 2)
    return info


def _infer_low_usage_reward(
    component: dict[str, Any],
    usage_rows: list[dict[str, Any]],
    cost: dict[str, dict[str, tuple[float, float]]],
    category: str,
) -> dict[str, Any]:
    """Infer the daily low-usage reward (ZEROHERO) and its window imports."""
    windows = component["windows"]
    imports = _window_import_by_day(usage_rows, windows)
    credits = {d: -cost[d][category][0] for d in cost if category in cost[d]}
    achieved = {d for d, credit in credits.items() if credit > 0.005}
    missed = {d for d in credits if d not in achieved}
    paid = [credits[d] for d in achieved]
    hours = sum(end - start for start, end in windows) / 60
    info: dict[str, Any] = {
        "name": component["name"],
        "windows": windows,
        "credit": round(median(paid), 2) if paid else None,
        "unit": "AUD/day",
        "achieved_days": len(achieved),
        "missed_days": len(missed),
        "threshold_kwh_per_hour": DEFAULT_ZEROHERO_KWH_PER_HOUR,
        "threshold_kwh": round(DEFAULT_ZEROHERO_KWH_PER_HOUR * hours, 3),
        "source": "portal_inferred",
    }
    achieved_imports = [imports[d] for d in achieved if d in imports]
    missed_imports = [imports[d] for d in missed if d in imports]
    if achieved_imports:
        info["max_window_import_kwh_achieved"] = round(max(achieved_imports), 4)
    if missed_imports:
        info["min_window_import_kwh_missed"] = round(min(missed_imports), 4)
    return info


def _normalise_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def apply_overrides(
    tariff: dict[str, Any],
    rate_overrides: dict[str, Any] | None,
    *,
    super_export_cap_kwh: float | None = None,
    zerohero_kwh_per_hour: float | None = None,
) -> dict[str, Any]:
    """Apply user-pinned rates (cents, as the GloBird app shows them)."""
    result = {
        **tariff,
        "components": [dict(c) for c in tariff.get("components") or []],
        "supply": dict(tariff["supply"]) if tariff.get("supply") else None,
        "super_export": (
            dict(tariff["super_export"]) if tariff.get("super_export") else None
        ),
        "zerohero": dict(tariff["zerohero"]) if tariff.get("zerohero") else None,
    }
    unmatched: list[str] = []
    for raw_key, raw_value in (rate_overrides or {}).items():
        cents = _as_float(raw_value)
        if cents is None:
            unmatched.append(str(raw_key))
            continue
        dollars = round(cents / 100, 5)
        key = _normalise_key(str(raw_key))
        # The app labels tiers "Super Export top up - Step1"/"- Step2".
        step2 = key.endswith("step2")
        key = key.removesuffix("step1").removesuffix("step2")
        if key in {_normalise_key(n) for n in DAILY_CHARGE_NAMES}:
            result["supply"] = {
                **(result["supply"] or {}),
                "rate": dollars,
                "unit": "AUD/day",
                "confidence": "user",
                "source": "override",
            }
            continue
        se = result["super_export"]
        if se and key == _normalise_key(se["name"]):
            if step2:
                se["rate_after_cap"] = dollars
            else:
                se.update({"rate": dollars, "confidence": "user", "source": "override"})
            continue
        zh = result["zerohero"]
        if zh and key == _normalise_key(zh["name"]):
            zh.update({"credit": dollars, "source": "override"})
            continue
        for component in result["components"]:
            if key == _normalise_key(component["name"]):
                component.update(
                    {"rate": dollars, "confidence": "user", "source": "override"}
                )
                break
        else:
            unmatched.append(str(raw_key))
    if super_export_cap_kwh is not None and result["super_export"]:
        result["super_export"]["cap_kwh"] = super_export_cap_kwh
        result["super_export"]["cap_source"] = "override"
    if zerohero_kwh_per_hour is not None and result["zerohero"]:
        zh = result["zerohero"]
        hours = sum(end - start for start, end in zh["windows"]) / 60
        zh["threshold_kwh_per_hour"] = zerohero_kwh_per_hour
        zh["threshold_kwh"] = round(zerohero_kwh_per_hour * hours, 3)
    result["unmatched_overrides"] = unmatched
    return result


def _component_at(
    tariff: dict[str, Any], minute: int, codes: set[str]
) -> dict[str, Any] | None:
    for component in tariff.get("components") or []:
        if component.get("category") in codes and in_windows(
            component.get("windows") or [], minute
        ):
            return component
    return None


def _minute(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def import_component_at(tariff: dict[str, Any], moment: datetime) -> dict[str, Any] | None:
    """Return the general-usage import component active at ``moment``."""
    return _component_at(tariff, _minute(moment), {USAGE_CODE})


def export_component_at(tariff: dict[str, Any], moment: datetime) -> dict[str, Any] | None:
    """Return the base feed-in component active at ``moment``."""
    return _component_at(tariff, _minute(moment), {SOLAR_CODE})


def super_export_active(tariff: dict[str, Any], moment: datetime) -> bool:
    """Return whether the capped export add-on window is open."""
    info = tariff.get("super_export") or {}
    return bool(info.get("rate")) and in_windows(info.get("windows") or [], _minute(moment))


def import_price_at(tariff: dict[str, Any], moment: datetime) -> float | None:
    """Return the marginal import price in $/kWh."""
    component = import_component_at(tariff, moment)
    return component.get("rate") if component else None


def export_price_at(
    tariff: dict[str, Any],
    moment: datetime,
    super_export_used_kwh: float | None = None,
) -> float | None:
    """Return the marginal export price in $/kWh, including the add-on."""
    component = export_component_at(tariff, moment)
    base = component.get("rate") if component else None
    if base is None:
        return None
    price = base
    if super_export_active(tariff, moment):
        info = tariff["super_export"]
        cap = info.get("cap_kwh")
        if cap is None or super_export_used_kwh is None or super_export_used_kwh < cap:
            price += info["rate"]
        else:
            price += info.get("rate_after_cap") or 0.0
    return round(price, 5)


def boundaries(tariff: dict[str, Any]) -> list[int]:
    """Return all minute-of-day boundaries where any price can change."""
    points = {0}
    windows: list[list[int]] = []
    for component in tariff.get("components") or []:
        windows.extend(component.get("windows") or [])
    for key in ("super_export", "zerohero"):
        windows.extend((tariff.get(key) or {}).get("windows") or [])
    for start, end in windows:
        points.add(start % MINUTES_PER_DAY)
        points.add(end % MINUTES_PER_DAY)
    return sorted(points)


def next_boundary(tariff: dict[str, Any], now: datetime) -> datetime:
    """Return the next local time at which a price may change."""
    current = _minute(now)
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for point in boundaries(tariff):
        if point > current:
            return base + timedelta(minutes=point)
    return base + timedelta(days=1)


def price_forecast(
    tariff: dict[str, Any], start: datetime, hours: int = 24, step_minutes: int = 30
) -> list[dict[str, Any]]:
    """Return a fixed-step import/export price forecast (add-on assumed uncapped)."""
    slot_start = start.replace(second=0, microsecond=0)
    slot_start -= timedelta(minutes=slot_start.minute % step_minutes)
    forecast = []
    for index in range(hours * 60 // step_minutes):
        moment = slot_start + timedelta(minutes=index * step_minutes)
        forecast.append(
            {
                "start": moment.isoformat(),
                "import": import_price_at(tariff, moment),
                "export": export_price_at(tariff, moment),
            }
        )
    return forecast


def describe_windows(windows: list[list[int]]) -> list[str]:
    return [f"{format_minutes(s)}-{format_minutes(e)}" for s, e in windows]


class LiveCostTracker:
    """Accumulate today's grid energy into tariff buckets from live meters.

    Fed with cumulative import/export energy readings (kWh), it allocates each
    delta to the window active at the midpoint of the interval the delta
    covers. That is accurate to the meter's update interval, which is seconds
    to a minute for most inverter integrations.
    """

    # Longer intervals are placed at the reading time rather than the midpoint.
    MAX_INTERVAL = timedelta(minutes=15)

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        self.day: str | None = state.get("day")
        self.last_import: float | None = _as_float(state.get("last_import"))
        self.last_export: float | None = _as_float(state.get("last_export"))
        self.import_kwh: dict[str, float] = dict(state.get("import_kwh") or {})
        self.export_kwh: dict[str, float] = dict(state.get("export_kwh") or {})
        self.super_export_kwh: float = _as_float(state.get("super_export_kwh")) or 0.0
        self.zerohero_import_kwh: float = (
            _as_float(state.get("zerohero_import_kwh")) or 0.0
        )
        self.last_at: datetime | None = None
        if state.get("last_at"):
            try:
                self.last_at = datetime.fromisoformat(str(state["last_at"]))
            except ValueError:
                self.last_at = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "last_import": self.last_import,
            "last_export": self.last_export,
            "import_kwh": self.import_kwh,
            "export_kwh": self.export_kwh,
            "super_export_kwh": self.super_export_kwh,
            "zerohero_import_kwh": self.zerohero_import_kwh,
            "last_at": self.last_at.isoformat() if self.last_at else None,
        }

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.day != today:
            self.day = today
            self.import_kwh = {}
            self.export_kwh = {}
            self.super_export_kwh = 0.0
            self.zerohero_import_kwh = 0.0

    @staticmethod
    def _delta(previous: float | None, current: float | None) -> float:
        if current is None or previous is None:
            return 0.0
        if current < previous:
            # Meter reset (daily counters, replaced inverter). Count from zero
            # unless it is a tiny downward correction.
            return current if previous - current > 0.5 else 0.0
        return current - previous

    def update(
        self,
        tariff: dict[str, Any],
        now: datetime,
        import_reading: float | None = None,
        export_reading: float | None = None,
    ) -> None:
        self._roll_day(now)
        moment = now
        if (
            self.last_at is not None
            and self.last_at.tzinfo == now.tzinfo
            and timedelta(0) < now - self.last_at <= self.MAX_INTERVAL
        ):
            moment = self.last_at + (now - self.last_at) / 2
            if moment.date() != now.date():
                moment = now
        if import_reading is not None or export_reading is not None:
            self.last_at = now
        minute = _minute(moment)
        if import_reading is not None:
            delta = self._delta(self.last_import, import_reading)
            self.last_import = import_reading
            if delta > 0:
                component = import_component_at(tariff, moment)
                name = component["name"] if component else "unclassified"
                self.import_kwh[name] = self.import_kwh.get(name, 0.0) + delta
                zerohero = tariff.get("zerohero") or {}
                if in_windows(zerohero.get("windows") or [], minute):
                    self.zerohero_import_kwh += delta
        if export_reading is not None:
            delta = self._delta(self.last_export, export_reading)
            self.last_export = export_reading
            if delta > 0:
                component = export_component_at(tariff, moment)
                name = component["name"] if component else "unclassified"
                self.export_kwh[name] = self.export_kwh.get(name, 0.0) + delta
                if super_export_active(tariff, moment):
                    self.super_export_kwh += delta

    def zerohero_state(self, tariff: dict[str, Any], now: datetime) -> str | None:
        info = tariff.get("zerohero")
        if not info or not info.get("windows"):
            return None
        threshold = info.get("threshold_kwh") or 0.0
        if self.zerohero_import_kwh > threshold + 1e-9:
            return "missed"
        windows = info["windows"]
        minute = _minute(now)
        if minute < windows[0][0]:
            return "pending"
        if in_windows(windows, minute):
            return "on_track"
        return "earned"

    def super_export_remaining(self, tariff: dict[str, Any]) -> float | None:
        cap = (tariff.get("super_export") or {}).get("cap_kwh")
        if cap is None:
            return None
        return round(max(0.0, cap - self.super_export_kwh), 3)

    def cost(self, tariff: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Return today's estimated cost and its breakdown."""
        rates = {c["name"]: c.get("rate") for c in tariff.get("components") or []}
        lines: dict[str, float] = {}
        missing: list[str] = []
        supply = (tariff.get("supply") or {}).get("rate")
        if supply is not None:
            lines["supply"] = supply
        else:
            missing.append("supply")
        for name, kwh in self.import_kwh.items():
            rate = rates.get(name)
            if rate is None:
                missing.append(name)
                continue
            lines[name] = lines.get(name, 0.0) + kwh * rate
        for name, kwh in self.export_kwh.items():
            rate = rates.get(name)
            if rate is None:
                missing.append(name)
                continue
            lines[name] = lines.get(name, 0.0) - kwh * rate
        se = tariff.get("super_export") or {}
        if se.get("rate") and self.super_export_kwh:
            cap = se.get("cap_kwh")
            credited = (
                self.super_export_kwh if cap is None else min(self.super_export_kwh, cap)
            )
            extra = self.super_export_kwh - credited
            lines[se["name"]] = -(
                credited * se["rate"] + extra * (se.get("rate_after_cap") or 0.0)
            )
        zerohero = self.zerohero_state(tariff, now)
        zh = tariff.get("zerohero") or {}
        if zerohero == "earned" and zh.get("credit"):
            lines[zh["name"]] = -zh["credit"]
        total = round(sum(lines.values()), 2)
        return {
            "total": total,
            "lines": {k: round(v, 3) for k, v in sorted(lines.items())},
            "missing_rates": sorted(set(missing)),
            "zerohero": zerohero,
            "zerohero_window_import_kwh": round(self.zerohero_import_kwh, 4),
            "zerohero_credit_pending": (
                zh.get("credit") if zerohero in {"pending", "on_track"} else None
            ),
            "super_export_kwh": round(self.super_export_kwh, 3),
            "super_export_remaining_kwh": self.super_export_remaining(tariff),
            "import_kwh": {k: round(v, 3) for k, v in sorted(self.import_kwh.items())},
            "export_kwh": {k: round(v, 3) for k, v in sorted(self.export_kwh.items())},
        }
