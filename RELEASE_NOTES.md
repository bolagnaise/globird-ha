## What's Changed

**Make ZeroHero Status date-aware**
ZeroHero Status now reports `achieved` or `missed` only when the latest complete cost data is for the current Home Assistant local day. It reports `pending` before the daily ZeroHero window finishes and `awaiting_result` after 21:00 until GloBird publishes complete cost data for today. The sensor now uses the enum device class and exposes `result_is_for_today`, `last_result`, and `last_result_day` attributes.

**Use Home Assistant local time for billing days**
Billing Period Days now uses Home Assistant's configured timezone instead of the host process date, avoiding an off-by-one day on UTC-based HAOS or Docker installs during the local morning.

**Replace the Expected Monthly Cost icon**
Expected Monthly Cost now uses the existing `mdi:cash-clock` icon instead of the unavailable `mdi:cash-calendar` icon.

*Update available via HACS*
