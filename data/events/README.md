# `data/events/` — pre-collected `company_events` fixtures

This directory holds pre-collected, dated company-event data used by the
`company_events` probe (spec.md §4) and the `material_negative_event` /
`freeze_or_pause` policy signals (spec.md §5). See `rli/events/store.py` and
`rli/events/policy_signals.py` for the code that reads/writes these files,
and `scripts/collect_events.py` for the collection-loop plumbing (it does
**not** implement live search itself — see below).

## Historical-search caveat (spec.md §4)

> For historical `company_events`, pre-collect dated events and replay by
> `available_at`; do not live-search during benchmark replay.

Concretely: benchmark replay at a historical time `T` only ever reads rows
already sitting in `company_events` (via `available_at <= T`); it never
issues a live web search. That means today's searchable news index — which
is what any real collection pass (an agent running web searches) actually
queries — is used as a **stand-in** for "what a searcher could have found
as of `T`". Today's index is **not a perfect reconstruction** of historical
search results: articles get deindexed, paywalled, merged, or re-dated;
some historical events may be underrepresented or missing entirely; and a
company with zero rows here may simply be a company nobody has searched
for, not a company with a clean record. Treat coverage here as a best-effort
approximation of historical search visibility, not a guarantee, and prefer
extending live collection over trusting sparse historical backfill for any
given company (the same caution spec.md §6 asks for with sparse Wayback
archive coverage).

## Materiality rule (`classify_materiality`, `rli/events/store.py`)

* `hiring_freeze`, `hiring_pause`, `shutdown` are always `"material"` — an
  explicit freeze/pause or a shutdown is unambiguously material.
* `layoff` events are classified by parsing `headline + " " + raw_excerpt`
  for a percentage (material if any found percentage is `>= 10`) or a
  headcount followed by a word like "employees/jobs/positions/staff/
  workers/roles" (material if any found count is `>= 100`). If neither
  pattern is found, the layoff defaults to `"material"` — a **deliberate
  conservative judgment call**, not a confident classification: understating
  a layoff's materiality is the worse failure mode for the
  `material_negative_event` policy signal.
* `funding`, `expansion`, `acquisition`, `other` are always `"minor"` —
  these are not negative events; materiality on them is unused by the
  policy signal, which only looks at `layoff`/`shutdown` event types.

## `available_at` convention

`available_at` is **the article's publish date/time**, not the time the
collection script/agent ran. spec.md §3 warns against backdating *current
discoveries* to when the underlying event happened — but a dated news
article's publish timestamp is an externally verifiable fact printed on the
source, not something invented after the fact, so using it as `available_at`
is required (not merely permitted) for correct point-in-time replay: it is
when a real historical searcher could plausibly have found that article.

`collected_at` is a separate field recording when the collection run itself
wrote the row — useful for auditing collection, never used as a replay
visibility bound.

## CSV schemas

### `company_events.csv`

```text
company_id,event_type,event_date,available_at,source_url,headline,raw_excerpt,collected_at
```

* `company_id` — normalized website domain (spec.md §3 Identity).
* `event_type` — one of `layoff, hiring_freeze, hiring_pause, funding,
  expansion, acquisition, shutdown, other`.
* `event_date` — `YYYY-MM-DD`, the date the underlying event happened.
* `available_at` — ISO 8601 UTC `Z` timestamp, the article's publish
  date/time.
* `source_url`, `headline`, `raw_excerpt` — as reported by the source;
  `raw_excerpt` may be empty.
* `collected_at` — ISO 8601 UTC `Z` timestamp, when this row was collected.

`materiality` and `source_quality` are **not** columns in this CSV — they
are computed (`classify_materiality`) / constant (`"news"`) on load, via
`rli.events.store.load_events_csv`.

### `collection_status.csv`

```text
company_id,searched_at,queries_run,events_found
```

* `company_id` — normalized website domain.
* `searched_at` — ISO 8601 UTC `Z` timestamp of the collection run.
* `queries_run` — number of search queries executed for this company.
* `events_found` — number of events found for this company in that run.

## Presence semantics

* Company **present** in `collection_status.csv` with **zero** rows in
  `company_events.csv` -> the company **was searched and nothing was
  found**: `material_negative_event` and `freeze_or_pause` resolve to
  `False` ("checked, none found").
* Company **absent** from `collection_status.csv` entirely -> **not yet
  searched**: both signals resolve to `Unknown` ("not yet investigated",
  per spec.md §4's "unresolved question").

See `rli/events/policy_signals.py::derive_policy_signals` for the exact
logic, including the `negative_event_window_days` lookback window
(`config.toml` `[thresholds]`) and the "a search recorded after `as_of`
does not count" replay-safety rule.
