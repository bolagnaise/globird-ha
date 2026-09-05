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
- Weather impacted days
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
- Calculated TOU cost (only when a rate schedule is configured)

Gas service-level sensors include:

- Service status
- Meter info
- Latest gas reading
- Latest gas reading date
- Calculated gas cost (only when a rate schedule is configured)

Recorder-safe daily summaries, the latest interval array, a recent window of per-day half-hourly interval breakdowns, compact usage register totals (including any time-of-use split such as Peak/Offpeak), cost category totals, a cost breakdown by time-of-use charge type when the portal provides one, daily net cost totals, and incomplete cost days are exposed as sensor attributes. Daily usage and cost attributes keep the most recent rows and include count/truncation flags; full cached snapshots are available through Home Assistant diagnostics with sensitive fields redacted. Meter Info exposes a human-readable meter type description (e.g. "Smart") alongside the raw meter row.

For gas services, historical basic-meter readings are also imported into Home Assistant recorder long-term statistics so historical charts can be populated from existing portal read history. Meter replacements and corrected lower reads are handled without losing subsequent consumption.

For electricity import usage, smart-meter intervals across the cached usage window (up to 31 days) are imported into Home Assistant recorder long-term statistics under the Recent Usage Total sensor, aggregated to hourly resolution. Home Assistant's recorder rejects external statistics whose timestamp is not exactly on the hour, so even though GloBird reports finer-grained intervals (commonly every 5 or 30 minutes depending on the meter), each hour's intervals are summed before import; the finer-grained data is still available uncombined via the `intervals_by_day` attribute and feeds the Calculated TOU Cost sensor's per-interval accuracy. This lets the built-in Energy Dashboard show real hourly consumption shape, not just daily totals, and backfills as far back as GloBird has published interval data. Like all GloBird data, this trails the portal by roughly a day. Solar export intervals are not currently imported into statistics, only into daily totals.

The portal attaches the same full-day interval array to every time-of-use row (e.g. Peak and Offpeak) for a meter, with only the billed portion differing per row; the integration counts each day's interval array once per meter register rather than once per time-of-use row, so time-of-use accounts don't get their interval data double-counted.

Net daily cost is also imported into recorder long-term statistics under the Recent Cost Total sensor (daily resolution only, since GloBird does not publish half-hourly cost detail). Pair it with the Recent Usage Total statistic as the cost stat for grid consumption in the Energy Dashboard's settings to see $ alongside kWh.

Both statistics imports are additive only: every sync re-uploads the currently cached ~31-day window, which upserts (adds or corrects) that window and backfills any day GloBird has newly published, but never deletes or touches statistics outside that window. History older than 31 days that was written by a previous sync is left untouched in Home Assistant's recorder and is only ever removed by your own recorder purge configuration or a manual statistics fix, never by this integration.

## Updates and data freshness

Home Assistant polls the GloBird portal every 30 minutes until all discovered electricity services have `ready` latest data for yesterday or newer. Gas services do not block that readiness check because basic-meter reads are not published daily. Once the electricity data is ready, or after a successful refresh for a gas-only account, polling slows until the configured daily polling start time on the next day to avoid unnecessary portal requests and repeated recorder updates for data that will not change again that day. The default start time is 00:05 local Home Assistant time, and it can be changed from the integration options if your GloBird account normally publishes data later. You can still force a check at any time with Home Assistant's standard **Update entity** action on any GloBird entity. Refresh Status only reports whether the latest portal fetch completed; it does not mean GloBird has finished publishing all derived daily usage, cost, and ZeroHero values.

GloBird usage and cost data normally trails by at least one day, and the portal can publish a fixed supply-charge row before the rest of that day's usage/export rows are ready. To avoid showing that early partial value as the latest daily cost, the integration only advances Latest Daily Cost to the newest cost date that has more than the fixed `SUPPLY` row. Latest Data Date only advances when usage and complete cost data are aligned for the same day. Latest Data Status reports `ready`, `waiting_for_cost`, `waiting_for_usage`, or `no_data` for automations that need to wait until a daily notification can safely use the latest date, cost, and ZeroHero sensors. If a newer incomplete date is visible from the portal, it is exposed in attributes on Latest Data Date, Latest Data Status, and cost sensors as `latest_available_day`, `latest_available_day_complete`, and `incomplete_days`.

ZeroHero status reports the latest complete portal result as `achieved` or `missed`, and exposes date-aware attributes so automations can tell whether that result is for the current Home Assistant local day. It reports `unknown` before any usable complete cost day is available.

Expected Monthly Cost projects the current billing period from completed daily net cost totals, using the latest invoice issue date as the billing-period start and a 30-day period. Billing Period Cost uses the same daily net totals so it matches the projection inputs. Billing Period Days uses Home Assistant's local date rather than the host process timezone.

Pricing/rate-plan sensors are not populated from the portal. GloBird's API exposes only product metadata (plan name, start/end date, a couple of flags) through `getProductsByAccountId` and `getAllProductHistoriesByAccountId` — verified directly, neither returns $/kWh rate figures — so there isn't enough rate detail available to derive prices automatically or safely provide EMHASS-ready import/export price sensors.

Instead, you can enter your own time-of-use rate schedule (from your contract/bill) as JSON in the integration options to enable the **Calculated TOU Cost** sensor per electricity service. It stays unavailable until a schedule is configured. Example:

```json
{
  "supply_charge": 1.12,
  "periods": [
    {"name": "Offpeak Usage", "rate": 0.28, "windows": [["00:00", "15:00"], ["21:00", "24:00"]]},
    {"name": "Peak Usage", "rate": 0.45, "windows": [["15:00", "21:00"]]}
  ]
}
```

Each period's `windows` are `[start, end)` 24-hour clock pairs (`"24:00"` means midnight at the end of the day); `name` is just a label (matching your bill's chargeType names, e.g. "Peak Usage", makes the breakdown easier to read but isn't required for the calculation to work). The sensor state is the latest calculated day's total cost; attributes include the per-period kWh/cost breakdown, a recent daily history, and `unassigned_kwh` for any usage that fell outside all configured windows (a sign the schedule doesn't fully cover the day and should be adjusted). The calculation uses the same real per-interval usage that feeds the half-hourly statistics import, so it reflects actual consumption shape, not just a daily total split evenly across periods.

Gas billing is shaped completely differently — a daily supply charge plus a seasonal, inclining-block `$/MJ` rate applied to *average* daily usage across each meter-read period (gas basic meters aren't read daily), and GloBird reports gas reads in the meter's native unit (typically m³), so a heating-value conversion factor to MJ is needed too. Enter this separately as gas rate schedule JSON in options to enable the **Calculated Gas Cost** sensor per gas service. Example, matching a typical GLOSAVE-style gas rate card:

```json
{
  "daily_charge": 0.58685,
  "conversion_mj_per_unit": 38.6,
  "seasons": [
    {"name": "Summer", "months": [10, 11, 12, 1, 2, 3],
     "tiers": [{"limit_mj_per_day": 20.70, "rate": 0.03735}, {"limit_mj_per_day": null, "rate": 0.02934}]},
    {"name": "Winter", "months": [4, 5, 6, 7, 8, 9],
     "tiers": [{"limit_mj_per_day": 20.70, "rate": 0.03735}, {"limit_mj_per_day": null, "rate": 0.02934}]}
  ]
}
```

`conversion_mj_per_unit` is the heating value for your network (check your gas bill — it's usually printed there, and varies by distributor, roughly 37.7–39.3 MJ/m³). Tiers apply in order to average daily MJ usage for that read period; a `limit_mj_per_day` of `null` means "the remainder" and should only appear on the last tier. The sensor state is the most recently completed meter-read period's total cost (daily charge + tiered usage cost); attributes include a recent history of billed periods.

## Notes

- This is read-only. It does not pay bills, submit meter reads, edit account details, or download PDFs.
- Captcha-required logins are reported as unsupported because they require browser interaction.
