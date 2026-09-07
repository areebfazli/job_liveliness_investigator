"""rli.events — `company_events` store and policy-signal derivation (spec.md §4).

See `rli.events.store` for the `CompanyEvent`/`CollectionStatus` models, CSV
I/O, and DB persistence, and `rli.events.policy_signals` for deriving
`material_negative_event` / `freeze_or_pause` (spec.md §5) from stored
events.
"""
