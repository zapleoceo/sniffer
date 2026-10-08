# Stars refund recovery

Refund confirmations are stored in `billing_events` by charge ID. A refund update or an
outgoing refund found in Stars history writes that confirmation before a late payment can
be inserted. The payment insert checks the confirmation in PostgreSQL and starts the row
as `refunded`, so a refund delivered before the payment cannot restore access.
The repository serializes refund notices and payment inserts for the same charge with a
PostgreSQL transaction lock, covering concurrent delivery as well as reversed delivery.

After Telegram confirms a refund, slot synchronization and subscription cancellation
have separate completion events. Reconciliation selects `refunded` payments missing either
completion regardless of payment age. It retries transient failures without refunding the
same charge again. Cancellation completion is keyed by invoice payload because all renewal
charges belong to one subscription. Owner alert deduplication is written only after a
successful send.

The recovery path uses the existing `billing_events` table and needs no migration. Tests
cover reversed delivery in the PostgreSQL repository and retries with fake Bot API and
slot ports. No live payment, refund, or message is needed to verify it.
