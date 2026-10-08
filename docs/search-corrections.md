# Search selection and explicit stops

The General chat uses `users.active_passport_root`. A Telegram topic uses its
own `search_tabs.passport_root`. Selecting or repeating a search in a topic must
read the selected, user-owned root and must not move the General pointer.

`QueryIntake.parse()` applies `default_city` when opening a new search. When
parsing text against a current search (`for_edit=True`), an omitted city stays
unset until the refinement or edit is merged with the current passport. An
explicit city remains an explicit change. The Chotot source still applies its
configured city filter to actual source requests.

Pause and Delete-search cancel pending outbox rows for the affected search,
including scheduled digests and retries. The notifier locks the subscription
before claiming its outbox rows. Enqueue takes the same subscription lock so
a match cannot insert a notification after the stop has cleared the queue.
A concurrent explicit stop either finishes first and prevents dispatch, or waits for an already claimed send. Repeated
stops do not change sent rows. The six-hour grace for natural subscription
expiry remains separate from an explicit stop.
