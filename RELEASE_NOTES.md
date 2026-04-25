## What's Changed

**Balance sensors now show correct sign for credit accounts**
The Balance and Dashboard Balance sensors were reporting credit accounts as positive values (e.g. A$138.89 in credit), which was backwards. They now follow the standard convention: negative = you're in credit, positive = you owe money. This matches how the Latest Invoice and Recent Cost Total sensors already behaved.

**Two new billing period sensors**
Added `Billing Period Days` and `Billing Period Cost` per service. `Billing Period Days` shows how many days have elapsed since your latest invoice date — matching the "Number of Days" shown in the GloBird app. `Billing Period Cost` shows your net spend for the current billing period only (filtered from the existing 31-day cost detail, no extra API calls). Negative means you're tracking in credit for the period; positive means a net charge.

**Solar export sensors**
Added `Recent Solar Export Total` and `Latest Day Solar Export` sensors for customers with solar feed-in. These read the B1 register (solar export) separately from the E1 import register, giving you accurate feed-in totals and the previous day's export figure.

**Fix: daily usage and export totals were under-reporting**
Customers on time-of-use tariffs have multiple usage rows per day (one per pricing period). Previously only one row's value was shown. The integration now correctly sums all TOU periods together to give accurate daily totals.

**Fix: Latest Daily Cost was showing supply charge only**
GloBird returns three rows per day in the cost detail (USAGE, SOLAR credit, SUPPLY charge). The sensor was incorrectly reporting only the last row (always the SUPPLY charge — a positive number), ignoring the solar credit offset. It now sums all three to give the true net daily cost.

*Update available via HACS*
