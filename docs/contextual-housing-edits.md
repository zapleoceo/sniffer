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
The model result is also rejected when the message negates removal. The
result is saved as a `manual_edit` event with `removed: ["budget_min"]`, so the
legacy floor recovery does not resurrect the earlier minimum from `raw_query`.

`tests/test_contextual_housing_edits.py` covers full restatement, partial upper
bound edit, explicit lower bound removal, reload, restoration, one search root,
and a mocked structured model response. It uses synthetic data and does not
demonstrate understanding by a live model or acceptance of a production search.
