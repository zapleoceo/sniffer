# Legacy housing budget floor

Older saved housing searches can contain an explicit price range in `raw_query`
while the persisted budget has only its upper bound. On read, the passport mapper
recovers the lower bound only when the parsed upper bound, currency, and period
agree with the stored budget. It does not rewrite the database row. A saved lower
bound remains authoritative. Follow-up history or a changed upper bound prevents
recovery because the original range may have been superseded.

This repair applies to repeat, edit, catalog search, and monitor paths that load
the current passport. It does not change previously delivered cards. Live
acceptance still needs a read-only check of the affected saved passport and a
new, user-authorized search after deployment.
