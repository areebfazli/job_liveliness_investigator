# Evaluation report (spec.md §6 / PLAN.md M6)

Dataset: `m4-dev-20` (`split_name=dev`, `split_kind=temporal`) · Splits read: `dev, validation, test` · `allow_test=True` · Policy version: `policy-v1:5ccbe192950455723f079c957b64965a` · Generated: `2026-09-08T00:06:19.030651Z`

## Headline gate (sample sizes)

| measure | evaluated dataset | collection corpus | spec §6 target | met? |
| --- | --- | --- | --- | --- |
| postings | 20 | 13396 | >= 300 | NO |
| companies | 20 | 78 | >= 40 | NO |
| closure events | n/a (a replay case is a (posting, T) grid point, not a closure) | 5697 | >= 100 | yes (corpus-wide) |
| replay cases scored | 118 | — | — | — |

Headline gate: **NOT MET**.

The two size columns are NOT interchangeable. The collection corpus may clear spec.md §6's targets while the replay dataset that was actually evaluated is a far smaller slice of it. Postings and companies are judged on the EVALUATED dataset, because that is the evidence behind every number below. The closure-event leg has no dataset-scoped equivalent and is judged corpus-wide, which makes it the optimistic leg of this verdict.

## Systems run

| system | status | scoped runs |
| --- | --- | --- |
| A | reused | 118 |
| B | reused | 118 |
| C | skipped: not requested (pass with_c=True / --with-c); not run: no API key | 0 |

`reused` means the dataset already carried that system's replay runs and they were scored as-is; `ran` means `rli.replay.run.run_replay` was invoked (offline by construction — the replay net client raises on any live call). No dataset was built and no network call was made by this evaluation.

## Case-set accounting

| item | value |
| --- | --- |
| cases collected | 118 |
| systems | A, B |
| allowed splits (read-side) | dev, validation, test |
| runs per system | A=118 B=118 |
| unassigned (no posting / posting absent from the split map) | 0 |
| excluded holdout (split outside the read-side filter) | 0 |
| duplicate re-runs collapsed to the latest | 0 |

A case is `(input_url, replay_at)`. An unassignable posting is EXCLUDED rather than defaulted into a split — an unknown split could, for all this report knows, be the holdout.

## Action distributions

| action | A | B |
| --- | --- | --- |
| quick_apply | 70 | 70 |
| skip | 6 | 6 |
| wait | 42 | 42 |

spec.md §6 requires agreement to be read WITH the action distribution: a default-heavy policy posts high overall agreement trivially.

System C has no action distribution: skipped: not requested (pass with_c=True / --with-c); not run: no API key.

## Agreement with System A

| system | paired cases (with A) | overall agreement | macro agreement |
| --- | --- | --- | --- |
| A | 118 | 100.0% | 100.0% |
| B | 118 | 100.0% | 100.0% |

`A` compared against itself is trivially 100% and is shown so that A's own probe, cost and latency figures have a row in every table below.

System C: not run: no API key.

### Per-class agreement

**A vs A** (classes are System A's actions)

| A action | n (A count) | A agreement |
| --- | --- | --- |
| quick_apply | 70 | 100.0% |
| skip | 6 | 100.0% |
| wait | 42 | 100.0% |

**B vs A** (classes are System A's actions)

| A action | n (A count) | B agreement |
| --- | --- | --- |
| quick_apply | 70 | 100.0% |
| skip | 6 | 100.0% |
| wait | 42 | 100.0% |

### Confusion matrices

**A action (row) -> A action (column)**

| A \ A | quick_apply | skip | wait |
| --- | --- | --- | --- |
| quick_apply | 70 | 0 | 0 |
| skip | 0 | 6 | 0 |
| wait | 0 | 0 | 42 |

**A action (row) -> B action (column)**

| A \ B | quick_apply | skip | wait |
| --- | --- | --- | --- |
| quick_apply | 70 | 0 | 0 |
| skip | 0 | 6 | 0 |
| wait | 0 | 0 | 42 |

System C has no confusion matrix: not run: no API key.

## Cost and latency (probe cost points and model dollars reported SEPARATELY)

| system | runs | probe steps | probe cost POINTS (total) | probe cost POINTS (mean/run) | model steps | model cost USD (total) | model cost USD (mean/run) | tokens in/out | latency ms (total) | latency ms (mean/run) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 118 | 466 | 814.00 | 6.90 | 0 | $0.0000 | $0.0000 | 0/0 | 105 | 1 |
| B | 118 | 408 | 650.00 | 5.51 | 0 | $0.0000 | $0.0000 | 0/0 | 92 | 1 |

**These are two different units and are NEVER summed.** `run_steps.cost_usd` holds unitless placeholder cost POINTS on `component='probe'` rows (configured in `[probe_costs]`: low=1, medium=3, high=10) and REAL DOLLARS on `component='model'` rows. `runs.total_cost_usd` adds the two together, which is why it is not quoted anywhere in this report and why no combined 'total cost' column exists. A probe-heavy system and a model-heavy system are not comparable on one axis.

Latency is SUMMED STEP LATENCY, a lower bound on wall-clock time: it excludes controller and scheduling overhead between steps.

System C: not run: no API key — no probe points, dollars or tokens.

## Failure and efficiency metrics

| system | probe steps by tier | medium/high probe steps | repeated calls | invalid arguments | recovered / runs with a failed probe | early-stop regret cases / opportunities | unnecessary probe steps | A's extra probes that changed no action | undecodable decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | low=292 medium=174 | 174 (1.47/run) | 0 | 0 | 0/0 (n/a) | 0/0 (n/a) | 225 (97.8%) | 0 | 0 |
| B | low=287 medium=121 | 121 (1.03/run) | 0 | 0 | 0/0 (n/a) | 0/48 (0.0%) | 172 (100.0%) | 58 | 0 |

`repeated calls` and `invalid arguments` are controller-forbidden events: any nonzero value is a real finding, not noise.

`unnecessary probe steps` is the LITERAL reading available in the trace — a dynamic probe execution that produced ZERO evidence rows for its own run. A probe whose evidence WAS recorded but did not move the action is not recoverable from the trace at all, so this number is a lower bound on wasted work, never an upper bound. The last column is the counterpart reading: dynamic probes the fuller reference system spent on cases where both systems ended up recommending the same action.

System C: not run: no API key.

## Data quality

Scoped to system(s) `A` over splits `dev, validation, test`. System A runs every probe, so scoping here to A measures the cached record at its fullest rather than penalising it for System B's deliberately narrower probe set.

| measure | value |
| --- | --- |
| runs checked | 118 |
| postings checked | 20 |
| ATS resolution rate | 118/118 (100.0%) |
| ATS distribution (distinct postings) | ashby=2 greenhouse=14 lever=4 |
| publish-date coverage | 1/118 (0.8%) |
| publish evidence by source quality (distinct runs) | ats_native=1 page_structured=1 |
| repost match precision | pending: data/match_precision.md not present |

### Citation support

| measure | value |
| --- | --- |
| reasons total | 83 |
| runs with reasons | 76/118 |
| every cited evidence id exists | 83 (100.0%) |
| reasons citing a missing id | 0 |
| reasons citing no id at all | 0 |
| classified / unclassified reason text | 83 / 0 |
| supported / unsupported (of classified) | 83 / 0 (100.0%) |
| claim families seen | posting_state=82 publish=1 |

An UNCLASSIFIED reason is one whose text matched no claim family: it is reported, never guessed at, and counts as neither supported nor unsupported. A reason citing no evidence id at all does not count as 'all ids exist'.

## Future leakage

| measure | value |
| --- | --- |
| violations (spec.md §6 target: 0) | 0 |
| clean | yes |
| violation kinds | (none) |

A replayed system reaches the network never: `rli.replay.mode.ReplayNetClient` raises on any attempt and the violation is written into the trace, so this audit counts recorded attempts rather than inferring them.

## Posting behaviour (survival summary)

**Corpus-wide, NOT dataset-scoped.** `rli.eval.survival.behavior_report` takes no dataset or split argument — its only scope is a single company — so these curves describe every posting the collector has ever seen, including postings outside the evaluated split. Do not read them as properties of the dataset scored above.

| measure | value |
| --- | --- |
| total intervals | 10692 |
| closed (interval-censored) | 2993 |
| right-censored (still open) | 7699 |
| archive-only observations | 2993 |

| curve | n | events | censored | median days | note |
| --- | --- | --- | --- | --- | --- |
| right-censored (Kaplan-Meier) | 10692 | 2993 | 7699 | 162.0 |  |
| interval-censored | 10692 | 2993 | 7699 | 84.8 | median CI not available: lifelines 0.30.3's Turnbull/NPMLE estimator does not compute a confidence interval for fit_interval_censoring |

Closures are interval-censored by construction (spec.md §4/§5: an exact `closed_at` is never invented), so the two curves answer slightly different questions and are shown side by side rather than merged.

_corpus-wide (all companies): rli.eval.survival.behavior_report takes no dataset or split scope, so these curves are NOT dataset-scoped_

## Agent gate

spec.md §6: System C must use <= 70% of System B's medium/high-cost probes while staying within 2% of B's agreement with System A, overall AND macro-averaged.

Verdict: **NOT RUN** (candidate runs: 0, baseline runs: 118).

> **System A structural caveat** — printed here, beside the verdict rather than in a footnote, because a probe-count comparison read without it is misleading.
>
> STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.

| leg | candidate (C) | baseline (B) | requirement | pass? |
| --- | --- | --- | --- | --- |
| medium/high probes per run | n/a | 1.03 | ratio n/a <= 0.70 | n/a |
| overall agreement with A | n/a | 100.0% | >= n/a | n/a |
| macro agreement with A | n/a | 100.0% | >= n/a | n/a |

The gate is `not_run`, not `fail`: System C produced zero scoped runs (skipped: not requested (pass with_c=True / --with-c); not run: no API key). An ungraded candidate has not failed. All candidate columns above read `not run: no API key`.

Notes:

- STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.
- the held-out 'test' split is IN SCOPE for this gate. spec.md §6 permits this once, for the final evaluation; any tuning decision made after reading it invalidates the holdout.
- system C has no runs in scope for dataset 'm4-dev-20' on splits ('dev', 'validation', 'test'), so the gate was not evaluated. On the current database this is expected for System C: there is no ANTHROPIC_API_KEY, so C was never run. `passed` is None (unknown), NOT False (failed).
- System C: skipped: not requested (pass with_c=True / --with-c); not run: no API key

## Product gate

spec.md §6: on held-out postings, following the recommended action must lower applications per screen or per interview versus the comparison group.

Verdict: **UNPROVEN**.

Reason: unproven: the `outcomes` table is empty: no application, screen or interview outcome has ever been recorded, so the product claim has no evidence of any kind; no posting with recorded outcomes falls in the held-out split(s) ('test',); applications-per-screen is undefined for the recommended group (n/a) or the comparison group (n/a): no screen outcomes to divide by; applications-per-interview is undefined for the recommended group (n/a) or the comparison group (n/a): no interview outcomes to divide by

| measure | value |
| --- | --- |
| outcomes recorded | 0 |
| postings with outcomes | 0 |
| postings matched to a run | 0 |
| held-out postings | 0 |
| outcome counts | (none) |
| minimum outcomes required | 30 |
| recommended actions | apply_now, quick_apply |
| effort per screen (recommended vs comparison) | n/a vs n/a |
| effort per interview (recommended vs comparison) | n/a vs n/a |

### Outcomes by recommended action

| action | postings | applied | screen | interview | offer | rejection | silence | applied/screen | applied/interview |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) |

`unproven` is not `fail`. A gate with no outcome data has not been failed, it has not been run; `fail` is reserved for a gate that had enough data and lost.

## C2 — learned probe ranking

spec.md §6's C2 is System C with a learned probe ranking substituted for the deterministic one. The label here is a PROXY built from the traces ("did running this probe move the partial action toward the reference action?"), not a ground-truth utility; the deterministic comparison score is `-probe_cost_points`, which is exactly what `rli.agent.controller.rank_candidates` falls back to once value is tied.

| measure | value |
| --- | --- |
| status | degenerate |
| rows (candidate probe decisions) | 230 |
| train / holdout rows | 172 / 58 |
| positive rate (train / holdout) | 0.0% / 0.0% |
| features | has_board_absent, has_board_present, has_publish_evidence, n_evidence, n_evidence_archive, n_evidence_ats_native, n_evidence_enrichment, n_evidence_news, n_evidence_page_structured, n_probes_before, probe_cost_points, probe_is_company_events, probe_is_repost_history, probe_is_requirements_drift, probe_is_team_signal |
| learned AUC / accuracy | n/a / n/a |
| deterministic AUC / accuracy | n/a / n/a |
| gain (AUC / accuracy) | n/a / n/a |
| keep the learned ranker? | NO |

Note: train or holdout labels are a single class; AUC is undefined for this split

`degenerate` means there were enough rows to try, but one side of the temporal split carried a single label class, so AUC is undefined and no comparison against the deterministic ranking is possible. Nothing is kept. This is a property of the proxy label on this dataset, not evidence that a learned ranking cannot help.

## Limitations

- Archive-era cases are weak by construction. For any replay case whose T predates this project's own daily snapshots, the only observation available is a Wayback capture, so `board_snapshot` evidence is sparse, `source_quality='archive'`, and often absent entirely. Those cases are scored, but a decision made on an archive-only corpus is a decision made on much less evidence than a present-day one, and the agreement figures average the two together.
- `team_signal` is unlicensed and disabled (`[team_signal].enabled = false`; spec.md §4 records that there is no licensed enrichment source). It is the only probe that populates the team-shrink input, so the action policy's P4 branch is UNREACHABLE in every number in this report. No system is penalised or credited for it, and the high-cost tier is effectively empty.
- Company-event coverage is partial: 0/78 companies in this database carry any `company_events` row at all (the M6 collection reached 15/78 companies at its fullest). `company_events` is a medium-cost probe and a policy input, so for the uncovered majority the material-negative-event and hiring-freeze inputs are the UNKNOWN sentinel rather than a negative finding (spec.md §4: missing history never means flat hiring).
- Sample sizes vs. spec.md §6 targets — evaluated dataset: 20 postings (target >=300), 20 companies (target >=40), 118 replay cases; collection corpus: 13396 postings, 78 companies, 5697 closure events (target >=100). The corpus may clear the targets while the evaluated replay dataset is a far smaller slice of it; the headline gate is judged on what was ACTUALLY evaluated, and it is NOT MET. The closure-event leg has no dataset-scoped equivalent (a replay dataset's unit is a (posting, T) grid point, not a closure) and is therefore corpus-wide.
- Probe cost POINTS and model DOLLARS are different units and are never summed. `run_steps.cost_usd` holds placeholder cost points on `component='probe'` rows and real USD on `component='model'` rows; `runs.total_cost_usd` adds them, which is why no single 'total cost' figure appears anywhere in this report.
- STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.
- Latency is SUMMED STEP LATENCY (`runs.total_latency_ms`), a lower bound on wall-clock time: it excludes controller and scheduling overhead between steps.
- System C was not run: no API key — there is no ANTHROPIC_API_KEY in this environment and no `llm`/`llm_factory` was supplied, so no live model call was attempted. The spec.md §6 agent gate is therefore `not_run`, not `fail`: an ungraded candidate has not failed. Every System C figure elsewhere in this report reads `not run: no API key`.
- The read-side split filter PERMITTED the `test` holdout (`allow_test=True`), but no scoped case fell in it, so no holdout data was actually read and no `holdout_test_evaluated` marker was written. The evaluated dataset was drawn from a non-holdout split; the holdout remains untouched.
- The posting-behaviour (survival) section is CORPUS-WIDE, not dataset-scoped: `rli.eval.survival.behavior_report` has no dataset or split argument, so its curves describe every posting the collector has ever seen, including postings outside the evaluated split.
- Repost match precision is pending: data/match_precision.md not present — spec.md §4 requires it to be validated on a hand-checked sample of 50 matches, and until that file exists the repost-derived policy inputs carry an unquantified error rate.
