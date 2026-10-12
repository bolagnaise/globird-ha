## v0.1.43

- Prevent `latest_intervals` from double-counting repeated full-day usage arrays on time-of-use plans. Arrays are now counted once per meter register and summed only across distinct registers, keeping interval totals aligned with `latest_day_usage` while preserving multi-register controlled-load usage.
