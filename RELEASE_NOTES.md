## v0.1.40

- Add an integration option for the daily polling start time, so accounts that normally publish usage, cost, and ZeroHero data later can delay the next day's automatic readiness polling instead of starting just after midnight.
- Keep the existing readiness model unchanged: Latest Data Status still determines when daily data is complete, Refresh Status still reports fetch success, and partial latest-day cost rows remain incomplete until full daily data is available.
