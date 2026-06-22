# GloBird HA

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/bolagnaise/globird-ha/actions/workflows/validate.yaml/badge.svg)](https://github.com/bolagnaise/globird-ha/actions/workflows/validate.yaml)

Read-only Home Assistant custom integration for the GloBird Energy customer portal.

This integration logs in to `https://myaccount.globirdenergy.com.au` and exposes account, balance, invoice, meter, usage, cost, and weather data as Home Assistant sensors.

## Install

### HACS

1. Open HACS in Home Assistant.
2. Go to **Custom repositories**.
3. Add `https://github.com/bolagnaise/globird-ha` as an **Integration** repository.
4. Install **GloBird HA** from HACS.
5. Restart Home Assistant.
6. Add the integration from **Settings > Devices & services > Add integration > GloBird HA**.

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=bolagnaise&repository=globird-ha&category=integration)

If **GloBird HA** does not appear in the Add integration search after installing through HACS:

1. Confirm HACS installed version `0.1.3` or newer.
2. Restart Home Assistant, not just HACS.
3. Search **GloBird HA** from **Settings > Devices & services > Add integration**.
4. Check that `/config/custom_components/globird_ha/manifest.json` exists.
5. Check `home-assistant.log` for `globird_ha` or `config_flow` import errors.

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
- Signup services
- Last successful refresh
- Refresh status
- One account summary sensor per returned account

Service-level sensors include:

- Service status
- Meter info
- Latest data date
- Latest data status
- Recent usage total
- Latest day usage
- Recent solar export total
- Latest day solar export
- Recent cost total
- Latest daily cost
- ZeroHero status
- Expected monthly cost
- Billing period days
- Billing period cost
- Weather summary

Recorder-safe daily summaries, the latest interval array, compact usage register totals, cost category totals, and incomplete cost days are exposed as sensor attributes. Daily usage and cost attributes keep the most recent rows and include count/truncation flags; full cached snapshots are available through Home Assistant diagnostics with sensitive fields redacted.

## Updates and data freshness

Home Assistant polls the GloBird portal every 30 minutes. You can also force a check with Home Assistant's standard **Update entity** action on any GloBird entity. Refresh Status only reports whether the latest portal fetch completed; it does not mean GloBird has finished publishing all derived daily usage, cost, and ZeroHero values.

GloBird usage and cost data normally trails by at least one day, and the portal can publish a fixed supply-charge row before the rest of that day's usage/export rows are ready. To avoid showing that early partial value as the latest daily cost, the integration only advances Latest Daily Cost to the newest cost date that has more than the fixed `SUPPLY` row. Latest Data Date only advances when usage and complete cost data are aligned for the same day. Latest Data Status reports `ready`, `waiting_for_cost`, `waiting_for_usage`, or `no_data` for automations that need to wait until a daily notification can safely use the latest date, cost, and ZeroHero sensors. If a newer incomplete date is visible from the portal, it is exposed in attributes on Latest Data Date, Latest Data Status, and cost sensors as `latest_available_day`, `latest_available_day_complete`, and `incomplete_days`.

ZeroHero status is derived from the latest complete cost day. It reports `achieved` when the cost detail for that day contains a non-zero `ZEROHERO Credit` row, `missed` when the day is complete but no credit was present, and `unknown` before a complete cost day is available.

Pricing/rate-plan sensors are not currently exposed. The portal exposes product metadata, but not enough rate detail has been validated to provide EMHASS-ready import/export price sensors safely.

## Notes

- This is read-only. It does not pay bills, submit meter reads, edit account details, or download PDFs.
- Captcha-required logins are reported as unsupported because they require browser interaction.
