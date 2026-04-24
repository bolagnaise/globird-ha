"""Constants for the GloBird HA integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "globird_ha"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

BASE_URL = "https://myaccount.globirdenergy.com.au"

DEFAULT_USAGE_DAYS = 31
DEFAULT_INVOICE_LIMIT = 20
DEFAULT_INVOICE_MONTHS = 12

ACCOUNT_UPDATE_INTERVAL = timedelta(minutes=30)
DETAIL_UPDATE_INTERVAL = timedelta(hours=6)

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

