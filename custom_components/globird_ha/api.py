"""GloBird customer portal API client and data helpers."""

from __future__ import annotations

import base64
import calendar
import html
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from yarl import URL

from .const import BASE_URL, DEFAULT_USAGE_DAYS, SENSITIVE_KEYS

_LOGGER = logging.getLogger(__name__)

ATTR_RECENT_ROW_LIMIT = 7


class GloBirdApiError(Exception):
    """Base GloBird API error."""


class GloBirdAuthError(GloBirdApiError):
    """Authentication failed."""


class GloBirdCaptchaRequired(GloBirdAuthError):
    """The portal requested captcha verification."""


class GloBirdSessionExpired(GloBirdAuthError):
    """The current session is not authorised."""


def _as_float(value: Any) -> float | None:
    """Return a float for numeric values, otherwise None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, precision: int = 3) -> float | None:
    """Round a numeric value while preserving None."""
    if value is None:
        return None
    return round(value, precision)


def _payload_data(payload: dict[str, Any] | None) -> Any:
    """Return the data object from a standard GloBird API payload."""
    if not isinstance(payload, dict):
        return None
    return payload.get("data")


def _date_key(value: dict[str, Any], *keys: str) -> str:
    """Return the first populated date-ish field from a row."""
    for key in keys:
        found = value.get(key)
        if found:
            return str(found)
    return ""


def _parse_date(value: Any) -> date | None:
    """Parse a portal date value."""
    if not value:
        return None
    raw = str(value).split("T")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _cost_category(row: dict[str, Any]) -> str:
    """Return a normalized cost category name."""
    return str(row.get("chargeCategory") or "unknown").strip()


def _is_supply_cost(row: dict[str, Any]) -> bool:
    """Return whether a cost row is only the fixed supply charge."""
    return _cost_category(row).upper() == "SUPPLY"


def _is_complete_cost_day(rows: list[dict[str, Any]]) -> bool:
    """Return whether a day has more than the early fixed supply-charge row."""
    return any(not _is_supply_cost(row) for row in rows)


def redact_sensitive(value: Any) -> Any:
    """Redact sensitive portal data for diagnostics."""
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in SENSITIVE_KEYS:
                redacted[key] = "**REDACTED**"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    return value


def _recent_rows(value: Any, limit: int = ATTR_RECENT_ROW_LIMIT) -> list[Any]:
    """Return the most recent rows from a list for recorder-safe attributes."""
    if not isinstance(value, list):
        return []
    return value[-limit:]


def _compact_usage_register(register: dict[str, Any]) -> dict[str, Any]:
    """Return register metadata without nested daily interval data."""
    return {
        "key": register.get("key"),
        "suffix": register.get("suffix"),
        "chargeType": register.get("chargeType"),
        "chargeCategoryCode": register.get("chargeCategoryCode"),
        "direction": register.get("direction"),
        "days": register.get("days"),
        "total": register.get("total"),
        "latest_day": register.get("latest_day"),
        "latest_day_usage": register.get("latest_day_usage"),
    }


def _usage_latest_day(summary: dict[str, Any], daily: list[Any]) -> Any:
    """Return the latest day for a daily usage attribute list."""
    if daily:
        latest = daily[-1]
        if isinstance(latest, dict):
            return latest.get("readDate")
    return summary.get("latest_day")


def usage_attributes(
    summary: dict[str, Any],
    *,
    direction: str,
    include_latest_intervals: bool = False,
    include_intervals_by_day: bool = False,
) -> dict[str, Any]:
    """Return recorder-safe usage attributes for import or export sensors."""
    daily_key = "export_daily" if direction == "export" else "daily"
    daily = summary.get(daily_key, [])
    daily_rows = daily if isinstance(daily, list) else []
    registers = [
        _compact_usage_register(register)
        for register in summary.get("registers", [])
        if isinstance(register, dict) and register.get("direction") == direction
    ]

    attrs: dict[str, Any] = {
        "days": len(daily_rows),
        "latest_day": _usage_latest_day(summary, daily_rows),
        "daily": _recent_rows(daily_rows),
        "daily_count": len(daily_rows),
        "daily_truncated": len(daily_rows) > ATTR_RECENT_ROW_LIMIT,
        "registers": registers,
    }
    if include_latest_intervals:
        attrs["latest_intervals"] = summary.get("latest_intervals", [])
    if include_intervals_by_day:
        intervals_by_day = summary.get("intervals_by_day", [])
        intervals_by_day_rows = (
            intervals_by_day if isinstance(intervals_by_day, list) else []
        )
        attrs["intervals_by_day"] = _recent_rows(intervals_by_day_rows)
        attrs["intervals_by_day_count"] = len(intervals_by_day_rows)
        attrs["intervals_by_day_truncated"] = (
            len(intervals_by_day_rows) > ATTR_RECENT_ROW_LIMIT
        )
    return attrs


def cost_attributes(summary: dict[str, Any]) -> dict[str, Any]:
    """Return recorder-safe cost summary attributes."""
    daily = summary.get("daily", [])
    daily_rows = daily if isinstance(daily, list) else []
    available_daily = summary.get("available_daily", [])
    available_rows = available_daily if isinstance(available_daily, list) else []
    return {
        "days": summary.get("days"),
        "total_quantity": summary.get("total_quantity"),
        "latest_day": summary.get("latest_day"),
        "latest_available_day": summary.get("latest_available_day"),
        "latest_available_day_complete": summary.get("latest_available_day_complete"),
        "incomplete_days": summary.get("incomplete_days", []),
        "daily": _recent_rows(daily_rows),
        "daily_count": len(daily_rows),
        "daily_truncated": len(daily_rows) > ATTR_RECENT_ROW_LIMIT,
        "daily_totals": _recent_rows(summary.get("daily_totals", [])),
        "available_daily": _recent_rows(available_rows),
        "available_daily_count": len(available_rows),
        "available_daily_truncated": len(available_rows) > ATTR_RECENT_ROW_LIMIT,
        "categories": summary.get("categories", []),
        "charge_type_totals": summary.get("charge_type_totals", []),
    }


def calculated_cost_attributes(summary: dict[str, Any]) -> dict[str, Any]:
    """Return recorder-safe calculated (rate-schedule based) cost attributes."""
    daily = summary.get("daily", [])
    daily_rows = daily if isinstance(daily, list) else []
    latest = daily_rows[-1] if daily_rows else None
    return {
        "days": summary.get("days", 0),
        "latest_day": summary.get("latest_day"),
        "total_cost": summary.get("total_cost"),
        "latest_day_periods": latest.get("periods", []) if latest else [],
        "latest_day_unassigned_kwh": latest.get("unassigned_kwh") if latest else None,
        "daily": _recent_rows(daily_rows),
        "daily_count": len(daily_rows),
        "daily_truncated": len(daily_rows) > ATTR_RECENT_ROW_LIMIT,
    }


def extract_accounts_and_services(
    current_user_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract accounts and active services from currentuser payload."""
    data = _payload_data(current_user_payload) or {}
    accounts: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []

    for account in data.get("accounts", []) or []:
        account_id = account.get("accountId")
        account_summary = {
            "accountId": account_id,
            "accountNumber": account.get("accountNumber"),
            "accountAddress": account.get("accountAddress"),
            "service_count": len(account.get("services", []) or []),
        }
        accounts.append(account_summary)

        for service in account.get("services", []) or []:
            if service.get("closedDate"):
                continue
            svc_status = str(service.get("status") or "").lower()
            if svc_status == "closed":
                continue

            service_type = str(service.get("serviceType") or "").lower()
            if service_type and not any(
                marker in service_type for marker in ("power", "electric", "gas")
            ):
                continue

            enriched = dict(service)
            enriched["accountId"] = account_id
            enriched["accountNumber"] = account.get("accountNumber")
            enriched["accountAddress"] = account.get("accountAddress")
            services.append(enriched)

    # Prefer power/electric services first to keep existing endpoint selection
    # behavior for mixed-service accounts.
    services.sort(
        key=lambda service: (
            0
            if any(
                marker in str(service.get("serviceType") or "").lower()
                for marker in ("power", "electric")
            )
            else 1
        )
    )

    return accounts, services


def service_id(service: dict[str, Any]) -> str:
    """Return a stable service identifier."""
    value = service.get("accountServiceId") or service.get("siteIdentifier")
    return str(value or "unknown")


_ACTIVE_METER_STATUSES = {"active", "current", "energized", "energised"}
_INACTIVE_METER_STATUSES = {
    "closed",
    "de-energised",
    "de-energized",
    "disconnected",
    "inactive",
    "removed",
}


def _normalised_meter_statuses(meter: dict[str, Any]) -> set[str]:
    """Return normalized meter status fields."""
    return {
        str(value).strip().lower()
        for value in (
            meter.get("serialStatus"),
            meter.get("suffixStatus"),
        )
        if value
    }


def _meter_status_rank(meter: dict[str, Any]) -> int:
    """Rank meters by whether the portal says they are usable."""
    statuses = _normalised_meter_statuses(meter)
    if statuses & _INACTIVE_METER_STATUSES:
        return 0
    if statuses & _ACTIVE_METER_STATUSES:
        return 3
    if not statuses:
        return 2
    return 1


def _meter_type_rank(meter: dict[str, Any]) -> int:
    """Prefer interval/smart meters over basic meters when both are usable."""
    meter_type = str(meter.get("meterReadType") or "").strip().lower()
    if meter_type == "basic":
        return 0
    if meter_type:
        return 1
    return 0


def meter_type_description(
    meter_types_payload: dict[str, Any] | None,
    serial_number: Any,
) -> str | None:
    """Look up a human-readable meter type (e.g. 'Smart') by serial number."""
    if not serial_number:
        return None
    lookup = _payload_data(meter_types_payload)
    if not isinstance(lookup, dict):
        return None
    return lookup.get(str(serial_number))


def select_meter_for_service(
    service: dict[str, Any],
    meters_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Select the best available meter row for a service."""
    raw = _payload_data(meters_payload)

    # API may return the list directly or wrapped in a nested dict
    if isinstance(raw, list):
        meters: list[dict[str, Any]] = raw
    elif isinstance(raw, dict):
        meters = []
        for key in ("data", "meters", "items", "readMeters"):
            val = raw.get(key)
            if isinstance(val, list):
                meters = val
                break
    else:
        return None

    if not meters:
        return None

    identifier = str(service.get("siteIdentifier") or "")
    if identifier:
        matched = [
            m
            for m in meters
            if str(m.get("siteIdentifier") or m.get("nmi") or "") == identifier
        ]
        if matched:
            meters = matched
        elif any(m.get("siteIdentifier") or m.get("nmi") for m in meters):
            # Do not attach another service's identified meter to this service.
            return None

    return max(
        enumerate(meters),
        key=lambda item: (
            _meter_status_rank(item[1]),
            _meter_type_rank(item[1]),
            -item[0],
        ),
    )[1]


def _build_register_summary(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarise a list of usage rows for a single register (E1 or B1).

    Each day has multiple rows (one per time-of-use period, e.g. Peak/Offpeak).
    Group by date so that daily totals and latest_day_usage are correct sums,
    not a single time-of-use period's value. The portal attaches the *same*
    full-day usageArray to every time-of-use row for a given suffix (only the
    scalar `usage` differs per period), so interval arrays must be taken once
    per (date, suffix) pair rather than summed across TOU rows, or they end
    up double- (or triple-) counted for time-of-use tariffs.
    """
    if not rows:
        return {
            "days": 0,
            "total": None,
            "latest_day": None,
            "latest_day_usage": None,
            "daily": [],
            "latest_intervals": [],
        }

    # Group rows by date
    by_date: dict[str, dict[str, Any]] = {}
    seen_interval_keys: set[tuple[str, str]] = set()
    for row in rows:
        d = row.get("readDate") or ""
        usage = _as_float(row.get("usage")) or 0.0
        if d not in by_date:
            by_date[d] = {
                "readDate": d,
                "usage": 0.0,
                "meterStatus": row.get("meterStatus"),
                "minQualityMethod": row.get("minQualityMethod"),
                "intervals": None,
            }
        by_date[d]["usage"] += usage

        arr = row.get("usageArray")
        interval_key = (d, str(row.get("suffix") or ""))
        if (
            isinstance(arr, list)
            and arr
            and interval_key not in seen_interval_keys
        ):
            seen_interval_keys.add(interval_key)
            existing = by_date[d]["intervals"]
            if existing is None:
                by_date[d]["intervals"] = list(arr)
            else:
                for i, v in enumerate(arr):
                    if i < len(existing):
                        existing[i] = (_as_float(existing[i]) or 0.0) + (
                            _as_float(v) or 0.0
                        )

    total = sum(v["usage"] for v in by_date.values())
    latest_date = max(by_date) if by_date else None
    latest_entry = by_date[latest_date] if latest_date else None

    daily = [
        {
            "readDate": v["readDate"],
            "usage": _round(v["usage"]),
            "meterStatus": v["meterStatus"],
            "minQualityMethod": v["minQualityMethod"],
        }
        for v in sorted(by_date.values(), key=lambda x: x["readDate"])
    ]

    latest_intervals: list[Any] = []
    if latest_entry and isinstance(latest_entry["intervals"], list):
        latest_intervals = [_round(_as_float(v), 5) for v in latest_entry["intervals"]]

    intervals_by_day = [
        {
            "readDate": v["readDate"],
            "intervals": [_round(_as_float(x), 5) for x in v["intervals"]],
        }
        for v in sorted(by_date.values(), key=lambda x: x["readDate"])
        if isinstance(v["intervals"], list)
    ]

    return {
        "days": len(by_date),
        "total": _round(total),
        "latest_day": latest_date,
        "latest_day_usage": _round(latest_entry["usage"]) if latest_entry else None,
        "daily": daily,
        "latest_intervals": latest_intervals,
        "intervals_by_day": intervals_by_day,
    }


def _usage_register_key(row: dict[str, Any]) -> str:
    """Return the portal's display key for a usage register row."""
    parts = [
        str(row.get("suffix") or "").strip(),
        str(row.get("chargeType") or "").strip(),
    ]
    key = "-".join(part for part in parts if part)
    return key or "unknown"


def _is_export_register(row: dict[str, Any]) -> bool:
    """Return whether a usage row represents export/feed-in energy."""
    suffix = str(row.get("suffix") or "").strip().upper()
    if suffix.startswith("B"):
        return True

    direction = str(row.get("direction") or "").strip().lower()
    if direction in {"export", "feed-in", "feed in", "solar"}:
        return True

    category = str(row.get("chargeCategoryCode") or "").strip().lower()
    charge_type = str(row.get("chargeType") or "").strip().lower()
    export_markers = ("solar", "export", "feed")
    return any(marker in category for marker in export_markers) or any(
        marker in charge_type for marker in export_markers
    )


def _build_usage_register_summaries(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build summaries for every returned usage register/category."""
    by_register: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_register.setdefault(_usage_register_key(row), []).append(row)

    summaries: list[dict[str, Any]] = []
    for key in sorted(by_register):
        register_rows = by_register[key]
        first = register_rows[0]
        summary = _build_register_summary(register_rows)
        summaries.append(
            {
                "key": key,
                "suffix": first.get("suffix"),
                "chargeType": first.get("chargeType"),
                "chargeCategoryCode": first.get("chargeCategoryCode"),
                "direction": "export" if _is_export_register(first) else "import",
                **summary,
            }
        )
    return summaries


def build_usage_summary(
    usage_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build recorder-safe usage summary split by register (E1 import / B1 solar export)."""
    rows = _payload_data(usage_payload)
    if not isinstance(rows, list):
        rows = []

    if not rows:
        return {
            "days": 0,
            "total_usage": None,
            "latest_day": None,
            "latest_day_usage": None,
            "daily": [],
            "latest_intervals": [],
            "intervals_by_day": [],
            "total_export": None,
            "latest_day_export": None,
            "export_daily": [],
            "registers": [],
        }

    import_rows = [r for r in rows if not _is_export_register(r)]
    export_rows = [r for r in rows if _is_export_register(r)]

    import_summary = _build_register_summary(import_rows)
    export_summary = _build_register_summary(export_rows)

    return {
        "days": import_summary["days"],
        "total_usage": import_summary["total"],
        "latest_day": import_summary["latest_day"],
        "latest_day_usage": import_summary["latest_day_usage"],
        "daily": import_summary["daily"],
        "latest_intervals": import_summary["latest_intervals"],
        "intervals_by_day": import_summary["intervals_by_day"],
        "total_export": export_summary["total"],
        "latest_day_export": export_summary["latest_day_usage"],
        "export_daily": export_summary["daily"],
        "registers": _build_usage_register_summaries(rows),
    }


def build_gas_reading_summary(
    usage_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a summary for gas basic meter index readings."""
    rows = _payload_data(usage_payload)
    if not isinstance(rows, dict):
        rows = {}

    history: list[dict[str, Any]] = []
    for source, source_rows in rows.items():
        if not isinstance(source_rows, list):
            continue

        for row in source_rows:
            if not isinstance(row, dict):
                continue

            reading = _as_float(row.get("readIndex"))
            read_day = _parse_date(row.get("readDate"))
            if reading is None or read_day is None:
                continue

            history.append(
                {
                    "date": read_day.isoformat(),
                    "read_index": _round(reading, 6),
                    "source": source,
                    "serial": row.get("serial"),
                    "quality_method": row.get("minQualityMethod"),
                }
            )

    history.sort(
        key=lambda row: (
            str(row.get("date") or ""),
            float(row.get("read_index") or 0.0),
            str(row.get("source") or ""),
        )
    )

    latest = history[-1] if history else None
    recent = _recent_rows(history)
    return {
        "latest_reading": latest.get("read_index") if latest else None,
        "latest_reading_date": latest.get("date") if latest else None,
        "latest_reading_source": latest.get("source") if latest else None,
        "latest_reading_serial": latest.get("serial") if latest else None,
        "latest_reading_quality_method": (
            latest.get("quality_method") if latest else None
        ),
        "history": history,
        "history_recent": recent,
        "history_count": len(history),
        "history_truncated": len(history) > ATTR_RECENT_ROW_LIMIT,
    }


def build_cost_summary(cost_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build recorder-safe cost summary."""
    rows = _payload_data(cost_payload)
    if not isinstance(rows, list):
        rows = []

    daily: list[dict[str, Any]] = []
    daily_totals: dict[str, float] = {}
    available_daily: list[dict[str, Any]] = []
    categories: dict[str, dict[str, Any]] = {}
    charge_types: dict[str, dict[str, Any]] = {}
    total_amount = 0.0
    total_quantity = 0.0
    grouped_rows: dict[str, list[dict[str, Any]]] = {}

    for raw_row in rows:
        dk = _date_key(raw_row, "date")
        if dk:
            grouped_rows.setdefault(dk, []).append(raw_row)

    complete_days = {
        day for day, day_rows in grouped_rows.items() if _is_complete_cost_day(day_rows)
    }
    latest_available_day = max(grouped_rows) if grouped_rows else None
    latest_day = max(complete_days) if complete_days else None

    for row in rows:
        amount = _as_float(row.get("amount")) or 0.0
        quantity = _as_float(row.get("quantity")) or 0.0
        dk = _date_key(row, "date")
        item = {
            "date": row.get("date"),
            "amount": _round(amount, 2),
            "quantity": _round(quantity),
            "chargeCategory": row.get("chargeCategory"),
            "chargeType": row.get("chargeType"),
            "complete": dk in complete_days,
        }
        available_daily.append(item)
        if dk not in complete_days:
            continue

        total_amount += amount
        total_quantity += quantity
        daily_totals[dk] = daily_totals.get(dk, 0.0) + amount
        category = _cost_category(row)
        if category not in categories:
            categories[category] = {
                "chargeCategory": row.get("chargeCategory"),
                "amount": 0.0,
                "quantity": 0.0,
            }
        categories[category]["amount"] += amount
        categories[category]["quantity"] += quantity

        charge_type = row.get("chargeType")
        if charge_type:
            charge_type_key = str(charge_type).strip()
            if charge_type_key not in charge_types:
                charge_types[charge_type_key] = {
                    "chargeType": charge_type,
                    "amount": 0.0,
                    "quantity": 0.0,
                }
            charge_types[charge_type_key]["amount"] += amount
            charge_types[charge_type_key]["quantity"] += quantity

        daily.append(item)

    # GloBird returns multiple rows per day (SOLAR, USAGE, SUPPLY, etc.). Sum all
    # complete-day rows so early supply-only rows don't become the latest daily cost.
    latest_day_amount: float | None = None
    latest_day_zerohero_credit: float | None = None
    if latest_day:
        latest_day_amount = _round(
            sum(e["amount"] for e in daily if e["date"] == latest_day), 2
        )
        zerohero_total = sum(
            e["amount"]
            for e in daily
            if e["date"] == latest_day
            and str(e.get("chargeCategory") or "").strip().lower() == "zerohero credit"
        )
        latest_day_zerohero_credit = _round(zerohero_total, 2)

    return {
        "days": len(daily),
        "total_amount": _round(total_amount, 2),
        "total_quantity": _round(total_quantity),
        "latest_day": latest_day,
        "latest_day_amount": latest_day_amount,
        "latest_available_day": latest_available_day,
        "latest_available_day_complete": (
            latest_available_day is not None and latest_available_day == latest_day
        ),
        "latest_day_zerohero_credit": latest_day_zerohero_credit,
        "latest_day_zerohero_achieved": (
            latest_day_zerohero_credit is not None and latest_day_zerohero_credit != 0
        ),
        "daily": daily,
        "daily_totals": [
            {"date": day, "amount": _round(amount, 2)}
            for day, amount in sorted(daily_totals.items())
        ],
        "available_daily": available_daily,
        "incomplete_days": sorted(set(grouped_rows) - complete_days),
        "projected_month": _build_projected_month_summary(daily),
        "categories": [
            {
                "chargeCategory": value["chargeCategory"],
                "amount": _round(value["amount"], 2),
                "quantity": _round(value["quantity"]),
            }
            for _, value in sorted(categories.items())
        ],
        "charge_type_totals": [
            {
                "chargeType": value["chargeType"],
                "amount": _round(value["amount"], 2),
                "quantity": _round(value["quantity"]),
            }
            for _, value in sorted(charge_types.items())
        ],
    }


def build_latest_data_status(
    usage_summary: dict[str, Any] | None,
    cost_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build readiness state for the latest complete service data date."""
    usage_summary = usage_summary or {}
    cost_summary = cost_summary or {}

    latest_usage_day = usage_summary.get("latest_day")
    latest_cost_day = cost_summary.get("latest_day")
    latest_available_cost_day = cost_summary.get("latest_available_day")
    latest_available_cost_day_complete = cost_summary.get(
        "latest_available_day_complete"
    )

    usage_day = _parse_date(latest_usage_day)
    cost_day = _parse_date(latest_cost_day)
    available_cost_day = _parse_date(latest_available_cost_day)

    latest_ready_day = latest_cost_day if cost_day else None
    if usage_day and cost_day and cost_day > usage_day:
        latest_ready_day = None

    if not usage_day and not cost_day:
        status = "no_data"
    elif usage_day and (not cost_day or cost_day < usage_day):
        status = "waiting_for_cost"
    elif cost_day and usage_day and cost_day > usage_day:
        status = "waiting_for_usage"
    elif (
        available_cost_day
        and cost_day
        and available_cost_day > cost_day
        and latest_available_cost_day_complete is False
    ):
        status = "waiting_for_cost"
    else:
        status = "ready"

    return {
        "status": status,
        "latest_ready_day": latest_ready_day,
        "latest_usage_day": latest_usage_day,
        "latest_cost_day": latest_cost_day,
        "latest_available_cost_day": latest_available_cost_day,
        "latest_available_cost_day_complete": latest_available_cost_day_complete,
        "incomplete_cost_days": cost_summary.get("incomplete_days", []),
    }


def all_services_ready_for_day(
    service_data: dict[str, Any] | None,
    target_day: date,
) -> bool:
    """Return whether every discovered service is ready for target_day or newer."""
    if not isinstance(service_data, dict) or not service_data:
        return False

    for detail in service_data.values():
        if not isinstance(detail, dict):
            return False

        latest_status = detail.get("latest_data_status")
        if not isinstance(latest_status, dict):
            return False

        if latest_status.get("status") != "ready":
            return False

        ready_day = _parse_date(latest_status.get("latest_ready_day"))
        if ready_day is None or ready_day < target_day:
            return False

    return True


def _build_projected_month_summary(
    daily: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    """Project the current calendar month from completed daily cost rows."""
    today = today or date.today()
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    daily_totals: dict[date, float] = {}
    for row in daily:
        row_date = _parse_date(row.get("date"))
        if row_date is None or row_date < month_start or row_date > today:
            continue
        daily_totals[row_date] = daily_totals.get(row_date, 0.0) + (
            _as_float(row.get("amount")) or 0.0
        )

    if not daily_totals:
        return {
            "month": month_start.strftime("%Y-%m"),
            "cost_to_date": None,
            "projected_cost": None,
            "completed_days": 0,
            "days_in_month": days_in_month,
            "latest_day": None,
        }

    latest_day = max(daily_totals)
    completed_days = latest_day.day
    cost_to_date = sum(daily_totals.values())
    projected_cost = (
        cost_to_date / completed_days * days_in_month if completed_days else None
    )

    return {
        "month": month_start.strftime("%Y-%m"),
        "cost_to_date": _round(cost_to_date, 2),
        "projected_cost": _round(projected_cost, 2),
        "completed_days": completed_days,
        "days_in_month": days_in_month,
        "latest_day": latest_day.isoformat(),
    }


def _parse_clock_minutes(value: Any) -> int:
    """Parse an HH:MM clock string into minutes since midnight (24:00 -> 1440)."""
    raw = str(value).strip()
    parts = raw.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid time '{value}'")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{value}'")
    return hour * 60 + minute


def parse_tou_rate_schedule(raw: str | None) -> dict[str, Any] | None:
    """Parse and validate a user-configured time-of-use rate schedule.

    GloBird's API does not expose usable $/kWh rate data, so a schedule
    entered from the customer's own contract/bill is the only source. Expected
    shape (rates in $/kWh, supply_charge in $/day, windows as [start, end)
    24-hour clock pairs, "24:00" meaning midnight at the end of the day)::

        {
            "supply_charge": 1.12,
            "periods": [
                {"name": "Offpeak Usage", "rate": 0.28,
                 "windows": [["00:00", "15:00"], ["21:00", "24:00"]]},
                {"name": "Peak Usage", "rate": 0.45,
                 "windows": [["15:00", "21:00"]]}
            ]
        }

    `name` should match the usage register's chargeType (e.g. "Peak Usage")
    so the breakdown lines up with what the portal itself reports, but it is
    only used as a label here - the windows are what select the rate.

    Returns None when raw is empty/not configured. Raises ValueError for
    malformed input so callers can surface a clear config error.
    """
    text = (raw or "").strip()
    if not text:
        return None

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("TOU rate schedule must be a JSON object")

    periods_raw = parsed.get("periods")
    if not isinstance(periods_raw, list) or not periods_raw:
        raise ValueError("TOU rate schedule must include a non-empty 'periods' list")

    periods: list[dict[str, Any]] = []
    for period in periods_raw:
        if not isinstance(period, dict):
            raise ValueError("Each TOU period must be a JSON object")
        name = str(period.get("name") or "").strip()
        rate = _as_float(period.get("rate"))
        windows_raw = period.get("windows")
        if (
            not name
            or rate is None
            or not isinstance(windows_raw, list)
            or not windows_raw
        ):
            raise ValueError(f"Invalid TOU period: {period!r}")

        windows: list[tuple[int, int]] = []
        for window in windows_raw:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                raise ValueError(f"Invalid TOU window: {window!r}")
            start = _parse_clock_minutes(window[0])
            end = _parse_clock_minutes(window[1])
            if end <= start:
                raise ValueError(f"TOU window end must be after start: {window!r}")
            windows.append((start, end))

        periods.append({"name": name, "rate": rate, "windows": windows})

    supply_charge = _as_float(parsed.get("supply_charge")) or 0.0
    return {"supply_charge": supply_charge, "periods": periods}


def calculate_tou_cost(
    intervals_by_day: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
) -> dict[str, Any]:
    """Calculate estimated daily cost from interval usage and a TOU rate schedule."""
    empty: dict[str, Any] = {
        "days": 0,
        "daily": [],
        "latest_day": None,
        "latest_day_cost": None,
        "total_cost": None,
    }
    if not schedule or not isinstance(intervals_by_day, list):
        return empty

    periods = schedule.get("periods") or []
    supply_charge = schedule.get("supply_charge") or 0.0

    daily: list[dict[str, Any]] = []
    for row in intervals_by_day:
        if not isinstance(row, dict):
            continue
        read_date = row.get("readDate")
        intervals = row.get("intervals")
        if not read_date or not isinstance(intervals, list) or not intervals:
            continue

        minutes_per_interval = 1440 // len(intervals)
        period_totals: dict[str, dict[str, Any]] = {
            period["name"]: {"kwh": 0.0, "cost": 0.0, "rate": period["rate"]}
            for period in periods
        }
        unassigned_kwh = 0.0

        for index, value in enumerate(intervals):
            usage = _as_float(value) or 0.0
            interval_start = index * minutes_per_interval
            matched = next(
                (
                    period
                    for period in periods
                    if any(
                        start <= interval_start < end
                        for start, end in period["windows"]
                    )
                ),
                None,
            )
            if matched is None:
                unassigned_kwh += usage
                continue
            bucket = period_totals[matched["name"]]
            bucket["kwh"] += usage
            bucket["cost"] += usage * matched["rate"]

        usage_cost = sum(bucket["cost"] for bucket in period_totals.values())
        total_cost = usage_cost + supply_charge

        daily.append(
            {
                "readDate": read_date,
                "total_cost": _round(total_cost, 2),
                "usage_cost": _round(usage_cost, 2),
                "supply_charge": _round(supply_charge, 2),
                "unassigned_kwh": _round(unassigned_kwh, 3),
                "periods": [
                    {
                        "name": name,
                        "kwh": _round(bucket["kwh"], 3),
                        "cost": _round(bucket["cost"], 2),
                        "rate": bucket["rate"],
                    }
                    for name, bucket in period_totals.items()
                ],
            }
        )

    if not daily:
        return empty

    daily.sort(key=lambda row: row["readDate"])
    latest = daily[-1]
    return {
        "days": len(daily),
        "daily": daily,
        "latest_day": latest["readDate"],
        "latest_day_cost": latest["total_cost"],
        "total_cost": _round(sum(row["total_cost"] for row in daily), 2),
    }


def parse_gas_rate_schedule(raw: str | None) -> dict[str, Any] | None:
    """Parse and validate a user-configured gas rate schedule.

    Gas billing shapes nothing like electricity time-of-use: it is a daily
    charge plus a seasonal, inclining-block $/MJ rate applied to *average*
    daily usage across the meter read period (gas basic meters are read
    periodically, not daily, so retailers bill on the average). GloBird
    reports gas meter reads in the read unit (typically m3), so a heating
    value conversion factor to MJ is required. Expected shape::

        {
            "daily_charge": 0.58685,
            "conversion_mj_per_unit": 38.6,
            "seasons": [
                {"name": "Summer", "months": [10, 11, 12, 1, 2, 3],
                 "tiers": [{"limit_mj_per_day": 20.70, "rate": 0.03735},
                           {"limit_mj_per_day": null, "rate": 0.02934}]},
                {"name": "Winter", "months": [4, 5, 6, 7, 8, 9],
                 "tiers": [{"limit_mj_per_day": 20.70, "rate": 0.03735},
                           {"limit_mj_per_day": null, "rate": 0.02934}]}
            ]
        }

    Tiers apply in order to average daily MJ usage; a `limit_mj_per_day` of
    null means "the remainder" and should only appear on the last tier.

    Returns None when raw is empty/not configured. Raises ValueError for
    malformed input so callers can surface a clear config error.
    """
    text = (raw or "").strip()
    if not text:
        return None

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gas rate schedule must be a JSON object")

    seasons_raw = parsed.get("seasons")
    if not isinstance(seasons_raw, list) or not seasons_raw:
        raise ValueError("Gas rate schedule must include a non-empty 'seasons' list")

    seasons: list[dict[str, Any]] = []
    for season in seasons_raw:
        if not isinstance(season, dict):
            raise ValueError("Each gas season must be a JSON object")

        months_raw = season.get("months")
        if not isinstance(months_raw, list) or not months_raw:
            raise ValueError(f"Invalid gas season months: {season!r}")
        months: list[int] = []
        for value in months_raw:
            month = int(value)
            if not (1 <= month <= 12):
                raise ValueError(f"Invalid month '{value}' in gas season: {season!r}")
            months.append(month)

        tiers_raw = season.get("tiers")
        if not isinstance(tiers_raw, list) or not tiers_raw:
            raise ValueError(f"Invalid gas season tiers: {season!r}")
        tiers: list[dict[str, Any]] = []
        for tier in tiers_raw:
            if not isinstance(tier, dict):
                raise ValueError(f"Invalid gas tier: {tier!r}")
            rate = _as_float(tier.get("rate"))
            if rate is None:
                raise ValueError(f"Invalid gas tier rate: {tier!r}")
            limit_raw = tier.get("limit_mj_per_day")
            limit = _as_float(limit_raw) if limit_raw is not None else None
            tiers.append({"limit_mj_per_day": limit, "rate": rate})

        seasons.append(
            {
                "name": str(season.get("name") or "").strip() or None,
                "months": months,
                "tiers": tiers,
            }
        )

    conversion = _as_float(parsed.get("conversion_mj_per_unit"))
    if conversion is None or conversion <= 0:
        raise ValueError(
            "Gas rate schedule must include a positive 'conversion_mj_per_unit'"
        )
    daily_charge = _as_float(parsed.get("daily_charge")) or 0.0

    return {
        "daily_charge": daily_charge,
        "conversion_mj_per_unit": conversion,
        "seasons": seasons,
    }


def _gas_season_for_date(day: date, seasons: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the configured season covering a given date's month."""
    return next((season for season in seasons if day.month in season["months"]), None)


def _gas_tiered_usage_cost(daily_mj: float, tiers: list[dict[str, Any]]) -> float:
    """Apply inclining-block $/MJ tiers to an average daily MJ usage figure."""
    remaining = daily_mj
    cost = 0.0
    for tier in tiers:
        if remaining <= 0:
            break
        limit = tier["limit_mj_per_day"]
        if limit is None:
            cost += remaining * tier["rate"]
            remaining = 0.0
            break
        take = min(remaining, limit)
        cost += take * tier["rate"]
        remaining -= take
    return cost


def calculate_gas_cost(
    history: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
) -> dict[str, Any]:
    """Calculate estimated gas cost per meter-read period from a rate schedule.

    Gas basic meters are read periodically (not daily), so each consecutive
    pair of reads for the same meter becomes one billed period: usage is
    converted to MJ, averaged over the days in that period, tiered per the
    matching season, and a per-day supply charge is added for the period.
    Meter replacements (a lower read on a new serial) are skipped rather
    than treated as negative usage, mirroring the recorder statistics import.
    """
    empty: dict[str, Any] = {
        "periods": [],
        "latest_period_cost": None,
        "total_cost": None,
    }
    if not schedule or not isinstance(history, list):
        return empty

    rows = sorted(
        (
            row
            for row in history
            if isinstance(row, dict)
            and _parse_date(row.get("date")) is not None
            and isinstance(row.get("read_index"), (int, float))
        ),
        key=lambda row: (str(row.get("date") or ""), str(row.get("serial") or "")),
    )
    if len(rows) < 2:
        return empty

    conversion = schedule["conversion_mj_per_unit"]
    daily_charge = schedule.get("daily_charge") or 0.0
    seasons = schedule.get("seasons") or []

    meter_high_water: dict[str, float] = {}
    periods: list[dict[str, Any]] = []
    previous_row: dict[str, Any] | None = None

    for row in rows:
        reading = float(row["read_index"])
        meter_key = str(row.get("serial") or "unknown")
        previous_reading = meter_high_water.get(meter_key)
        meter_high_water[meter_key] = (
            reading if previous_reading is None else max(previous_reading, reading)
        )

        if previous_row is not None and previous_reading is not None and reading > previous_reading:
            start_day = _parse_date(previous_row.get("date"))
            end_day = _parse_date(row.get("date"))
            days = (end_day - start_day).days if start_day and end_day else 0
            if days > 0:
                units_used = reading - previous_reading
                mj_used = units_used * conversion
                avg_daily_mj = mj_used / days
                season = _gas_season_for_date(end_day, seasons)
                if season is not None:
                    usage_cost = _gas_tiered_usage_cost(avg_daily_mj, season["tiers"]) * days
                    daily_charge_cost = daily_charge * days
                    periods.append(
                        {
                            "start": start_day.isoformat(),
                            "end": end_day.isoformat(),
                            "days": days,
                            "mj_used": _round(mj_used, 3),
                            "avg_daily_mj": _round(avg_daily_mj, 3),
                            "season": season.get("name"),
                            "usage_cost": _round(usage_cost, 2),
                            "daily_charge_cost": _round(daily_charge_cost, 2),
                            "total_cost": _round(usage_cost + daily_charge_cost, 2),
                        }
                    )
        previous_row = row

    if not periods:
        return empty

    periods.sort(key=lambda period: period["end"])
    latest = periods[-1]
    return {
        "periods": periods,
        "latest_period_cost": latest["total_cost"],
        "total_cost": _round(sum(period["total_cost"] for period in periods), 2),
    }


def gas_cost_attributes(summary: dict[str, Any]) -> dict[str, Any]:
    """Return recorder-safe calculated gas cost attributes."""
    periods = summary.get("periods", [])
    periods = periods if isinstance(periods, list) else []
    return {
        "periods": len(periods),
        "latest_period_cost": summary.get("latest_period_cost"),
        "total_cost": summary.get("total_cost"),
        "recent_periods": _recent_rows(periods),
        "periods_truncated": len(periods) > ATTR_RECENT_ROW_LIMIT,
    }


def build_billing_period_projection(
    daily_totals: list[dict[str, Any]] | None,
    billing_period_start: date | None,
    *,
    period_days: int = 30,
) -> dict[str, Any]:
    """Project billing-period cost from completed daily net totals."""
    if billing_period_start is None or not isinstance(daily_totals, list):
        return {
            "billing_period_start": (
                billing_period_start.isoformat() if billing_period_start else None
            ),
            "cost_to_date": None,
            "projected_cost": None,
            "completed_days": 0,
            "period_days": period_days,
            "latest_day": None,
        }

    totals: dict[date, float] = {}
    for row in daily_totals:
        if not isinstance(row, dict):
            continue
        row_date = _parse_date(row.get("date"))
        if row_date is None or row_date < billing_period_start:
            continue
        totals[row_date] = totals.get(row_date, 0.0) + (
            _as_float(row.get("amount")) or 0.0
        )

    if not totals:
        return {
            "billing_period_start": billing_period_start.isoformat(),
            "cost_to_date": None,
            "projected_cost": None,
            "completed_days": 0,
            "period_days": period_days,
            "latest_day": None,
        }

    latest_day = max(totals)
    completed_days = max(1, (latest_day - billing_period_start).days + 1)
    cost_to_date = sum(totals.values())
    projected_cost = cost_to_date / completed_days * period_days

    return {
        "billing_period_start": billing_period_start.isoformat(),
        "cost_to_date": _round(cost_to_date, 2),
        "projected_cost": _round(projected_cost, 2),
        "completed_days": completed_days,
        "period_days": period_days,
        "latest_day": latest_day.isoformat(),
    }


def build_weather_summary(weather_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build a compact weather summary."""
    rows = _payload_data(weather_payload)
    if not isinstance(rows, list):
        rows = []

    latest = None
    for row in rows:
        if latest is None or _date_key(row, "dateAsDate") >= _date_key(
            latest, "dateAsDate"
        ):
            latest = row

    return {
        "days": len(rows),
        "latest_date": latest.get("dateAsDate") if latest else None,
        "latest_min_temp": latest.get("obMinTemp") if latest else None,
        "latest_max_temp": latest.get("obMaxTemp") if latest else None,
        "daily": [
            {
                "dateAsDate": row.get("dateAsDate"),
                "obMinTemp": row.get("obMinTemp"),
                "obMaxTemp": row.get("obMaxTemp"),
                "distanceMeters": row.get("distanceMeters"),
            }
            for row in rows
        ],
    }


def date_range_for_usage(
    days: int = DEFAULT_USAGE_DAYS,
) -> tuple[str, str, str, str, str, str]:
    """Return slash, dashed, and ISO date ranges for portal endpoints."""
    today = date.today()
    start = today - timedelta(days=days)
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(today, time.max, tzinfo=timezone.utc)
    return (
        start.strftime("%Y/%m/%d"),
        today.strftime("%Y/%m/%d"),
        start.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
        start_dt.isoformat().replace("+00:00", "Z"),
        end_dt.isoformat().replace("+00:00", "Z"),
    )


class GloBirdClient:
    """Async client for the GloBird customer portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        """Initialize the client."""
        self._base_url = base_url.rstrip("/")
        if session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False

        self._email: str | None = None
        self._password: str | None = None
        self._authenticated = False
        self._reauth_enabled = True

    @property
    def is_authenticated(self) -> bool:
        """Return whether this client believes it has an active session."""
        return self._authenticated

    def disable_reauth(self) -> None:
        """Suppress automatic re-authentication (use during bulk optional fetches)."""
        self._reauth_enabled = False

    def enable_reauth(self) -> None:
        """Re-enable automatic re-authentication."""
        self._reauth_enabled = True

    async def close(self) -> None:
        """Close the owned HTTP session."""
        if self._owns_session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        """Build portal-like request headers."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "User-Agent": "GloBird-HA/0.1",
        }

    async def _raw_request_json(
        self,
        method: str,
        path: str,
        *,
        json_data: Any | None = None,
        timeout: int = 30,
        allow_api_failure: bool = False,
    ) -> dict[str, Any]:
        """Request JSON without automatic reauthentication."""
        kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "timeout": aiohttp.ClientTimeout(total=timeout),
        }
        if json_data is not None:
            kwargs["json"] = json_data

        async with self._session.request(
            method, f"{self._base_url}{path}", **kwargs
        ) as resp:
            text = await resp.text()

        if resp.status in (401, 403):
            raise GloBirdSessionExpired(f"GloBird session expired ({resp.status})")
        if resp.status < 200 or resp.status >= 300:
            raise GloBirdApiError(f"GloBird API returned HTTP {resp.status}")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise GloBirdApiError("GloBird API returned invalid JSON") from err

        if (
            isinstance(payload, dict)
            and payload.get("success") is False
            and not allow_api_failure
        ):
            message = payload.get("message") or "GloBird API request failed"
            raise GloBirdApiError(str(message))

        return payload

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_data: Any | None = None,
        timeout: int = 30,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Request JSON, retrying once after a session expiry."""
        try:
            return await self._raw_request_json(
                method, path, json_data=json_data, timeout=timeout
            )
        except GloBirdSessionExpired:
            self._authenticated = False
            if (
                not retry_auth
                or not self._reauth_enabled
                or not self._email
                or not self._password
            ):
                raise
            _LOGGER.info("GloBird session expired; attempting re-login")
            await self.authenticate(self._email, self._password, fresh_session=False)
            return await self._raw_request_json(
                method, path, json_data=json_data, timeout=timeout
            )

    async def _establish_session(self) -> None:
        """GET the portal homepage to obtain session and sticky-routing cookies.

        The Azure ARRAffinity cookies are issued with Domain=globirdcustomerportalprod
        .azurewebsites.net (the backend hostname), but all requests go to
        myaccount.globirdenergy.com.au. aiohttp won't send cross-domain cookies, so
        we copy ARRAffinity values into the cookie jar under the primary domain to
        ensure all requests hit the same backend shard.
        """
        try:
            async with self._session.request(
                "GET",
                self._base_url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                await resp.read()

            primary = URL(self._base_url)
            sticky = {
                c.key: c.value
                for c in self._session.cookie_jar
                if "arraff" in c.key.lower()
            }
            if sticky:
                self._session.cookie_jar.update_cookies(sticky, primary)
        except Exception:  # noqa: BLE001 - best-effort; login will surface any real error
            pass

    async def _encrypt_password(self, password: str) -> str:
        """RSA-OAEP (SHA-256) encrypt password using the portal's public JWK."""
        jwk = await self._raw_request_json("GET", "/api/account/publicjwk")

        def _pad(b64: str) -> str:
            return b64 + "=" * (-len(b64) % 4)

        n_int = int.from_bytes(base64.urlsafe_b64decode(_pad(jwk["n"])), "big")
        e_int = int.from_bytes(base64.urlsafe_b64decode(_pad(jwk["e"])), "big")
        public_key = RSAPublicNumbers(e_int, n_int).public_key()

        encrypted = public_key.encrypt(
            password.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(encrypted).decode("utf-8")

    async def authenticate(
        self, email: str, password: str, *, fresh_session: bool = True
    ) -> dict[str, Any]:
        """Authenticate and validate the portal session."""
        self._email = email
        self._password = password

        if fresh_session:
            await self._establish_session()
        encrypted_password = await self._encrypt_password(password)

        payload = await self._raw_request_json(
            "POST",
            "/api/account/login",
            json_data={
                "emailAddress": email,
                "password": encrypted_password,
                "rememberMe": False,
            },
            allow_api_failure=True,
        )
        data = _payload_data(payload) or {}

        if data.get("requireRetryCaptCha") or data.get("requireHCaptcha"):
            self._authenticated = False
            raise GloBirdCaptchaRequired("GloBird requested captcha verification")

        if not payload.get("success") or data.get("isLoginSucceeded") is False:
            self._authenticated = False
            portal_msg = payload.get("message") or data.get("message") or ""
            raise GloBirdAuthError(
                f"GloBird login failed{f': {portal_msg}' if portal_msg else ''}"
            )

        self._authenticated = True
        current_user = await self._raw_request_json("GET", "/api/account/currentuser")
        return current_user

    async def restore_session(self, email: str, password: str) -> dict[str, Any] | None:
        """Validate an imported cookie/token session without sending credentials."""
        self._email = email
        self._password = password
        try:
            current_user = await self._raw_request_json(
                "GET", "/api/account/currentuser"
            )
        except GloBirdApiError:
            self._authenticated = False
            return None
        self._authenticated = True
        return current_user

    async def get_current_user(self) -> dict[str, Any]:
        """Fetch the current user payload."""
        return await self._request_json("GET", "/api/account/currentuser")

    async def get_dashboard(
        self, *, account_id: int | str | None = None
    ) -> dict[str, Any]:
        """Fetch dashboard account data."""
        path = "/api/account/dashboard"
        if account_id is not None:
            path = f"{path}?accountId={account_id}"
        return await self._request_json("GET", path)

    async def get_balance(
        self, *, account_id: int | str | None = None
    ) -> dict[str, Any]:
        """Fetch account balance data."""
        path = "/api/transaction/balance"
        if account_id is not None:
            path = f"{path}?accountId={account_id}"
        return await self._request_json("GET", path)

    async def get_signup_info(
        self, *, account_id: int | str | None = None
    ) -> dict[str, Any]:
        """Fetch signup/service information."""
        path = "/api/account/getSignupInfo"
        if account_id is not None:
            path = f"{path}?accountId={account_id}"
        return await self._request_json("GET", path)

    async def get_account_service_status(self) -> dict[str, Any]:
        """Fetch account service statuses."""
        return await self._request_json("GET", "/api/site/accountservicestatus")

    async def get_power_meter_types(self, *, nmi: str | None = None) -> dict[str, Any]:
        """Fetch power meter type lookup data."""
        path = "/api/site/GetPowerMeterTypes"
        if nmi is not None:
            path = f"{path}?nmi={nmi}"
        return await self._request_json("GET", path)

    async def get_read_meters(
        self, *, account_service_id: int | str | None = None
    ) -> dict[str, Any]:
        """Fetch meter read metadata."""
        path = "/api/site/readmeters"
        if account_service_id is not None:
            path = f"{path}?accountServiceId={account_service_id}"
        return await self._request_json("GET", path)

    async def get_usage(
        self,
        *,
        identifier: str,
        serial_number: str,
        account_service_id: int | str | None = None,
        is_smart: bool = True,
        days: int = DEFAULT_USAGE_DAYS,
    ) -> dict[str, Any]:
        """Fetch usage data for smart or basic meters."""
        from_slash, to_slash, *_ = date_range_for_usage(days)
        path = (
            "/api/site/accountservicetimezonesmartmeterread"
            if is_smart
            else "/api/site/basicmeterread"
        )
        if account_service_id is not None:
            path = f"{path}?accountServiceId={account_service_id}"
        return await self._request_json(
            "POST",
            path,
            json_data={
                "identifier": identifier,
                "serialNumber": serial_number,
                "fromDate": from_slash,
                "toDate": to_slash,
                "isSmart": is_smart,
                "isAcrossAccount": False,
            },
        )

    async def get_cost_detail(
        self,
        *,
        account_service_id: int | str,
        identifier: str,
        is_smart: bool = True,
        days: int = DEFAULT_USAGE_DAYS,
    ) -> dict[str, Any]:
        """Fetch cost detail data."""
        _, _, from_dash, to_dash, *_ = date_range_for_usage(days)
        return await self._request_json(
            "POST",
            "/api/transaction/CostDetail",
            json_data={
                "accountServiceId": account_service_id,
                "identifier": identifier,
                "from": from_dash,
                "to": to_dash,
                "isSmart": is_smart,
            },
        )

    async def get_weather_data(
        self,
        *,
        account_service_id: int | str,
        post_code: str,
        days: int = DEFAULT_USAGE_DAYS,
    ) -> dict[str, Any]:
        """Fetch weather data for a service."""
        *_, from_iso, to_iso = date_range_for_usage(days)
        return await self._request_json(
            "POST",
            "/api/weather/getWeatherData",
            json_data={
                "accountServiceId": account_service_id,
                "dateFrom": from_iso,
                "dateTo": to_iso,
                "postCode": post_code,
            },
        )

    async def get_weather_impacted_days(
        self, *, account_id: int | str | None = None
    ) -> dict[str, Any]:
        """Fetch weather impacted day count."""
        path = "/api/weather/calculateweatherimpacteddays"
        if account_id is not None:
            path = f"{path}?accountId={account_id}"
        return await self._request_json("GET", path)

    def export_session_cookies(self) -> list[dict[str, str]]:
        """Export current session cookies for persistence."""
        cookies: list[dict[str, str]] = []
        for cookie in self._session.cookie_jar:
            cookies.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": cookie["domain"] or "",
                    "path": cookie["path"] or "/",
                    "secure": str(cookie["secure"] or ""),
                    "httponly": str(cookie["httponly"] or ""),
                }
            )
        return cookies

    def import_session_cookies(self, cookies: list[dict[str, str]]) -> None:
        """Import previously persisted session cookies."""
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            morsel = SimpleCookie()
            morsel[name] = value
            morsel[name]["domain"] = cookie.get("domain", "")
            morsel[name]["path"] = cookie.get("path", "/")
            if cookie.get("secure"):
                morsel[name]["secure"] = True
            if cookie.get("httponly"):
                morsel[name]["httponly"] = True

            domain = cookie.get("domain", "").lstrip(".") or URL(self._base_url).host
            self._session.cookie_jar.update_cookies(morsel, URL(f"https://{domain}/"))

    @staticmethod
    def decode_html_json(value: str) -> Any:
        """Decode a JSON string that may be HTML escaped."""
        return json.loads(html.unescape(value))
