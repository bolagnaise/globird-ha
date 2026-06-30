## What's Changed

**Use the active smart meter after meter upgrades**
Meter selection now treats portal statuses such as `Energized` as active and avoids falling back to removed meters when GloBird returns both an old BASIC meter and a new SMART meter. This lets upgraded sites fetch usage and cost data from the current smart meter instead of continuing to use the old removed meter.

**Keep stable ordering for equivalent meters**
When returned meters have the same status and type, GloBird HA still keeps the portal's first returned row so existing equivalent-meter behaviour stays stable.

*Update available via HACS*
