# GloBird HA

Read-only Home Assistant custom integration for the GloBird Energy customer portal.

This integration logs in to `https://myaccount.globirdenergy.com.au` and exposes account, balance, invoice, meter, usage, cost, referral, and weather data as Home Assistant sensors.

## Install

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

