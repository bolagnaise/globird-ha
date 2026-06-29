## What's Changed

**Slow portal polling after daily data is ready**
GloBird HA now keeps the normal 30-minute polling cadence only until every discovered service has `ready` latest data for yesterday or newer. Once the daily data is ready, automatic polling pauses until just after midnight on the next day, reducing unnecessary portal requests and repeated recorder updates for values that should not change again that day.

**Keep manual refresh available**
The standard Home Assistant **Update entity** action still forces an immediate refresh whenever you want one, and polling returns to the normal 30-minute cadence whenever data is not ready or a refresh fails.

*Update available via HACS*
