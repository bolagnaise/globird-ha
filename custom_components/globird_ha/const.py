"""Constants for the GloBird HA integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "globird_ha"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

BASE_URL = "https://myaccount.globirdenergy.com.au"

DEFAULT_USAGE_DAYS = 31
DEFAULT_GAS_READING_DAYS = 365
DEFAULT_INVOICE_LIMIT = 20
DEFAULT_INVOICE_MONTHS = 12

ACCOUNT_UPDATE_INTERVAL = timedelta(minutes=30)
ACCOUNT_READY_UPDATE_OFFSET = timedelta(minutes=5)

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
