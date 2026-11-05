# Legacy housing budget floor

Older saved housing searches can contain an explicit price range in `raw_query`
while the persisted budget has only its upper bound. Repository reads first check
the edit history for an explicit removal of `budget_min`. Only when no such edit
exists does the passport mapper recover the lower bound, and only when the parsed
upper bound, currency, and period agree with the stored budget. It does not
rewrite the database row. A saved lower bound remains authoritative. Follow-up
history or a changed upper bound prevents recovery because the original range
may have been superseded. Direct mapper calls do not recover a floor without the
repository's event check.

This repair applies to repeat, edit, catalog search, and monitor paths that load
the current passport. It does not change previously delivered cards. Live
acceptance still needs a read-only check of the affected saved passport and a
new, user-authorized search after deployment.
