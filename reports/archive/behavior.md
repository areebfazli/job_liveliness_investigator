# Posting-behavior report

Scope: `(all companies)` · Generated: `2026-09-08T00:25:13.851447Z` · As of: `2026-09-08T00:24:43.665115Z`

## Sample accounting

| metric | value |
| --- | --- |
| total intervals (postings/jobs observed) | 14879 |
| closed (interval-censored) | 7183 |
| still open (right-censored) | 7696 |
| archive-only (never seen by our own captures) | 7178 |
| own-observed | 7701 |
| dropped: non-finite bounds | 0 |
| dropped: negative/inverted bounds | 0 |

## Right-censored curve (KaplanMeierFitter.fit)

| metric | value |
| --- | --- |
| n_observations | 14879 |
| n_events | 7183 |
| n_censored | 7696 |
| median_days | 150.7 |
| median 95% CI | 149.3 - 156.6 |
| survival_at_7d | 99.7% |
| survival_at_14d | 98.9% |
| survival_at_30d | 94.9% |
| survival_at_60d | 82.8% |
| survival_at_90d | 71.8% |

## Interval-censored curve (KaplanMeierFitter.fit_interval_censoring — Turnbull)

| metric | value |
| --- | --- |
| n_observations | 14879 |
| n_events | 7183 |
| n_censored | 7696 |
| median_days | 84.1 |
| median 95% CI | n/a - n/a |
| survival_at_7d | 97.2% |
| survival_at_14d | 92.0% |
| survival_at_30d | 80.7% |
| survival_at_60d | 63.0% |
| survival_at_90d | 47.1% |
| note | median CI not available: lifelines 0.30.3's Turnbull/NPMLE estimator does not compute a confidence interval for fit_interval_censoring |

## Coverage (rli.history.features.coverage_window)

| metric | value |
| --- | --- |
| companies in scope | 78 |
| history_days (min/median/max) | 0.3 / 312.6 / 360.9 |
| mean history_coverage | 88.7% |
| mean calendar_coverage | 29.1% |
| companies below min_history_days (30d) | 22 |

## Reposting

| metric | value |
| --- | --- |
| closed postings in scope | 7183 |
| reposted (linked in repost_links) | 349 |
| repost rate | 4.9% |
| repost_links rows in scope | 349 |
| median days: first_seen_absent -> replacement first_observed | 0.0 (n=349) |

## Limitations and interpretation

- **Closures are interval-censored; no exact `closed_at` is ever invented.** spec.md §4: archive-derived closures keep only `last_seen_open` and `first_seen_absent` — the true closure time is bracketed, never pinpointed, and both curves above are fit from those brackets (or, for the right-censored arm, the bracket's upper bound) rather than from any fabricated point estimate.
- **The right-censored arm's median is biased UPWARD.** Its event time for a closed posting is the interval's upper bound (`closure_absent_at`), i.e. "known closed by this point" — the least-informative end of the bracket. Prefer the interval-censored (Turnbull) curve for any real claim about typical posting lifetime; the right-censored arm exists because spec.md §5 separately describes own-snapshot closures as right-censored on their own.
- **`history_coverage`'s denominator is our own attempt days**, not calendar days — see `rli.history.features.coverage_window`. A company with sparse attempts can show high `history_coverage` while having very little actual calendar span observed; `calendar_coverage` in the table above is the stricter reading.
- **Do not use posting survival probability as the v1 action engine** (spec.md §5, verbatim). This report is an evaluation artifact only; nothing in `rli.policy` reads `SurvivalCurve` or `BehaviorReport`.
- **Sparse archive coverage makes these curves indicative, not authoritative.** With few closure events (see the sample-accounting table above), both curves can be dominated by a handful of observations; treat medians and survival probabilities as a rough sense of shape, not a calibrated estimate, until spec.md §6's headline evaluation targets (≥100 observed closure events) are met.
