## What's Changed

**Align billing-period projections with GloBird**
Expected Monthly Cost now projects from the current billing period rather than the calendar month, using completed daily net cost totals and the invoice issue date as the billing-period start. Billing Period Cost now uses the same daily net totals so the projection and cost-so-far values are based on the same inputs.

**Improve solar export classification**
Usage rows are now classified as solar/export when the portal marks them with solar, export, feed-in, or export direction metadata, not only when the register suffix starts with `B`. This should better match GloBird app totals for sites where export rows are returned under newer or unexpected register names.

*Update available via HACS*
