## What's Changed

**Gate latest-data readiness on aligned daily data**
Latest Data Status now waits until usage and complete cost data are available for the same daily date before reporting `ready`. This prevents automations from treating a newer partial portal update as safe to use when GloBird has not finished publishing all of the matching usage, cost, and ZeroHero data.

**Expose incomplete portal days consistently**
Latest Data Date, Latest Data Status, and cost sensors now expose newer incomplete portal dates through readiness attributes so it is clearer when the portal has started publishing a day but the integration is deliberately holding back the ready signal.

*Update available via HACS*
