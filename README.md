# GloBird HA

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/bolagnaise/globird-ha/actions/workflows/validate.yaml/badge.svg)](https://github.com/bolagnaise/globird-ha/actions/workflows/validate.yaml)

Read-only Home Assistant custom integration for the GloBird Energy customer portal.

This integration logs in to `https://myaccount.globirdenergy.com.au` and exposes account, balance, invoice, meter, usage, cost, referral, and weather data as Home Assistant sensors.

## Install

### HACS

1. Open HACS in Home Assistant.
2. Go to **Custom repositories**.
3. Add `https://github.com/bolagnaise/globird-ha` as an **Integration** repository.
4. Install **GloBird HA** from HACS.
5. Restart Home Assistant.
6. Add the integration from **Settings > Devices & services > Add integration > GloBird HA**.

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=bolagnaise&repository=globird-ha&category=integration)

### Manual

1. Copy `custom_components/globird_ha` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from **Settings > Devices & services > Add integration > GloBird HA**.
4. Enter your GloBird portal email address and password.

## Entities

The integration creates one config entry and discovers all electricity accounts/services returned by the portal.

Account-level sensors include:

- Account balance
- Dashboard balance and recent transactions
- Latest invoice
- Invoice count
- Referral links
- Signup services
- One account summary sensor per returned account

Service-level sensors include:

- Service status
- Meter info
- Recent usage total
- Latest day usage
- Recent cost total
- Latest daily cost
- Weather summary

Detailed daily summaries and the latest interval array are exposed as sensor attributes. Full cached snapshots are available through Home Assistant diagnostics with sensitive fields redacted.

## Notes

- This is read-only. It does not pay bills, submit meter reads, edit account details, or download PDFs.
- Captcha-required logins are reported as unsupported because they require browser interaction.
- The GloBird portal HAR used to identify endpoints is intentionally not included in this repository.
