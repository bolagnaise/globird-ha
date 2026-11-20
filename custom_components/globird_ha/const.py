"""Constants for the GloBird HA integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "globird_ha"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DAILY_POLL_START_TIME = "daily_poll_start_time"
DEFAULT_DAILY_POLL_START_TIME = "00:05"
CONF_RATE_OVERRIDES = "rate_overrides"
CONF_SUPER_EXPORT_CAP_KWH = "super_export_cap_kwh"
CONF_ZEROHERO_KWH_PER_HOUR = "zerohero_kwh_per_hour"
CONF_GRID_IMPORT_ENTITY = "grid_import_entity"
CONF_GRID_EXPORT_ENTITY = "grid_export_entity"

BASE_URL = "https://myaccount.globirdenergy.com.au"

DEFAULT_USAGE_DAYS = 31
DEFAULT_GAS_READING_DAYS = 365
DEFAULT_INVOICE_LIMIT = 20
DEFAULT_INVOICE_MONTHS = 12

ACCOUNT_UPDATE_INTERVAL = timedelta(minutes=30)

STORAGE_VERSION = 1

SENSITIVE_KEYS = {
    "accessToken",
    "accountAddress",
    "accountName",
    "accountNumber",
    "address",
    "concessionAddress",
    "documentId",
    "email",
    "emailAddress",
    "identifier",
    "invoiceNumber",
    "nmi",
    "password",
    "serial",
    "serialNumber",
    "siteAddress",
    "siteIdentifier",
    "streetAddress",
}
