# Repost-match precision validation (hand-checked sample of 50)

Source: `data/match_sample.csv` (top 50 candidates by `combined_score` across
all companies, per `rli.history.sample`'s documented design — see caveat in
§4 below), hand-checked row by row against `data/rli.db` (read-only):
`postings`, `board_snapshot_jobs` joined through `board_snapshots`,
`posting_snapshots`, `repost_links`, and `company_events`. Full per-row
verdicts and justifications are in `data/match_sample_checked.csv`.

## 1. Precision

`is_match == true` in the input: **42 / 50** rows.

**Precision = (rows with `human_verdict == true_repost` AND `is_match ==
true`) / (rows with `is_match == true`) = 36 / 42 = 0.857 (85.7%).**

(A looser, not-quite-right reading of the spec's formula — counting all 38
`true_repost` verdicts in the numerator regardless of `is_match`, since 2 of
them, ranks 31 and 39, are `is_match=false` rows that lost the greedy
one-to-one assignment but are still genuine reposts on their own merits —
would give 38/42 = 90.5%. The 85.7% figure is the one that matches what
"precision" normally means: of what the algorithm actually accepted as a
match, how much was correct.)

## 2. Verdict distribution

| verdict | is_match=true | is_match=false | total |
|---|---|---|---|
| true_repost | 36 | 2 | 38 |
| different_role | 3 | 4 | 7 |
| unclear | 2 | 2 | 4 |
| version | 1 | 0 | 1 |
| **total** | **42** | **8** | **50** |

The 8 `is_match=false` rows all have `reject_reason=assignment` (they
passed the score thresholds but lost the greedy one-to-one assignment to a
higher-ranked competing candidate for the same old or new posting id) — none
were rejected by the score thresholds themselves. Every single one of the 8
`assignment`-losers was still hand-checked as a real candidate pair on its
own merits (not just deferred to whichever won the assignment).

## 3. False-positive patterns actually observed

**This validation sample cannot exercise `title_min`, `title_only_min`, or
`combined_min` at all.** `rli.history.sample` deliberately exports the *top
50 candidates by `combined_score` across every company* (see the module's
own docstring), not a random or stratified sample. The result: **all 50
rows have `title_score = 1.0000`, `combined_score = 1.0000`,
`location_score = 1.0000`, and `team_score = 1.0000` (49/50; one row has no
team on either side)**, and `title_only = false` on every row (so
`title_only_min` — which only fires when no other component is known — is
never even reached). Every false positive found below scored a *perfect*
1.0 on every component the matcher checks; none of them were "almost" gated
out on similarity. So the false positives here are not a fuzzy-matching
problem — they are cases where the algorithm's inputs (title, team,
location, description-hash-equality) are genuinely identical between two
*different* real postings, because the postings are concurrent duplicates,
not because the scoring is too permissive.

**Pattern A — evergreen/rolling roles produce concurrent duplicate reqs
that collide with the matcher (the dominant false-positive class).**
`includedhealth.com`'s "Psychiatrist" role runs multiple simultaneous open
reqs per location (confirmed independently in Washington, Texas, and
Pennsylvania):
- Ranks 14/15 (WA): old req `1859975b` closed and TWO new WA reqs
  (`d2383a2f`, `d69c2327`) opened with byte-identical
  `first_observed`/`first_seen_absent` timestamps — an arbitrary pick
  between exact twins.
- Ranks 21/22 (TX) and 32/33 (PA): same twin-opening pattern.
- Rank 20 (TX): the "successor" (`03a46278`) is actually an 11-month
  continuously-open evergreen req that merely happened to co-occur with
  `2095c271`'s closure — not a successor at all.
- Rank 17 (Remote): the old req was itself the sole survivor of a former
  3-way concurrent Remote wave, so even a single, otherwise-plausible
  42-day-gap successor can't be trusted here — marked `unclear`.

  Of this cluster, **3 rows that the algorithm actually accepted as matches
  (ranks 14, 20, 32) are real false positives** — every score is perfect,
  so no threshold change fixes this; the fix has to be structural (e.g.
  reject/flag a candidate pair when more than one new posting shares the
  identical (title, team, location) tuple within the same appearance
  window at that company — a uniqueness check, not a similarity gate).

**Pattern B — genuine parallel reqs at a single company collide the same
way even outside "evergreen" roles.** `harvey.ai` "Staff Software Engineer,
Frontend" (ranks 8/9) and "IT Operations Analyst" (ranks 45/46): in both
cases TWO brand-new distinct ats_job_ids (confirmed via different
`description_hash`es) appear in the *same* snapshot as candidates for the
same closed old posting. Rank 8 could be disambiguated (its candidate
stayed open through both later crawls, its rival closed again quickly) but
ranks 45/46 could not — both marked `unclear` rather than guessed.

**Pattern C — id churn on a continuously-live posting slips past the
`job_id`/`canonical_url` hard gate (`version`, rank 49).**
`clay.com` "Account Executive (GTME - New Business)": old (`53939c5e`) and
new (`1cd80341`) share a byte-identical `description_hash`
(`b3abcce9...`), and that *same* hash also belongs to a third, still-earlier
id (`edcab69b`) spanning six continuous weeks before that — three different
Ashby job ids, zero real gap, identical JD text throughout. This is one
continuously-live listing whose ATS id keeps getting reissued, not a
close/repost cycle, and it is currently scored and accepted as `is_match =
true`. The matcher's hard gate ("different `ats_job_id` and different
`canonical_url`") isn't enough by itself to catch this — it needs a
same-content, zero-gap signal too (e.g. down-rank or flag a candidate as
likely-`version` when `description_score == 1.0` AND `gap_days` is at or
near 0, rather than treating identical content as pure corroboration).

**Pattern D — chain-skipping inflates `gap_days` on otherwise-correct
matches (not a false positive, but a threshold-relevant artifact).**
`gohighlevel.com` "Implementation Advisor, Mexico" has a real sequential
chain `3d04fe80 -> 33125d36 -> ff2ad979 -> 5a92b4ad`. Ranks 30 and 39 each
pair an old posting directly with a *non-adjacent* descendant, skipping the
correct intermediate link (which is present in the sample as ranks 31 and
38, both real, but rank 31 lost its assignment to rank 30's longer-gap
pairing): rank 30 shows `gap_days=117.39` instead of the true adjacent-step
`gap_days=60.90` (rank 31); rank 39 shows `gap_days=86.40` instead of the
adjacent `gap_days=0` (rank 38, which won). Both rank 30 and 39 are still
`true_repost` (it is the same role), but the recorded gap is misleadingly
large and the wrong link wins the one-to-one assignment.

**Data-quality caveat — sparse capture cadence, not real-time gaps.**
Most `gap_days` values in this sample (including nearly every `0.0000`)
reflect capture cadence, not a literal same-instant transition:
`incident.io` and `ironcladapp.com` only have archive snapshots weeks to
months apart, and `capital.com` and the entire `gamma.app`/`harvey.ai`
"own"-crawl cohort show 40+ postings all flipping `first_seen_absent` at
the exact instant our own crawler's first pass ran (2026-09-07T17:43:51Z).
This doesn't create false positives by itself (the true-vs-false split
here is not correlated with `gap_days`), but it means `gap_days≈0` should
never be read as "the same moment" — only as "somewhere inside this
company's capture gap."

## 4. Threshold recommendations for `[matching]`

Current values (from `config.toml`, all still flagged PLACEHOLDER there):
`title_min=0.85`, `title_only_min=0.95`, `team_min=0.80`, `location_min=0.90`,
`description_min=0.80`, `combined_min=0.70`, `max_gap_days=120`,
`pre_absence_tolerance_days=7.0`.

- **`title_min`: keep at 0.85 (no change).** Every row in this sample —
  true and false alike — scores `title_score = 1.0000`. There is zero
  evidence in this sample bearing on where the real discriminating line is
  between 0.85 and 1.0. Recommend pulling a *second*, separately-sampled
  batch stratified across the 0.70–0.95 title-score band (not top-N by
  combined score) before touching this value.
- **`title_only_min`: keep at 0.95 (no change, untested).** `title_only` is
  `false` for all 50 rows — this gate never fired once in the whole
  sample. It is completely unvalidated by this exercise; do not infer
  anything about it from this report.
- **`combined_min`: keep at 0.70 (no change).** Same issue as `title_min`:
  every row (true and false) scores `combined_score = 1.0000`, so this
  sample supplies no signal on whether 0.70 is too loose or too strict. The
  false positives found here (Pattern A/B/C above) all pass `combined_min`
  by the widest possible margin — moving this knob would not touch any of
  them.
- **`max_gap_days`: lower from 120 to 115.** This is the one knob this
  sample actually supports changing, and only marginally: the highest
  `gap_days` among *confirmed, well-evidenced* true positives are rank 41
  (114.23 days, harvey.ai Contracts Manager, full open-then-close lifecycle
  observed) and rank 23 (112.33 days, ironcladapp.com, confirmed via
  adjacent archive snapshots) — both must stay admitted. The next-highest
  value in the whole sample is rank 30's chain-skipping artifact at 117.39
  days (Pattern D). Setting `max_gap_days=115` sits in the ~1–3 day window
  between these: it excludes only rank 30's over-reaching pairing (which
  loses nothing — the same role is still correctly captured by the
  now-freed-up adjacent link, rank 31, at 60.90 days) while both confirmed
  true positives (112.33, 114.23) remain admitted. Rank 39's chain-skip
  (86.40 days) is unaffected by this change — catching it too would need a
  much lower cap (roughly 85), and that is NOT recommended: it would also
  cut confirmed true positives sitting just above that line, e.g. rank 50
  (94.03 days, confirmed via 6 intermediate absent captures — the single
  best-evidenced true positive in the whole sample) and rank 34 (66.44
  days, confirmed via 3 intermediate absent captures, would survive but
  leaves very little headroom). So 115 is the recommended value: it fixes
  the one clearly-identifiable, cost-free case (rank 30) and leaves the
  rest of the chain-skip problem (rank 39) to the structural fix below
  rather than a threshold cut that would do more harm than good.

**Bigger-impact recommendation not covered by the four knobs above:** the
dominant false-positive class (Pattern A, 3 of 7 `different_role` rows
currently accepted as matches: ranks 14, 20, 32) is caused by concurrent
duplicate postings at one company sharing an identical (title, team,
location) tuple — no similarity threshold can fix this since every score is
already 1.0. Recommend adding a structural check to `rank_matches`/
`assign_one_to_one`: if more than one currently-open posting at the same
company shares the exact (title, team, location) tuple at the time a
candidate "new" posting first appears, treat that tuple as non-discriminating
for this pair (require description-hash equality or drop the candidate)
rather than accepting title+team+location as sufficient corroboration.
Separately, the `version` miss (Pattern C, rank 49) suggests adding an
explicit "likely id-churn, not a repost" flag when `description_score ==
1.0` and `gap_days` is at or near 0 — right now identical content is scored
as strong *corroboration* for a repost, when zero-gap + identical content is
actually the version pattern the existing `job_id`/`canonical_url` gate was
supposed to catch and, in this one case, didn't.

## 5. Files

- `data/match_sample_checked.csv` — all 50 original rows/columns, in
  original order, with `human_verdict` filled in and a new `justification`
  column appended.
- `data/match_precision.md` — this report.

No other files were written or modified. `data/rli.db`, `config.toml`,
`spec.md`, and `PLAN.md` were only read (via the read-only
`file:data/rli.db?mode=ro` URI for the database).
