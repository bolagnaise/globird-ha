"""Data update coordinator for GloBird HA."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    GloBirdClient,
    build_cost_summary,
    build_usage_interval_daily_series,
    build_usage_summary,
    build_weather_summary,
    extract_accounts_and_services,
    merge_usage_payloads,
    meters_for_service,
    select_meter_for_service,
    service_id,
    usage_requires_history_fallback,
)
from .const import (
    ACCOUNT_UPDATE_INTERVAL,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class GloBirdCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for fetching GloBird portal data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=ACCOUNT_UPDATE_INTERVAL,
        )

        self.entry = entry
        self.email = entry.data[CONF_EMAIL]
        self.password = entry.data[CONF_PASSWORD]
        self.client = GloBirdClient()

        self._cache_store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.cache.{entry.entry_id}"
        )
        self._cookie_store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.cookies.{entry.entry_id}"
        )
        self._cache: dict[str, Any] | None = None
        self._interval_backfill_cursor: dict[str, str] = {}
        self._initialized = False

    async def async_shutdown(self) -> None:
        """Close resources."""
        await self.client.close()

    async def _async_initialize(self) -> None:
        """Load cached data and any persisted cookies."""
        if self._initialized:
            return

        loaded_cache = await self._cache_store.async_load()
        self._cache = loaded_cache if isinstance(loaded_cache, dict) else None
        cursor = self._cache.get("interval_backfill_cursor") if isinstance(self._cache, dict) else None
        self._interval_backfill_cursor = cursor if isinstance(cursor, dict) else {}

        cookie_state = await self._cookie_store.async_load()
        cookies = cookie_state.get("cookies", []) if isinstance(cookie_state, dict) else []
        if isinstance(cookies, list) and cookies:
            self.client.import_session_cookies(cookies)
            restored = await self.client.restore_session(self.email, self.password)
            if restored is not None:
                _LOGGER.info("GloBird session restored from persisted cookies")

        self._initialized = True

    async def _fetch_optional(
        self,
        key: str,
        callback: Callable[[], Awaitable[dict[str, Any]]],
        cache: dict[str, Any],
        *,
        _errors: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch optional data, falling back to cache on endpoint failure."""
        try:
            return await callback()
        except Exception as err:  # noqa: BLE001 - optional portal endpoint.
            _LOGGER.warning("GloBird optional fetch failed for %s: %s", key, err)
            if _errors is not None:
                _errors[key] = str(err)
            cached_value = cache.get(key)
            return cached_value if isinstance(cached_value, dict) else None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch GloBird data."""
        await self._async_initialize()
        cache = self._cache or {}

        try:
            if self.client.is_authenticated:
                # Session cookies are still valid — _request_json will automatically
                # re-authenticate (fresh_session=False) if a 403 is returned.
                current_user = await self.client.get_current_user()
            else:
                current_user = await self.client.authenticate(self.email, self.password)

            accounts, services = extract_accounts_and_services(current_user)

            # Extract primary identifiers for account-scoped endpoints
            primary_account_id = (
                services[0].get("accountId") if services
                else (accounts[0].get("accountId") if accounts else None)
            )
            primary_nmi = services[0].get("siteIdentifier") if services else None
            primary_account_service_id = services[0].get("accountServiceId") if services else None

            fetch_errors: dict[str, str] = {}
            self.client.disable_reauth()
            try:
                data: dict[str, Any] = {
                    "current_user": current_user,
                    "accounts": accounts,
                    "services": services,
                    "last_update": time.time(),
                }

                data["dashboard"] = await self._fetch_optional(
                    "dashboard",
                    lambda: self.client.get_dashboard(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["balance"] = await self._fetch_optional(
                    "balance",
                    lambda: self.client.get_balance(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["signup_info"] = await self._fetch_optional(
                    "signup_info",
                    lambda: self.client.get_signup_info(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["service_status"] = await self._fetch_optional(
                    "service_status", self.client.get_account_service_status, cache, _errors=fetch_errors
                )
                data["meter_types"] = await self._fetch_optional(
                    "meter_types",
                    lambda: self.client.get_power_meter_types(nmi=primary_nmi),
                    cache,
                    _errors=fetch_errors,
                )
                data["read_meters"] = await self._fetch_optional(
                    "read_meters",
                    lambda: self.client.get_read_meters(account_service_id=primary_account_service_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["weather_impacted_days"] = await self._fetch_optional(
                    "weather_impacted_days",
                    lambda: self.client.get_weather_impacted_days(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["_fetch_errors"] = fetch_errors
            finally:
                self.client.enable_reauth()

            cached_service_data = cache.get("service_data", {})
            cached_service_data = (
                cached_service_data if isinstance(cached_service_data, dict) else {}
            )
            service_data = {}
            for service in services:
                sid = service_id(service)
                cached_detail = cached_service_data.get(sid)
                service_data[sid] = await self._fetch_service_detail(
                    service,
                    data.get("read_meters"),
                    data.get("service_status"),
                    cached_detail if isinstance(cached_detail, dict) else {},
                    self._interval_backfill_cursor,
                )

            data["service_data"] = service_data
            data["interval_backfill_cursor"] = self._interval_backfill_cursor

            self._cache = data
            await self._cache_store.async_save(data)
            await self._cookie_store.async_save({
                "cookies": self.client.export_session_cookies(),
            })
            return data

        except Exception as err:  # noqa: BLE001 - coordinator should preserve cache.
            if cache:
                stale = dict(cache)
                stale["refresh_error"] = str(err)
                stale["last_failed_update"] = time.time()
                return stale
            raise UpdateFailed(f"Unable to fetch GloBird data: {err}") from err

    @staticmethod
    def _parse_portal_date(value: Any) -> datetime | None:
        """Return midnight datetime for supported portal date formats."""
        if not value:
            return None
        raw = str(value).split("T")[0]
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def _build_interval_statistics_points(
        self,
        series: list[dict[str, Any]],
        *,
        since_utc: datetime | None,
    ) -> tuple[list[dict[str, Any]], datetime | None]:
        """Build external-statistics points from daily interval arrays."""
        tz_name = self.hass.config.time_zone or "UTC"
        try:
            local_tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - fallback to UTC when timezone is invalid.
            local_tz = timezone.utc

        points: list[dict[str, Any]] = []
        newest_point: datetime | None = None

        for day_row in series:
            day = self._parse_portal_date(day_row.get("readDate"))
            intervals = day_row.get("intervals")
            if day is None or not isinstance(intervals, list) or not intervals:
                continue

            interval_step = timedelta(seconds=(24 * 60 * 60) / len(intervals))
            day_start = datetime.combine(day.date(), datetime.min.time(), tzinfo=local_tz)
            for idx, interval_value in enumerate(intervals):
                if not isinstance(interval_value, (int, float)):
                    continue
                start_utc = (day_start + interval_step * idx).astimezone(timezone.utc)
                if since_utc is not None and start_utc <= since_utc:
                    continue

                value = float(interval_value)
                points.append(
                    {
                        "start": start_utc,
                        "mean": value,
                        "min": value,
                        "max": value,
                    }
                )
                if newest_point is None or start_utc > newest_point:
                    newest_point = start_utc

        return points, newest_point

    @staticmethod
    def _parse_cursor_timestamp(value: str | None) -> datetime | None:
        """Parse a persisted UTC timestamp cursor."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def _publish_usage_interval_backfill(
        self,
        service: dict[str, Any],
        usage_payload: dict[str, Any] | None,
        interval_backfill_cursor: dict[str, str],
    ) -> dict[str, Any]:
        """Publish historical interval usage into Home Assistant external statistics."""
        if not isinstance(usage_payload, dict):
            return {
                "import_points": 0,
                "export_points": 0,
                "updated": False,
            }

        try:
            from homeassistant.components.recorder.statistics import (  # pylint: disable=import-outside-toplevel
                async_add_external_statistics,
            )
        except Exception:  # noqa: BLE001 - recorder/statistics may be unavailable.
            return {
                "import_points": 0,
                "export_points": 0,
                "updated": False,
                "reason": "recorder_unavailable",
            }

        sid = service_id(service)
        totals = {
            "import_points": 0,
            "export_points": 0,
            "updated": False,
        }

        for direction in ("import", "export"):
            cursor_key = f"{sid}:{direction}"
            since_utc = self._parse_cursor_timestamp(interval_backfill_cursor.get(cursor_key))
            series = build_usage_interval_daily_series(usage_payload, direction=direction)
            points, newest = self._build_interval_statistics_points(series, since_utc=since_utc)
            if not points:
                continue

            statistic_id = f"{DOMAIN}:{sid}:{direction}_interval_kwh"
            metadata = {
                "has_mean": True,
                "has_sum": False,
                "name": f"GloBird {sid} {direction.title()} Interval Usage",
                "source": DOMAIN,
                "statistic_id": statistic_id,
                "unit_of_measurement": "kWh",
            }

            try:
                await async_add_external_statistics(self.hass, metadata, points)
            except Exception as err:  # noqa: BLE001 - statistics import should not fail refresh.
                _LOGGER.debug("Unable to publish interval backfill for %s: %s", statistic_id, err)
                continue

            totals[f"{direction}_points"] = len(points)
            totals["updated"] = True
            if newest is not None:
                interval_backfill_cursor[cursor_key] = newest.isoformat()

        return totals

    async def _fetch_service_detail(
        self,
        service: dict[str, Any],
        meters_payload: dict[str, Any] | None,
        status_payload: dict[str, Any] | None,
        cache: dict[str, Any],
        interval_backfill_cursor: dict[str, str],
    ) -> dict[str, Any]:
        """Fetch heavier per-service detail."""
        sid = service_id(service)
        status_map = (
            status_payload.get("data", {})
            if isinstance(status_payload, dict)
            else {}
        )
        service_status = status_map.get(sid) if isinstance(status_map, dict) else None

        all_meters = meters_for_service(service, meters_payload)
        meter = select_meter_for_service(service, meters_payload)
        identifier = service.get("siteIdentifier")
        serial_number = meter.get("serialNumber") if meter else None
        meter_read_type = str(meter.get("meterReadType") or "" if meter else "")
        is_smart = meter_read_type.lower() != "basic"
        account_service_id = service.get("accountServiceId")
        usage_meter_serials: list[str] = [str(serial_number)] if serial_number else []

        usage = None
        if identifier and serial_number:
            usage = await self._fetch_optional(
                "usage",
                lambda: self.client.get_usage(
                    identifier=str(identifier),
                    serial_number=str(serial_number),
                    account_service_id=account_service_id,
                    is_smart=is_smart,
                    days=DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

            if usage_requires_history_fallback(usage, days=DEFAULT_USAGE_DAYS):
                selected_serial = str(serial_number)
                removed_statuses = {
                    "removed",
                    "inactive",
                    "retired",
                    "deenergized",
                }

                removed_meters = [
                    candidate
                    for candidate in all_meters
                    if str(candidate.get("serialNumber") or "") != selected_serial
                    and str(candidate.get("serialStatus") or "").strip().lower() in removed_statuses
                ]

                for fallback_meter in removed_meters:
                    fallback_serial = str(fallback_meter.get("serialNumber") or "")
                    if not fallback_serial:
                        continue

                    fallback_is_smart = (
                        str(fallback_meter.get("meterReadType") or "").strip().lower() != "basic"
                    )
                    fallback_usage = await self._fetch_optional(
                        f"usage_fallback_{fallback_serial}",
                        lambda: self.client.get_usage(
                            identifier=str(identifier),
                            serial_number=fallback_serial,
                            account_service_id=account_service_id,
                            is_smart=fallback_is_smart,
                            days=DEFAULT_USAGE_DAYS,
                        ),
                        cache,
                    )
                    usage = merge_usage_payloads(usage, fallback_usage)
                    usage_meter_serials.append(fallback_serial)

                    if not usage_requires_history_fallback(
                        usage,
                        days=DEFAULT_USAGE_DAYS,
                    ):
                        break

        usage_backfill = await self._publish_usage_interval_backfill(
            service,
            usage,
            interval_backfill_cursor,
        )

        cost = None
        if identifier and account_service_id:
            cost = await self._fetch_optional(
                "cost",
                lambda: self.client.get_cost_detail(
                    account_service_id=account_service_id,
                    identifier=str(identifier),
                    is_smart=is_smart,
                    days=DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

        weather = None
        post_code = service.get("postCode")
        if post_code and account_service_id:
            weather = await self._fetch_optional(
                "weather",
                lambda: self.client.get_weather_data(
                    account_service_id=account_service_id,
                    post_code=str(post_code),
                    days=DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

        return {
            "service": service,
            "status": service_status,
            "meter": meter,
            "usage_meter_serials": usage_meter_serials,
            "usage_fallback_applied": len(usage_meter_serials) > 1,
            "usage_backfill": usage_backfill,
            "usage": usage,
            "usage_summary": build_usage_summary(usage),
            "cost": cost,
            "cost_summary": build_cost_summary(cost),
            "weather": weather,
            "weather_summary": build_weather_summary(weather),
        }
