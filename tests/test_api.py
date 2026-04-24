"""Tests for the GloBird API helpers."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
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
extract_accounts_and_services = api.extract_accounts_and_services
redact_sensitive = api.redact_sensitive


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

    result = asyncio.run(client.authenticate("user@example.test", "secret"))

    assert client.is_authenticated is True
    assert result["data"]["emailAddress"] == "user@example.test"
    assert session.requests[0][0] == "POST"
    assert session.requests[0][1].endswith("/api/account/login")
    assert session.requests[0][2]["json"] == {
        "emailAddress": "user@example.test",
        "password": "secret",
    }
    assert session.requests[1][1].endswith("/api/account/currentuser")


def test_authenticate_captcha_required() -> None:
    """Captcha flags produce a dedicated auth error."""
    fixtures = load_fixtures()
    session = FakeSession([(200, fixtures["login_captcha"])])
    client = GloBirdClient(session=session, base_url="https://example.test")

    try:
        asyncio.run(client.authenticate("user@example.test", "secret"))
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

    try:
        asyncio.run(client.authenticate("user@example.test", "wrong"))
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

    async def scenario() -> dict[str, Any]:
        await client.authenticate("user@example.test", "secret")
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
        test_redact_sensitive_diagnostics,
    ):
        suite.addTest(unittest.FunctionTestCase(test_func))
    return suite
