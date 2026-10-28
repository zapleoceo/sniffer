# Quota reservation sweep

The worker releases stale, unconfirmed search reservations in bounded batches.
`QuotaRepository.sweep_stale` first locks at most `limit` eligible rows with
`FOR UPDATE SKIP LOCKED`. The selected IDs are materialized once before the
`DELETE`, so PostgreSQL cannot reevaluate the limiting subquery for each row
and delete more reservations than the requested batch size. Ties in
`shown_at` are ordered by ID.

Confirmed reservations and monitor views remain untouched. The database
contract test exercises consecutive batches against PostgreSQL, including
the exact count and the remaining rows.
