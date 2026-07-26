## v0.1.39

- Add gas service discovery, latest gas reading sensors, and recorder long-term statistics import for historical gas reads.
- Keep existing electricity entity names stable while adding gas-specific service suffixes where needed to distinguish services.
- Fetch and validate meter metadata per service so mixed electricity/gas accounts do not reuse the wrong meter details.
- Keep gas-only accounts and mixed gas services out of electricity daily-data readiness decisions, so polling slows appropriately once the relevant data is ready.
- Handle corrected gas reads and meter replacements without dropping later consumption history.
