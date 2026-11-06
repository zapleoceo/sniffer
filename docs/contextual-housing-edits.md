# Contextual housing edits

The query parser recognizes written Russian room counts such as «две комнаты» as
total rooms. In «дом, минимум две комнаты» the room count describes the house;
it does not turn the request into a room rental. Bedrooms remain a separate
criterion. A full restatement keeps the current search root when the subject and
city are unchanged.

For an existing search, the structured intake receives the current non-secret
criteria as context. Its response includes grounded quotes for total rooms and
an explicit lower-budget removal action. An omitted lower bound is a **keep**,
not a removal. The removal action requires a quote from the user's message;
common direct removal phrasing is also recognized without a model call. The
direct parser accepts both word orders and refuses negated removal requests.
An explicit request to leave the minimum unchanged is a no-op: it does not
create a search version or root. The model result is also rejected when the
message negates removal. The
result is saved as a `manual_edit` event with `removed: ["budget_min"]`, so the
legacy floor recovery does not resurrect the earlier minimum from `raw_query`.
Compound edits are applied in one revision: removing the floor and changing the
upper bound or the named city zone does not discard the other clause. A request
to raise the minimum is treated as a lower-bound edit, not as a new maximum.

Removing a room minimum works for every housing category and for either of
its keys. The field registry owns `rooms` for a house and a flat but not for a
room rental, and owns neither `rooms_min` nor `bedrooms` anywhere, while the
parser stores them from the words of the request and the card prints them. The
edit therefore asks the registry about each key and strips what the registry
does not own. All of the keys are removed together: answering about one of them
would report «уже не задан» for a condition that is still discarding results.

A refusal from the domain is answered in words on the same search root. Nothing
catches an exception raised here — `bot/app.py` starts polling without an error
router — so a traceback means silence: the update is lost and the client is not
told even that the edit failed. The search itself is left untouched.

`tests/test_contextual_housing_edits.py` covers full restatement, partial upper
bound edit, explicit lower bound removal, reload, restoration, one search root,
and a mocked structured model response. It uses synthetic data and does not
demonstrate understanding by a live model or acceptance of a production search.
Normal refinements keep `raw_query` as an audit trail; restoring search criteria
does not erase that trail. An explicit card edit can replace the raw phrase.
