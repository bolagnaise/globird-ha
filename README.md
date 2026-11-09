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

The integration creates one config entry and discovers all active services returned by the portal.

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

Gas service-level sensors include:

- Service status
- Meter info
- Latest gas reading
- Latest gas reading date

Recorder-safe daily summaries, the latest interval array, compact usage register totals, cost category totals, daily net cost totals, and incomplete cost days are exposed as sensor attributes. Daily usage and cost attributes keep the most recent rows and include count/truncation flags; full cached snapshots are available through Home Assistant diagnostics with sensitive fields redacted.

For gas services, historical basic-meter readings are also imported into Home Assistant recorder long-term statistics so historical charts can be populated from existing portal read history. Meter replacements and corrected lower reads are handled without losing subsequent consumption.

## Updates and data freshness

Home Assistant polls the GloBird portal every 30 minutes until all discovered electricity services have `ready` latest data for yesterday or newer. Gas services do not block that readiness check because basic-meter reads are not published daily. Once the electricity data is ready, or after a successful refresh for a gas-only account, polling slows until the configured daily polling start time on the next day to avoid unnecessary portal requests and repeated recorder updates for data that will not change again that day. The default start time is 00:05 local Home Assistant time, and it can be changed from the integration options if your GloBird account normally publishes data later. You can still force a check at any time with Home Assistant's standard **Update entity** action on any GloBird entity. Refresh Status only reports whether the latest portal fetch completed; it does not mean GloBird has finished publishing all derived daily usage, cost, and ZeroHero values.

GloBird usage and cost data normally trails by at least one day, and the portal can publish a fixed supply-charge row before the rest of that day's usage/export rows are ready. To avoid showing that early partial value as the latest daily cost, the integration only advances Latest Daily Cost to the newest cost date that has more than the fixed `SUPPLY` row. Latest Data Date only advances when usage and complete cost data are aligned for the same day. Latest Data Status reports `ready`, `waiting_for_cost`, `waiting_for_usage`, or `no_data` for automations that need to wait until a daily notification can safely use the latest date, cost, and ZeroHero sensors. If a newer incomplete date is visible from the portal, it is exposed in attributes on Latest Data Date, Latest Data Status, and cost sensors as `latest_available_day`, `latest_available_day_complete`, and `incomplete_days`.

ZeroHero status reports the latest complete portal result as `achieved` or `missed`, and exposes date-aware attributes so automations can tell whether that result is for the current Home Assistant local day. It reports `unknown` before any usable complete cost day is available.

Expected Monthly Cost projects the current billing period from completed daily net cost totals, using the latest invoice issue date as the billing-period start and a 30-day period. Billing Period Cost uses the same daily net totals so it matches the projection inputs. Billing Period Days uses Home Assistant's local date rather than the host process timezone.

## Rates and live pricing

GloBird's portal does not publish plan rates through the endpoints this integration reads, but it does publish what is needed to recover them. Every smart-meter usage row names its charge (`Peak Usage`, `Offpeak Usage`, `Solar/Generation Feed in (4pm-11pm)`, `Super Export top up`, `ZeroHero`...) and the local time windows it applies to, and `CostDetail` returns the billed amount per day for each cost category. Each refresh rebuilds the plan's windows from the newest usage day, so a plan change is picked up automatically. Rates are then fitted per cost category by least squares across the complete days in the 31-day window, and snapped to the nearest 0.1 c/kWh, the precision retailers publish rates at (the unrounded fit is kept as `fitted_rate`).

What can be recovered, and how well:

| Charge | How | Accuracy |
| --- | --- | --- |
| Usage rates per time-of-use period | Fit of daily `USAGE` cost against kWh in each window | Excellent where you use energy (standard error below 0.1 c/kWh); weak for a period you rarely import in, such as peak with a battery. Each rate carries a `confidence` and `standard_error_cents`. |
| Feed-in rates per window | Fit of daily `SOLAR` credit against exports in each window | Excellent |
| Super Export (capped add-on) | Credit / kWh on uncapped days; the cap from days where the credit stops growing | Exact after snapping; the 15 kWh cap is found once a day exceeds it |
| Daily supply charge | Median `SUPPLY` row | To the cent (the portal rounds each day) |
| ZeroHero | Median credit; window from the usage rows | Credit exact. The import threshold defaults to GloBird's 0.03 kWh per hour; days achieved and missed are reported with the window imports seen |
| Controlled load, free-usage allowance, critical peak events | Only if they appear in the data | Shown as `unobserved` until used |

Rates you know can be pinned in the integration options under **Rate overrides**, using the charge names and cents the GloBird app shows on **Plan Details**, for example:

```yaml
Peak Usage: 47.3
Shoulder Usage: 33
Daily Charge: 147.4
Super Export top up - Step1: 10
Controlled Load: 29.7
```

Pinned values replace inferred ones; anything that does not match a charge is listed in `unmatched_overrides`.

Electricity services get these sensors:

- **Tariff Rates**: `portal_inferred` or `partly_overridden`, with every rate, window, confidence, the supply charge, Super Export and ZeroHero details as attributes, plus `plan_names` when the portal's product endpoints return them.
- **Current Import Price** and **Current Export Price** (AUD/kWh): update at each window boundary, with a 24-hour, 30-minute `forecast` attribute (not recorded). The export price includes the Super Export top-up while its window is open and, when an export meter is configured, until the daily cap is used.

If you set a **grid import** and/or **grid export** energy sensor in the options (any cumulative kWh sensor, such as an inverter's lifetime grid counters), the integration also tracks today live:

- **Estimated Cost Today** (AUD): supply plus each window's import at its rate, minus feed-in, Super Export (capped) and the ZeroHero credit once earned, with a per-charge breakdown.
- **ZeroHero Today**: `pending`, `on_track`, `earned` or `missed`, with the window import so far and the headroom left.
- **Super Export Remaining Today** (kWh).

Each meter reading is placed in the window at the midpoint of the interval it covers. Energy used while Home Assistant was down for more than 15 minutes cannot be placed in a window, so the tracker re-seeds and counts the gap in `restart_gaps`. A single grid meter cannot separate controlled load, so it is priced as general usage.

## Notes

- This is read-only. It does not pay bills, submit meter reads, edit account details, or download PDFs.
- Captcha-required logins are reported as unsupported because they require browser interaction.
