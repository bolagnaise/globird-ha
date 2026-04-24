"""GloBird customer portal API client and data helpers."""
from __future__ import annotations

import base64
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

from .const import BASE_URL, DEFAULT_INVOICE_LIMIT, DEFAULT_USAGE_DAYS, SENSITIVE_KEYS

_LOGGER = logging.getLogger(__name__)


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


def extract_accounts_and_services(
    current_user_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract accounts and electricity services from currentuser payload."""
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
            service_type = str(service.get("serviceType") or "").lower()
            if service_type and not any(
                marker in service_type for marker in ("power", "electric")
            ):
                continue

            enriched = dict(service)
            enriched["accountId"] = account_id
            enriched["accountNumber"] = account.get("accountNumber")
            enriched["accountAddress"] = account.get("accountAddress")
            services.append(enriched)

    if not services:
        for account in data.get("accounts", []) or []:
            for service in account.get("services", []) or []:
                enriched = dict(service)
                enriched["accountId"] = account.get("accountId")
                enriched["accountNumber"] = account.get("accountNumber")
                enriched["accountAddress"] = account.get("accountAddress")
                services.append(enriched)

    return accounts, services


def service_id(service: dict[str, Any]) -> str:
    """Return a stable service identifier."""
    value = service.get("accountServiceId") or service.get("siteIdentifier")
    return str(value or "unknown")


def select_meter_for_service(
    service: dict[str, Any],
    meters_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Select the best available meter row for a service."""
    meters = _payload_data(meters_payload)
    if not isinstance(meters, list) or not meters:
        return None

    active_meters = [
        meter
        for meter in meters
        if str(meter.get("serialStatus") or "").lower() in ("", "active", "current")
    ]
    if active_meters:
        return active_meters[0]
    return meters[0]


def build_usage_summary(
    usage_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build recorder-safe usage summary and latest interval attributes."""
    rows = _payload_data(usage_payload)
    if not isinstance(rows, list):
        rows = []

    daily: list[dict[str, Any]] = []
    total = 0.0
    latest_row: dict[str, Any] | None = None

    for row in rows:
        usage = _as_float(row.get("usage")) or 0.0
        total += usage
        summary = {
            "readDate": row.get("readDate"),
            "usage": _round(usage),
            "chargeType": row.get("chargeType"),
            "chargeCategoryCode": row.get("chargeCategoryCode"),
            "meterStatus": row.get("meterStatus"),
            "minQualityMethod": row.get("minQualityMethod"),
        }
        daily.append(summary)
        if latest_row is None or _date_key(row, "readDate") >= _date_key(
            latest_row, "readDate"
        ):
            latest_row = row

    latest_intervals = []
    if latest_row:
        values = latest_row.get("usageArray")
        if isinstance(values, list):
            latest_intervals = [_round(_as_float(value), 5) for value in values]

    return {
        "days": len(daily),
        "total_usage": _round(total),
        "latest_day": latest_row.get("readDate") if latest_row else None,
        "latest_day_usage": _round(_as_float(latest_row.get("usage"))) if latest_row else None,
        "daily": daily,
        "latest_intervals": latest_intervals,
    }


def build_cost_summary(cost_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build recorder-safe cost summary."""
    rows = _payload_data(cost_payload)
    if not isinstance(rows, list):
        rows = []

    daily: list[dict[str, Any]] = []
    total_amount = 0.0
    total_quantity = 0.0
    latest_row: dict[str, Any] | None = None

    for row in rows:
        amount = _as_float(row.get("amount")) or 0.0
        quantity = _as_float(row.get("quantity")) or 0.0
        total_amount += amount
        total_quantity += quantity
        daily.append(
            {
                "date": row.get("date"),
                "amount": _round(amount, 2),
                "quantity": _round(quantity),
                "chargeCategory": row.get("chargeCategory"),
                "chargeType": row.get("chargeType"),
            }
        )
        if latest_row is None or _date_key(row, "date") >= _date_key(latest_row, "date"):
            latest_row = row

    return {
        "days": len(daily),
        "total_amount": _round(total_amount, 2),
        "total_quantity": _round(total_quantity),
        "latest_day": latest_row.get("date") if latest_row else None,
        "latest_day_amount": _round(_as_float(latest_row.get("amount")), 2)
        if latest_row
        else None,
        "daily": daily,
    }


def build_invoice_summary(invoice_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build a compact invoice summary."""
    data = _payload_data(invoice_payload) or {}
    invoices = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(invoices, list):
        invoices = []

    return {
        "totalCount": data.get("totalCount") if isinstance(data, dict) else len(invoices),
        "invoices": [
            {
                "invoiceNumber": invoice.get("invoiceNumber"),
                "issuedDate": invoice.get("issuedDate"),
                "dueDate": invoice.get("dueDate"),
                "amount": invoice.get("amount"),
                "discountedAmont": invoice.get("discountedAmont"),
                "documentId": invoice.get("documentId"),
            }
            for invoice in invoices[:DEFAULT_INVOICE_LIMIT]
        ],
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
        self._access_token: str | None = None
        self._authenticated = False

    @property
    def is_authenticated(self) -> bool:
        """Return whether this client believes it has an active session."""
        return self._authenticated

    async def close(self) -> None:
        """Close the owned HTTP session."""
        if self._owns_session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        """Build portal-like request headers."""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "User-Agent": "GloBird-HA/0.1",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

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
            if not retry_auth or not self._email or not self._password:
                raise
            _LOGGER.info("GloBird session expired; attempting re-login")
            await self.authenticate(self._email, self._password)
            return await self._raw_request_json(
                method, path, json_data=json_data, timeout=timeout
            )

    async def _establish_session(self) -> None:
        """GET the portal homepage to obtain the session cookie required before login."""
        try:
            async with self._session.request(
                "GET",
                self._base_url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                await resp.read()
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

    async def authenticate(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate and validate the portal session."""
        self._email = email
        self._password = password

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
            raise GloBirdAuthError("Invalid GloBird email or password")

        access_token = data.get("accessToken")
        if isinstance(access_token, str) and access_token:
            self._access_token = access_token

        self._authenticated = True
        current_user = await self._raw_request_json("GET", "/api/account/currentuser")
        return current_user

    async def restore_session(self, email: str, password: str) -> dict[str, Any] | None:
        """Validate an imported cookie/token session without sending credentials."""
        self._email = email
        self._password = password
        try:
            current_user = await self._raw_request_json("GET", "/api/account/currentuser")
        except GloBirdApiError:
            self._authenticated = False
            return None
        self._authenticated = True
        return current_user

    async def get_current_user(self) -> dict[str, Any]:
        """Fetch the current user payload."""
        return await self._request_json("GET", "/api/account/currentuser")

    async def get_dashboard(self) -> dict[str, Any]:
        """Fetch dashboard account data."""
        return await self._request_json("GET", "/api/account/dashboard")

    async def get_balance(self) -> dict[str, Any]:
        """Fetch account balance data."""
        return await self._request_json("GET", "/api/transaction/balance")

    async def get_signup_info(self) -> dict[str, Any]:
        """Fetch signup/service information."""
        return await self._request_json("GET", "/api/account/getSignupInfo")

    async def get_account_service_status(self) -> dict[str, Any]:
        """Fetch account service statuses."""
        return await self._request_json("GET", "/api/site/accountservicestatus")

    async def get_power_meter_types(self) -> dict[str, Any]:
        """Fetch power meter type lookup data."""
        return await self._request_json("GET", "/api/site/GetPowerMeterTypes")

    async def get_read_meters(self) -> dict[str, Any]:
        """Fetch meter read metadata."""
        return await self._request_json("GET", "/api/site/readmeters")

    async def get_usage(
        self,
        *,
        identifier: str,
        serial_number: str,
        is_smart: bool = True,
        days: int = DEFAULT_USAGE_DAYS,
    ) -> dict[str, Any]:
        """Fetch smart meter usage data."""
        from_slash, to_slash, *_ = date_range_for_usage(days)
        return await self._request_json(
            "POST",
            "/api/site/accountservicetimezonesmartmeterread",
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

    async def get_weather_impacted_days(self) -> dict[str, Any]:
        """Fetch weather impacted day count."""
        return await self._request_json(
            "GET", "/api/weather/calculateweatherimpacteddays"
        )

    async def get_invoices(self, *, limit: int = DEFAULT_INVOICE_LIMIT) -> dict[str, Any]:
        """Fetch recent invoices."""
        return await self._request_json(
            "POST",
            "/api/transaction/invoice",
            json_data={
                "startDate": None,
                "endDate": None,
                "offset": 0,
                "limit": limit,
            },
        )

    async def get_referral_links(self) -> dict[str, Any]:
        """Fetch referral link list."""
        return await self._request_json("GET", "/api/referral/getReferralLinks")

    async def lookup_referral_link(self) -> dict[str, Any]:
        """Fetch the default referral lookup payload."""
        return await self._request_json("POST", "/api/referral/lookupReferralLink")

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
            self._session.cookie_jar.update_cookies(
                morsel, URL(f"https://{domain}/")
            )

    @staticmethod
    def decode_html_json(value: str) -> Any:
        """Decode a JSON string that may be HTML escaped."""
        return json.loads(html.unescape(value))
