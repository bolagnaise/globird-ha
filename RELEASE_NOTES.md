## v0.1.41

- Fix the integration options flow on current Home Assistant versions so the daily polling start time option can be opened and saved instead of failing with a config-flow server error.
- Keep the daily readiness semantics unchanged: Latest Data Status remains separate from Refresh Status, and the option only controls when next-day readiness polling resumes.
