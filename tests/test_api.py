"""Tests for the GloBird API helpers."""
from __future__ import annotations

import asyncio
import calendar
import importlib
import json
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "globird_responses.json"
COMPONENT_PATH = Path(__file__).parents[1] / "custom_components"
INTEGRATION_PATH = COMPONENT_PATH / "globird_ha"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENT_PATH)]  # type: ignore[attr-defined]
globird_package = types.ModuleType("custom_components.globird_ha")
globird_package.__path__ = [str(INTEGRATION_PATH)]  # type: ignore[attr-defined]
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.globird_ha", globird_package)

api = importlib.import_module("custom_components.globird_ha.api")

GloBirdCaptchaRequired = api.GloBirdCaptchaRequired
GloBirdAuthError = api.GloBirdAuthError
GloBirdClient = api.GloBirdClient
build_cost_summary = api.build_cost_summary
build_usage_summary = api.build_usage_summary
build_weather_summary = api.build_weather_summary
cost_attributes = api.cost_attributes
extract_accounts_and_services = api.extract_accounts_and_services
merge_usage_payloads = api.merge_usage_payloads
redact_sensitive = api.redact_sensitive
select_meter_for_service = api.select_meter_for_service
usage_attributes = api.usage_attributes
usage_requires_history_fallback = api.usage_requires_history_fallback


def load_fixtures() -> dict[str, Any]:
    """Load sanitized fixture payloads."""
    return json.loads(FIXTURE_PATH.read_text())


class FakeResponse:
    """Minimal aiohttp response context manager."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def text(self) -> str:
        return json.dumps(self._payload)


class FakeSession:
    """Minimal aiohttp session for deterministic request sequences."""

    closed = False
    cookie_jar: list[Any] = []

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        status, payload = self._responses.pop(0)
        return FakeResponse(status, payload)


def stub_password_encryption(client: Any) -> None:
    """Keep auth tests focused on request flow, not RSA encryption."""

    async def fake_encrypt_password(password: str) -> str:
        return password

    client._encrypt_password = fake_encrypt_password


def test_authenticate_success() -> None:
    """Login posts credentials and validates with currentuser."""
    fixtures = load_fixtures()
    session = FakeSession(
        [
            (200, fixtures["login_success"]),
            (200, fixtures["current_user"]),
        ]
    )
    client = GloBirdClient(session=session, base_url="https://example.test")
    stub_password_encryption(client)

    result = asyncio.run(
        client.authenticate("user@example.test", "secret", fresh_session=False)
    )

    assert client.is_authenticated is True
    assert result["data"]["emailAddress"] == "user@example.test"
    assert session.requests[0][0] == "POST"
    assert session.requests[0][1].endswith("/api/account/login")
    assert session.requests[0][2]["json"] == {
        "emailAddress": "user@example.test",
        "password": "secret",
        "rememberMe": False,
    }
    assert session.requests[1][1].endswith("/api/account/currentuser")


def test_authenticate_captcha_required() -> None:
    """Captcha flags produce a dedicated auth error."""
    fixtures = load_fixtures()
    session = FakeSession([(200, fixtures["login_captcha"])])
    client = GloBirdClient(session=session, base_url="https://example.test")
    stub_password_encryption(client)

    try:
        asyncio.run(
            client.authenticate("user@example.test", "secret", fresh_session=False)
        )
    except GloBirdCaptchaRequired:
        pass
    else:
        raise AssertionError("Expected captcha-required authentication failure")

    assert client.is_authenticated is False


def test_authenticate_invalid_credentials() -> None:
    """Failed login payloads produce a dedicated auth error."""
    fixtures = load_fixtures()
    session = FakeSession([(200, fixtures["login_failure"])])
    client = GloBirdClient(session=session, base_url="https://example.test")
    stub_password_encryption(client)

    try:
        asyncio.run(
            client.authenticate("user@example.test", "wrong", fresh_session=False)
        )
    except GloBirdAuthError:
        pass
    else:
        raise AssertionError("Expected invalid-auth failure")

    assert client.is_authenticated is False


def test_session_expiry_reauthenticates_once() -> None:
    """A 401 response triggers exactly one credential re-login and retry."""
    fixtures = load_fixtures()
    session = FakeSession(
        [
            (200, fixtures["login_success"]),
            (200, fixtures["current_user"]),
            (401, {"success": True}),
            (200, fixtures["login_success"]),
            (200, fixtures["current_user"]),
            (200, fixtures["balance"]),
        ]
    )
    client = GloBirdClient(session=session, base_url="https://example.test")
    stub_password_encryption(client)

    async def scenario() -> dict[str, Any]:
        await client.authenticate("user@example.test", "secret", fresh_session=False)
        return await client.get_balance()

    result = asyncio.run(scenario())

    assert result["data"]["balance"] == 123.45
    requested_paths = [
        request[1].replace("https://example.test", "")
        for request in session.requests
    ]
    assert requested_paths == [
        "/api/account/login",
        "/api/account/currentuser",
        "/api/transaction/balance",
        "/api/account/login",
        "/api/account/currentuser",
        "/api/transaction/balance",
    ]


def test_extract_accounts_services_and_summaries() -> None:
    """Parser helpers produce compact, recorder-safe summaries."""
    fixtures = load_fixtures()

    accounts, services = extract_accounts_and_services(fixtures["current_user"])
    usage = build_usage_summary(fixtures["usage"])
    cost = build_cost_summary(fixtures["cost"])
    weather = build_weather_summary(fixtures["weather"])

    assert len(accounts) == 2
    assert len(services) == 2
    assert usage["total_usage"] == 3.5
    assert usage["latest_day"] == "2026-04-02"
    assert usage["latest_intervals"] == [0.4, 0.5, 0.6]
    # Fixture: 2 days × (SOLAR + USAGE + SUPPLY). Net = (1.48) + (-0.43) = 1.05
    assert cost["total_amount"] == 1.05
    assert cost["total_quantity"] == 21.5
    # latest_day_amount is the net sum for 2026/04/02: -2.36 + 0.60 + 1.33 = -0.43
    assert cost["latest_day_amount"] == -0.43
    assert cost["latest_available_day"] == "2026/04/02"
    assert cost["latest_available_day_complete"] is True
    assert weather["latest_max_temp"] == 29


def test_cost_summary_net_daily_is_sum_not_last_row() -> None:
    """latest_day_amount sums all rows for the day — not just the last row (SUPPLY charge)."""
    payload = {
        "data": [
            {"chargeCategory": "SOLAR",  "chargeType": None, "date": "2026/04/24", "amount": -3.12, "quantity": 21.0},
            {"chargeCategory": "USAGE",  "chargeType": None, "date": "2026/04/24", "amount":  0.21, "quantity": 47.0},
            {"chargeCategory": "SUPPLY", "chargeType": None, "date": "2026/04/24", "amount":  1.40, "quantity":  0.0},
        ],
        "message": None,
        "success": True,
    }
    cost = build_cost_summary(payload)
    # Net = -3.12 + 0.21 + 1.40 = -1.51 — NOT the supply-charge-only value of 1.40
    assert cost["total_amount"] == -1.51
    assert cost["latest_day"] == "2026/04/24"
    assert cost["latest_day_amount"] == -1.51
    assert cost["latest_available_day_complete"] is True


def test_cost_summary_ignores_supply_only_partial_latest_day() -> None:
    """latest_day ignores a newer supply-only day until usage/export rows arrive."""
    payload = {
        "data": [
            {
                "chargeCategory": "SOLAR",
                "chargeType": None,
                "date": "2026/05/15",
                "amount": -2.10,
                "quantity": 14.0,
            },
            {
                "chargeCategory": "USAGE",
                "chargeType": None,
                "date": "2026/05/15",
                "amount": 0.40,
                "quantity": 3.0,
            },
            {
                "chargeCategory": "SUPPLY",
                "chargeType": None,
                "date": "2026/05/15",
                "amount": 1.24,
                "quantity": 0.0,
            },
            {
                "chargeCategory": "SUPPLY",
                "chargeType": None,
                "date": "2026/05/16",
                "amount": 1.24,
                "quantity": 0.0,
            },
        ],
        "message": None,
        "success": True,
    }

    cost = build_cost_summary(payload)

    assert cost["latest_available_day"] == "2026/05/16"
    assert cost["latest_available_day_complete"] is False
    assert cost["latest_day"] == "2026/05/15"
    assert cost["latest_day_amount"] == -0.46
    assert cost["total_amount"] == -0.46
    assert cost["incomplete_days"] == ["2026/05/16"]
    assert cost["daily"] == [
        row for row in cost["available_daily"] if row["date"] == "2026/05/15"
    ]


def test_usage_summary_tracks_all_registers_and_b_exports() -> None:
    """Usage summaries expose all returned registers and treat B* suffixes as export."""
    payload = {
        "data": [
            {
                "readDate": "2026-04-24",
                "usage": 3.0,
                "suffix": "E1",
                "chargeType": "Peak",
                "chargeCategoryCode": "USAGE",
                "usageArray": [1.0, 2.0],
            },
            {
                "readDate": "2026-04-24",
                "usage": 1.0,
                "suffix": "E2",
                "chargeType": "Controlled Load",
                "chargeCategoryCode": "CONTROL",
                "usageArray": [0.25, 0.75],
            },
            {
                "readDate": "2026-04-24",
                "usage": 2.5,
                "suffix": "B2",
                "chargeType": "Super Export",
                "chargeCategoryCode": "SOLAR",
                "usageArray": [1.5, 1.0],
            },
        ],
        "message": None,
        "success": True,
    }

    usage = build_usage_summary(payload)

    assert usage["total_usage"] == 4.0
    assert usage["latest_day_usage"] == 4.0
    assert usage["total_export"] == 2.5
    assert usage["latest_day_export"] == 2.5
    assert [register["key"] for register in usage["registers"]] == [
        "B2-Super Export",
        "E1-Peak",
        "E2-Controlled Load",
    ]
    assert usage["registers"][0]["direction"] == "export"
    assert usage["registers"][2]["chargeCategoryCode"] == "CONTROL"


def test_select_meter_prefers_energized_smart_over_removed_basic() -> None:
    """Meter selection should switch to an energized smart meter after upgrade."""
    service = {"accountServiceId": 810964}
    payload = {
        "data": [
            {
                "meterReadType": "BASIC",
                "serialNumber": "1180281",
                "serialStatus": "Removed",
            },
            {
                "meterReadType": "SMART",
                "serialNumber": "LG12G055914",
                "serialStatus": "Energized",
            },
        ]
    }

    meter = select_meter_for_service(service, payload)

    assert meter is not None
    assert meter["serialNumber"] == "LG12G055914"
    assert meter["meterReadType"] == "SMART"


def test_select_meter_prefers_smart_when_status_unknown() -> None:
    """When statuses are empty, prefer the smart meter row."""
    service = {"accountServiceId": 810964}
    payload = {
        "data": [
            {"meterReadType": "BASIC", "serialNumber": "110563", "serialStatus": None},
            {"meterReadType": "SMART", "serialNumber": "LG12G055914", "serialStatus": None},
        ]
    }

    meter = select_meter_for_service(service, payload)

    assert meter is not None
    assert meter["meterReadType"] == "SMART"


def test_usage_requires_history_fallback_when_earliest_date_too_recent() -> None:
    """Fallback should trigger when the earliest usage date misses window start."""
    payload = {
        "data": [
            {
                "readDate": "2026/06/20",
                "usage": 1.0,
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
            }
        ],
        "success": True,
    }

    assert usage_requires_history_fallback(
        payload,
        days=31,
        today=date(2026, 6, 22),
    ) is True


def test_usage_requires_history_fallback_false_for_covered_window() -> None:
    """Fallback should not trigger when usage starts near the requested window."""
    payload = {
        "data": [
            {
                "readDate": "2026/05/22",
                "usage": 1.0,
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
            }
        ],
        "success": True,
    }

    assert usage_requires_history_fallback(
        payload,
        days=31,
        today=date(2026, 6, 22),
    ) is False


def test_usage_requires_history_fallback_when_all_dates_unparseable() -> None:
    """Fallback should trigger when rows exist but every readDate is unparseable."""
    payload = {
        "data": [
            {
                "readDate": None,
                "usage": 1.0,
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
            },
            {
                "readDate": "not-a-date",
                "usage": 2.0,
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
            },
        ],
        "success": True,
    }

    assert usage_requires_history_fallback(
        payload,
        days=31,
        today=date(2026, 6, 22),
    ) is True


def test_merge_usage_payloads_combines_and_deduplicates_rows() -> None:
    """Merging usage payloads should keep unique rows and preserve chronological order."""
    primary = {
        "data": [
            {
                "readDate": "2026/06/20",
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
                "serial": "SMART1",
                "usage": 2.0,
            }
        ],
        "success": True,
    }
    fallback = {
        "data": [
            {
                "readDate": "2026/06/18",
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
                "serial": "BASIC1",
                "usage": 1.0,
            },
            {
                "readDate": "2026/06/20",
                "suffix": "E1",
                "chargeType": "Peak Usage",
                "chargeCategoryCode": "USAGE",
                "serial": "SMART1",
                "usage": 2.0,
            },
        ],
        "success": True,
    }

    merged = merge_usage_payloads(primary, fallback)

    assert merged is not None
    assert [row["readDate"] for row in merged["data"]] == ["2026/06/18", "2026/06/20"]
    assert len(merged["data"]) == 2


def test_cost_summary_exposes_new_category_totals() -> None:
    """Cost summaries preserve newer GloBird categories separately."""
    payload = {
        "data": [
            {
                "chargeCategory": "USAGE",
                "date": "2026/04/24",
                "amount": 1.2,
                "quantity": 3.0,
            },
            {
                "chargeCategory": "SOLAR",
                "date": "2026/04/24",
                "amount": -0.5,
                "quantity": 2.0,
            },
            {
                "chargeCategory": "Super Export top up",
                "date": "2026/04/24",
                "amount": -0.8,
                "quantity": 2.0,
            },
            {
                "chargeCategory": "ZEROHERO Credit",
                "date": "2026/04/24",
                "amount": -0.3,
                "quantity": 0.0,
            },
        ],
        "message": None,
        "success": True,
    }

    cost = build_cost_summary(payload)

    assert cost["total_amount"] == -0.4
    assert cost["latest_day_amount"] == -0.4
    assert cost["latest_day_zerohero_credit"] == -0.3
    assert cost["latest_day_zerohero_achieved"] is True
    assert cost["categories"] == [
        {"chargeCategory": "SOLAR", "amount": -0.5, "quantity": 2.0},
        {"chargeCategory": "Super Export top up", "amount": -0.8, "quantity": 2.0},
        {"chargeCategory": "USAGE", "amount": 1.2, "quantity": 3.0},
        {"chargeCategory": "ZEROHERO Credit", "amount": -0.3, "quantity": 0.0},
    ]


def test_cost_summary_projects_current_month_cost() -> None:
    """Current-month projection extrapolates completed daily cost rows."""
    today = date.today()
    day_1 = today.replace(day=1)
    day_2 = day_1 + timedelta(days=1) if today.day > 1 else day_1
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    payload = {
        "data": [
            {
                "chargeCategory": "USAGE",
                "date": day_1.strftime("%Y/%m/%d"),
                "amount": 2.0,
                "quantity": 4.0,
            },
            {
                "chargeCategory": "SUPPLY",
                "date": day_1.strftime("%Y/%m/%d"),
                "amount": 1.0,
                "quantity": 0.0,
            },
            {
                "chargeCategory": "SOLAR",
                "date": day_2.strftime("%Y/%m/%d"),
                "amount": -1.0,
                "quantity": 2.0,
            },
        ],
        "message": None,
        "success": True,
    }

    cost = build_cost_summary(payload)

    assert cost["projected_month"] == {
        "month": today.strftime("%Y-%m"),
        "cost_to_date": 2.0,
        "projected_cost": round(2.0 / day_2.day * days_in_month, 2),
        "completed_days": day_2.day,
        "days_in_month": days_in_month,
        "latest_day": day_2.isoformat(),
    }


def test_sensor_attributes_are_recorder_safe_summaries() -> None:
    """Entity attributes keep recent rows only and strip nested register details."""
    usage_payload = {
        "data": [
            {
                "readDate": (date(2026, 4, 1) + timedelta(days=offset)).isoformat(),
                "usage": 1.0,
                "suffix": "E1",
                "chargeType": "Peak",
                "chargeCategoryCode": "USAGE",
                "usageArray": [0.1] * 48,
            }
            for offset in range(45)
        ],
    }
    cost_payload = {
        "data": [
            {
                "chargeCategory": category,
                "date": (date(2026, 4, 1) + timedelta(days=offset)).strftime(
                    "%Y/%m/%d"
                ),
                "amount": amount,
                "quantity": 1.0,
            }
            for offset in range(45)
            for category, amount in (
                ("USAGE", 1.1),
                ("SUPPLY", 1.2),
                ("SOLAR", -0.4),
            )
        ],
    }

    usage = usage_attributes(build_usage_summary(usage_payload), direction="import")
    cost = cost_attributes(build_cost_summary(cost_payload))

    assert usage["daily_count"] == 45
    assert len(usage["daily"]) == 7
    assert usage["daily_truncated"] is True
    assert "daily" not in usage["registers"][0]
    assert len(cost["daily"]) == 7
    assert len(cost["available_daily"]) == 7
    assert cost["daily_truncated"] is True
    assert len(json.dumps(usage)) < 16_384
    assert len(json.dumps(cost)) < 16_384


def test_redact_sensitive_diagnostics() -> None:
    """Diagnostics redaction removes credentials and account identifiers."""
    payload = {
        "emailAddress": "user@example.test",
        "password": "secret",
        "nested": {
            "accountNumber": "GB0001",
            "safe": "kept",
        },
    }

    redacted = redact_sensitive(payload)

    assert redacted["emailAddress"] == "**REDACTED**"
    assert redacted["password"] == "**REDACTED**"
    assert redacted["nested"]["accountNumber"] == "**REDACTED**"
    assert redacted["nested"]["safe"] == "kept"


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Expose pytest-style functions to the stdlib unittest runner."""
    suite = unittest.TestSuite()
    for test_func in (
        test_authenticate_success,
        test_authenticate_captcha_required,
        test_authenticate_invalid_credentials,
        test_session_expiry_reauthenticates_once,
        test_extract_accounts_services_and_summaries,
        test_cost_summary_net_daily_is_sum_not_last_row,
        test_cost_summary_ignores_supply_only_partial_latest_day,
        test_usage_summary_tracks_all_registers_and_b_exports,
        test_select_meter_prefers_energized_smart_over_removed_basic,
        test_select_meter_prefers_smart_when_status_unknown,
        test_usage_requires_history_fallback_when_earliest_date_too_recent,
        test_usage_requires_history_fallback_false_for_covered_window,
        test_usage_requires_history_fallback_when_all_dates_unparseable,
        test_merge_usage_payloads_combines_and_deduplicates_rows,
        test_cost_summary_exposes_new_category_totals,
        test_cost_summary_projects_current_month_cost,
        test_sensor_attributes_are_recorder_safe_summaries,
        test_redact_sensitive_diagnostics,
    ):
        suite.addTest(unittest.FunctionTestCase(test_func))
    return suite
