# Pilot: archived per-job ATS pages (Wayback Machine, Common Crawl index)

Date: 2026-10-08. A one-day pilot that decides whether a full integration is worth building.
Code: `rli/pilot/wayback_pages.py` (collection and parsers), `rli/pilot/analysis.py`
(analysis) and `scripts/pilot_wayback.py` (driver). Data: `data/pilot/wayback_job_pages.db`,
`data/pilot/cc_index.db` and the raw response cache `data/pilot/raw/` (gitignored).
`data/rli.db` was opened read-only (`mode=ro`, `PRAGMA query_only`) and was not changed.

## Verdict

| Question | Answer |
|---|---|
| **Coverage**: do archived job pages add point-in-time evidence? | **Yes, for Greenhouse and Ashby boards hosted by the ATS. Little for Lever. None for Greenhouse tenants that use their own careers site.** Across 5,429 of our postings at 20 companies, 58% have at least one archived job-page capture and 50% get a publish date from a capture. In dev-7d-v4, **256 of 505 cases (51%) at these companies would gain a first-publish date available at T**. Re-running the frozen policy turns all 256 from weak to strong evidence and 107 from `quick_apply` to `apply_now`. |
| **Signal**: with this history, do company habit, reposts and team activity predict closure better than age alone? | **Not convincingly, and the archive does not change that.** Age alone barely ranks closures (AUC 0.51–0.56). Posting-level signals (repost, new roles on the team) add nothing over age within a company. Company habit separates companies (within-fold AUC up to 0.67 against 0.53–0.55 for age, with a CI that excludes 0 in 2 of the 4 model × data combinations). But it does not hold up across model types, and it is confounded with how densely each company is observed. Reposted jobs still close at about the same rate as others. |
| **Recommendation** | **GO for a narrow integration**: archived Greenhouse (and Lever) job-page publish dates as point-in-time first-publish evidence, plus open/closed observations. **NO-GO for building a closure-prediction signal on top of them.** The spec amendment "an archived ATS job page counts as primary evidence" is justified for Greenhouse `published_at` (it is the ATS field itself; 342 of 342 matched our API value exactly). It is not justified for Ashby (`publishedDate` is last-published). It is plausible but under-tested for Lever (6 of 6 matched). |

## 1. Method

### Companies (20; 9 Greenhouse / 8 Ashby / 3 Lever)

The 20 companies come from the replay datasets. I mixed densely and sparsely archived ones, using the
count of archived *board* captures already in rli.db as a rough guide. reddit, canonical and cohere
were excluded as instructed.

| ATS | Company (tenant) | Why |
|---|---|---|
| Greenhouse | affirm, carta, discord, vercel, webflow, figma | dev-7d-v4; 14–38 archived board captures each |
| Greenhouse | brex, instacart, duolingo | company-7d-v4; no archived boards in rli.db |
| Ashby | notion, vanta, cognition, incident, modal | dev-7d-v4; 2–14 archived board captures |
| Ashby | cursor, temporal, drata | company-7d-v4; 0–1 archived board captures |
| Lever | includedhealth, extremenetworks | dev-7d-v4 |
| Lever | binance | company-7d-v4 |

### Collection

* **CDX**: 29 prefix queries over 2025-10-01..2026-10-08. The prefixes were `job-boards.greenhouse.io/<t>/jobs/`,
  `boards.greenhouse.io/<t>/jobs/`, `jobs.ashbyhq.com/<t>/` and `jobs.lever.co/<t>/`. No status filter and
  no digest collapse were applied: a collapsed run of identical captures would hide the last time a page was
  seen open. There were 17,567 job-page captures of 5,389 jobs; 13,405 of those captures map to 3,201 of our postings.
  URLs were normalised by dropping the scheme, `gh_src`, `utm_*` and `embed`, mapping
  Ashby `/application` and Lever `/apply` pages to their job, and dropping Greenhouse `/confirmation`.
  Each job maps to our `posting_id` through `(company_id, ats_job_id)`.
* **Fetches**: 3,300 raw `id_` fetches (the cap was 3,000, raised by 300 so that every mapped Ashby job got its
  earliest capture). They ran 13:33–15:41 UTC (about 2.3 s per fetch, under the configured
  `web.archive.org` limit of 0.5 req/s). Wayback answered **0 × HTTP 429**. There were 11 transport errors, all
  retried, and 2 persistent gaps, recorded as gaps. Archived redirects were **not** followed, because the
  redirect target is the data. The fetch order was:
  1. A validation sample: up to 8 captures per (company, host, status) class, half of them from the
     own-collection era so they can be checked against our own board captures. 436 fetches.
  2. The earliest 200 capture of every mapped Greenhouse or Lever posting, for its publish date. 1,795 fetches.
  3. The earliest capture of every mapped Ashby job. 1,069 fetches.

  The per-URL plan of up to 8 captures (earliest, latest, month boundaries) is implemented in
  `select_captures`. With this budget only the top rank was reached for Ashby. For Greenhouse and Lever
  more captures were unnecessary, because states are read from the CDX status (below).
* **States for captures that were not fetched** come from the CDX status code, and only where the fetched
  sample validated the rule: at least 95% agreement, n ≥ 15 globally or n ≥ 6 for one company. Everything
  else stays `unknown`. A failed or throttled fetch is a gap, never a closure.
* **Common Crawl (optional)**: index-only lookups of the same 29 prefixes in 12 crawls
  (CC-MAIN-2025-43..2026-39), about 3 h in the background; 341 of 348 queries succeeded. Two
  byte-range WARC reads confirmed that a CC capture of a Greenhouse page carries the same `published_at`.
  No CC content was used in the analysis.

### Parsers (`rli/pilot/wayback_pages.py`, tests in `tests/test_pilot_wayback_pages.py`)

| ATS | Open | Closed | Date |
|---|---|---|---|
| Greenhouse | 200 page with an embedded `jobPost` | 3xx to `<board>?error=true` | `published_at` = first publish |
| Ashby | `window.__appData.posting` present, `isListed: true` | 404 only | `posting.publishedDate` = **last** publish |
| Lever | 200 job page (JSON-LD) or `/apply` page | 404 | JSON-LD `datePosted` |

These findings correct the prior notes:

* **An Ashby empty shell (`"posting":null`, about 3 KB) is not a closure.** Shells occur for jobs our own
  collector saw open on the same day: all 143 shell-sized own-era captures were of jobs our own board captures
  showed open, and every fetched cursor.com capture is a shell. Shells come and go by month for the same company, so they track how the
  crawler rendered the page, not the job's state. Ashby archived pages therefore give **no closure evidence
  at all**. Only 404s would count, and none occurred. A large capture (CDX `length` ≥ 6,000) is a full
  server-rendered page: 928 of 930 fetched agreed it was open and listed, so the rule was used for captures
  that were not fetched.
* **A Greenhouse 302 means "closed" only on boards hosted by Greenhouse.** On tenants with a custom careers
  site (instacart; one webflow capture) it redirects to that site, so the rule is validated per company.
  `boards.greenhouse.io` captures are 301/302 host-move redirects and are uninformative.
* **Lever job pages stay 200 after the job leaves the board.** includedhealth dropped 64 jobs from its
  board between 09-08 and 09-13, and Wayback captured 21 of them as live job pages on 09-16. Lever
  "open" on an archived page means "page live", not "listed".

### Validation against our own data

| Check | Result |
|---|---|
| Greenhouse archived `published_at` vs our own API `first_published` | **342 / 342 identical to the second** |
| Lever archived `datePosted` vs our own job-page date | 6 / 6 identical |
| Ashby archived `publishedDate` vs our own `last_published` (any own capture) | 264 match, 59 differ (republished between the two captures, which is the expected behaviour of a last-publish date) |
| CDX inference vs fetched parse | GH job-boards 200 → open 1,575/1,579; GH 302 → closed 100% on 6 of 8 Greenhouse-hosted boards (instacart 0/8, webflow 7/8: not used); Lever 404 → closed 34/34; Lever 200 → open 308/310; Ashby large 200 → open 928/930 |
| Page state vs our own board captures, same day (own era) | Greenhouse: open/open 280, closed/closed 26, **0 contradictions**. Ashby (large page): open/open 828, 0 contradictions. Lever: 61 agree, **21 page-open / board-absent** (above) |

## 2. Coverage

Postings are our rli.db postings at these companies that were open at some point since 2025-10-01.
"Before DB first sighting" means the first archived page observation predates the earliest sighting in
rli.db (own plus archived boards). "Before own first" means it predates our own collector's first sighting.

| ATS | Company | Postings | ≥1 capture | Usable open/closed obs | Before DB first sighting | Before own first | Publish date | First-publish date | Archived closed obs | Closure the DB lacks | Jobs only in archive |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **all** | | 5,429 | 58.0% | 53.4% | 21.7% | 14.8% | 50.0% | 32.8% | 2.4% | 0.1% | 2,188 |
| **Greenhouse** | all | 2,784 | 56.5% | 55.1% | 17.5% | 11.5% | 54.2% | 54.2% | 3.8% | 0.1% | 782 |
| | affirm | 681 | 77.2% | 77.1% | 31.9% | 15.6% | 76.4% | 76.4% | 2.5% | 0 | 382 |
| | carta | 270 | 70.7% | 70.7% | 27.0% | 8.5% | 70.4% | 70.4% | 3.0% | 0 | 54 |
| | discord | 254 | 94.1% | 94.1% | 32.7% | 19.7% | 92.9% | 92.9% | 13.0% | 0 | 74 |
| | figma | 502 | 65.1% | 61.6% | 11.4% | 16.3% | 59.0% | 59.0% | 8.0% | 0.2% | 110 |
| | vercel | 215 | 59.1% | 55.8% | 14.9% | 12.1% | 55.3% | 55.3% | 2.8% | 0 | 43 |
| | webflow | 188 | 83.5% | 79.3% | 12.8% | 17.0% | 79.3% | 79.3% | 0.5% | 0 | 49 |
| | brex | 387 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0 | 0 |
| | instacart | 185 | 3.2% | 0% | 0% | 0% | 0% | 0% | 0% | 0 | 35 |
| | duolingo | 102 | 1.0% | 1.0% | 1.0% | 1.0% | 0% | 0% | 1.0% | 1.0% | 35 |
| **Ashby** | all | 1,591 | 77.9% | 64.6% | 32.1% | 22.6% | 58.5% (last-published) | 0% | 0% | 0% | 489 |
| | notion | 449 | 99.6% | 98.7% | 72.6% | 31.6% | 94.2% | 0% | 0% | 0 | 200 |
| | cognition | 136 | 97.1% | 96.3% | 35.3% | 59.6% | 89.0% | 0% | 0% | 0 | 2 |
| | modal | 71 | 83.1% | 81.7% | 28.2% | 38.0% | 67.6% | 0% | 0% | 0 | 2 |
| | vanta | 569 | 68.2% | 55.9% | 13.7% | 11.6% | 45.9% | 0% | 0% | 0 | 152 |
| | incident | 74 | 59.5% | 48.6% | 13.5% | 20.3% | 48.6% | 0% | 0% | 0 | 29 |
| | temporal | 86 | 40.7% | 40.7% | 24.4% | 24.4% | 40.7% | 0% | 0% | 0 | 2 |
| | drata | 51 | 11.8% | 11.8% | 11.8% | 11.8% | 11.8% | 0% | 0% | 0 | 47 |
| | cursor | 155 | 83.2% | 0.6% | 0.6% | 0.6% | 0.6% | 0% | 0% | 0 | 55 |
| **Lever** | all | 1,054 | 31.8% | 31.8% | 17.4% | 11.7% | 25.9% | 25.9% | 2.3% | 0.2% | 917 |
| | binance | 400 | 33.2% | 33.2% | 25.5% | 25.5% | 22.2% | 22.2% | 3.5% | 0.2% | 851 |
| | includedhealth | 320 | 42.5% | 42.5% | 22.8% | 4.1% | 39.4% | 39.4% | 0.6% | 0 | 55 |
| | extremenetworks | 334 | 19.8% | 19.8% | 2.4% | 2.4% | 17.4% | 17.4% | 2.4% | 0.3% | 11 |

Takeaways:

* **Publish dates are the payoff.** On Greenhouse-hosted boards 55–93% of postings get an exact
  first-publish date, from a capture usually made close to publication. For postings that appeared during our own
  collection, the first archived capture came a median 0.9 days after our first sighting, and 32% came before it. Ashby gives a last-publish date,
  and repeated captures of one page would date republishes. Only the earliest capture was fetched here, so
  republish detection is untested.
* **Closures barely improve.** Archived closed observations exist for 2.4% of our postings and add a closure
  the DB lacked for 0.1%. Board captures already carry closures. Archived job pages mostly observe *open*
  jobs, because aggregators link to live jobs.
* **Greenhouse tenants with their own careers site are invisible** (brex, instacart, duolingo):
  `job-boards.greenhouse.io/<t>/jobs/<id>` redirects to the careers site, and `boards.greenhouse.io/embed/job_app?for=<t>`
  had 0–7 captures. Covering them would need per-company careers-site parsers (JSON-LD), a separate project.
* **Common Crawl is complementary for Greenhouse**: 1,762 mapped Greenhouse postings in the CC index against 1,624 in
  Wayback, 478 of them in CC only (brex 122, figma 71, carta 60, vercel 51). For brex these are 301/302s to
  the careers site, so they carry no date. CC adds almost nothing for Ashby (947 of 980 also in Wayback) or Lever
  (42 postings). CC has monthly granularity, about 2× the effort, and was not analysed beyond the index.

### Replay cases that would gain a first-publish date available at T

Cases of these 20 companies in the v4 datasets. A gain needs an archived Greenhouse `published_at` or Lever
`datePosted` captured at or before T, with the same "not after our first observation" guard the
replay builder applies. The policy was re-run with `rli.policy` (`derive_policy_inputs` →
`evidence_quality` → `decide`) on the System A run's own evidence plus one extra `first_published` claim.
History features were not reloaded (`long_lived` and `repost_pattern` stay UNKNOWN). Every counted case
reproduced its recorded quality and action without the extra claim (256 of 256 and 2 of 2).

| Dataset | Cases at these companies | Weak | With primary publish evidence now | Would gain a first-publish date at T | Re-run: weak → strong | `quick_apply` → `apply_now` | Unchanged action |
|---|---:|---:|---:|---:|---:|---:|---:|
| dev-7d-v4 | 505 | 503 | 2 | **256** | 256 | **107** | 149 (138 quick_apply, 11 skip) |
| company-7d-v4 | 195 | 164 | 31 | 2 | 2 | 0 | 2 |

company-7d-v4 gains almost nothing. Its cases start at 2026-09-09 and its companies are mostly
careers-site Greenhouse tenants, Ashby (last-publish only) or Lever. **No policy was changed**: these counts
only show what the amendment would do. The extra claim was tagged `ats_native` for Greenhouse and
`page_structured` for Lever, matching what the amendment would say.

## 3. Signal test (point-in-time)

**Design.** There are 10 monthly cuts, 2025-11-01..2026-08-01, chosen so that cut + 60 days falls before
today. The unit is a (posting, cut) pair. A posting enters the sample at cut *c* when its latest observation
at or before *c* is open and at most 31 days old.

**Label.** The label is "closed by *c* + 60 d". It is 1 when a closed or absent observation falls in
(*c*, *c*+60]. It is 0 when an open observation falls at or after *c*+60 with no closed observation in the
window. When the status at *c*+60 is unknown (the closure interval straddles *c*+60, or nothing was observed
later), the case is **dropped**: 2,580 of 5,733 DB cases and 4,164 of 9,057 archive cases. Cases that
reappeared within the window are dropped too.

**Features** (data available at or before *c* only):

| Feature | Definition |
|---|---|
| Age | *c* − earliest of first sighting / publish dates captured by *c* (log) |
| Company habit | Share of the company's postings first seen ≥ 90 d before *c* that were seen open ≥ 90 d; median time-to-close (closure-interval midpoint − first sighting) of its closures observed by *c*. Each needs ≥ 5 postings, else missing + indicator |
| Repost | `repost_links` new posting whose old posting's absence was observed by *c*, or an Ashby republish dated by *c* |
| Team activity | log(1 + new roles on the same team in (*c*−30, *c*]) |

Models were logistic regression and gradient boosting, with GroupKFold (5 folds) by company. 95% CIs come
from a company-cluster bootstrap (500 resamples). A pooled AUC over out-of-fold predictions is biased
*down* for weak models under leave-companies-out CV, because a held-out fold's base rate runs opposite to
its training folds'. The headline is therefore the **within-fold** AUC, alongside the
**within-company-cut** AUC, which isolates posting-level signal because company habit is constant there.

**Variants:**

* **DB** = own captures + archived *boards* already in rli.db.
* **DB + pages** = DB plus archived job pages: observations, publish dates, Ashby republish dates.
* **Own collection only cannot be tested.** It spans 31 days (since 2026-09-07), so no cut has a 60-day
  horizon. Even with a shorter horizon, age would be truncated at 31 days and company habit (which needs 90
  days of history) would be undefined for every company. Historical data is a precondition for this
  test at all.

| Data | Model | Cases / postings / companies | Closed ≤ 60 d | AUC age only | AUC full | Δ full − age (95% CI) | Within company-cut: age → full | Brier age / full / base rate |
|---|---|---|---:|---|---|---|---|---|
| DB | logit | 3,153 / 1,747 / 13 | 46.5% | 0.550 [0.45, 0.63] | **0.672** [0.59, 0.72] | **+0.12 [+0.02, +0.19]** | 0.530 → 0.555 (Δ CI [−0.02, +0.06]) | 0.265 / **0.241** / 0.249 |
| DB | GBM | same | | 0.492 [0.46, 0.53] | 0.533 [0.42, 0.59] | +0.04 [−0.08, +0.11] | 0.500 → 0.541 (Δ CI [−0.02, +0.09]) | 0.279 / 0.291 / 0.249 |
| DB + pages | logit | 4,893 / 2,275 / 15 | 38.7% | 0.526 [0.46, 0.60] | 0.566 [0.44, 0.65] | +0.04 [−0.11, +0.14] | 0.513 → **0.391** (Δ CI [−0.22, −0.03]) | 0.245 / 0.245 / 0.237 |
| DB + pages | GBM | same | | 0.527 [0.50, 0.56] | **0.668** [0.58, 0.70] | **+0.14 [+0.05, +0.17]** | 0.529 → 0.536 (Δ CI [−0.03, +0.05]) | 0.251 / **0.230** / 0.237 |
| Same 3,135 cases, DB features | logit | 3,135 / 1,741 / 13 | 46.4% | 0.533 | 0.630 | +0.10 [−0.07, +0.19] | 0.530 → 0.555 | 0.268 / 0.241 |
| Same 3,135 cases, DB + pages features | logit | same | | 0.556 | 0.624 | +0.07 [−0.07, +0.14] | 0.533 → 0.546 | 0.262 / 0.263 |

Posting-level only (age vs age + repost + team activity, no company habit):

| Data | Model | Within-fold age → +repost +team | Δ (95% CI) |
|---|---|---|---|
| DB | logit | 0.550 → 0.457 | −0.09 [−0.17, −0.01] |
| DB | GBM | 0.492 → 0.493 | 0.00 [−0.03, +0.04] |
| DB + pages | logit | 0.526 → 0.485 | −0.04 [−0.13, +0.01] |
| DB + pages | GBM | 0.527 → 0.528 | 0.00 [−0.03, +0.03] |

### Feature effects

These are standardized logit coefficients (DB / DB + pages). Raw single-feature AUC is in parentheses;
above 0.5 means a higher value goes with more closure.

* **Age**: −0.16 / −0.05 (0.45 / 0.46). Older surviving postings close slightly *less* within 60 days, a
  survivor effect, and the effect is weak.
* **Company long-open share**: −0.73 / −0.40 (0.38 / 0.44). This is the strongest signal: companies whose
  postings often stay open past 90 days close fewer postings in the next 60.
* **Company median time-to-close**: −0.20 / −0.08 (0.51 / 0.46). Weak.
* **Repost**: −0.05 / +0.02 (0.50 / 0.51). No effect.
* **Team new roles in 30 days**: −0.20 / +0.14 (pooled 0.53 / 0.55, but **0.41** within company-cut).
  Within a company, postings on teams that are actively adding roles close *less* often in 60 days. Its sign
  flips with pooling, so it is unstable.

Calibration (logit, DB): the predicted closure rate by quintile was 0.14 / 0.40 / 0.48 / 0.56 / 0.76 against
observed rates of 0.13 / 0.53 / 0.50 / 0.61 / 0.56. The model ranks the bottom quintile reasonably and is
overconfident at the top. The calibration tables for every model are in the analysis JSON.

### Repost re-check

| Data | Reposted: closed ≤ 60 d | Others | Age-adjusted std coef |
|---|---|---|---|
| DB | 53% [43, 63] (n = 100) | 46% [45, 48] (n = 3,053) | +0.04 |
| DB + pages | 48% [40, 56] (n = 152) | 39% [37, 40] (n = 4,741) | +0.07 |

The earlier finding stands. Reposted jobs do **not** stay open longer. If anything they close slightly
*more* often, and the difference vanishes after adjusting for age.

### Reading

* Age alone is close to useless for "will this posting close in 60 days?" (AUC 0.49–0.56).
* The only signal that beats age is **company habit**, and only between companies: +0.12 (logit, DB) and
  +0.14 (GBM, DB + pages), with CIs excluding 0. It is not stable. The other two model × data combinations
  show no gain, and GBM on DB data shows none at all. It is also confounded: a company's long-open share and
  its labels both depend on how densely that company is observed. With 13–15 companies the estimate rests
  on few independent units.
* Posting-level signals (repost, team activity) do **not** help within a company. Logit with them is worse
  than age alone.
* Archived job pages enlarge the sample (+55% cases, +2 companies). They do **not** improve the
  features: on the same 3,135 cases, DB features 0.630 vs DB + pages features 0.624.

## 4. Bias check (postings with vs without archived page captures)

| Stratum | Group | n | Closed by now | Median observed open days | Engineering team share |
|---|---|---:|---:|---:|---:|
| First seen before own collection | with capture | 2,550 | 83.3% | 42.6 | 24.0% |
| | without | 717 | 87.9% | 21.2 | 18.5% |
| Already open when own collection started | with capture | 424 | 37.3% | 30.7 | 13.2% |
| | without | 1,027 | 46.9% | 29.7 | 20.7% |
| New during own collection (first seen > 2 d after start) | with capture | 175 | 22.9% | 17.5 | 16.6% |
| | without | 536 | 21.1% | 12.0 | 15.7% |

* **The selection is by company far more than by posting.** The capture share runs from 0% (brex) and 1–3%
  (duolingo, instacart) to 97–100% (cognition, notion). Within a company, closure rates barely differ
  between captured and uncaptured postings: the median within-company difference is −2.3 pp before own
  collection, −2.0 pp already-open and −4.2 pp new.
* **Captured postings are observed open longer** (within-company median +13 d before own collection,
  +9 d for new postings), and in the signal sample the closure rate is 34% for postings with a capture
  vs 76% without. Archived pages over-represent postings that stayed up long enough for an aggregator to link
  them. Any closure model or prior estimated from them will be biased toward longer-lived postings.
* Archive pages are captured fast. For new postings the first capture lands a median 0.9 days after our first
  sighting, and 32% are captured before it. Publish dates are therefore usually available near the start
  of a posting's life.
* Engineering roles are slightly over-represented among captured postings before own collection (24% vs
  18.5%). This is consistent with aggregators that focus on tech jobs.

## 5. Limits

* There are 20 companies, chosen by hand. Coverage is extremely company-specific: hosted vs custom careers
  site, and the Ashby rendering quirk. A full run must expect perhaps 30–40% of companies with almost no
  coverage.
* States for captures that were not fetched rest on CDX status rules validated on 436 + 2,864 fetched
  captures. They are reliable for Greenhouse-hosted boards and Lever 404. Lever 200 means "page live", not
  "listed".
* Ashby: only each job's earliest capture was fetched, so dated republish detection from page-to-page
  `publishedDate` changes was not measured. Ashby closures cannot be observed from archived pages.
* Signal test: labels exist only where later observations exist, so dropped cases are not random (sparser
  companies drop more). The company-habit effect may partly measure observation density. Cuts reuse the
  same postings, which is handled by clustering on company and not on posting. All results are on the same 20
  companies; there was no held-out company set beyond grouped CV.
* The policy re-run did not reload history features (`long_lived`, `repost_pattern`), so a skip via the
  repost/long-lived branch would not be re-derived. All counted cases reproduced their recorded action
  without the extra claim.
* Common Crawl was index-only, with 7 of 348 queries failing (6 × HTTP 400, 1 × HTTP 502). The 2 WARC reads were a spot check.

## 6. Recommendation and effort

**GO (narrow):** add archived Greenhouse and Lever job pages as a point-in-time **publish-date** source
for replay and backfill, and record their open and closed observations as archive evidence
(`available_at` = capture time).

* Expected gain: about half of dev-temporal cases at hosted-Greenhouse companies move from weak to strong
  evidence. Without this, almost no strong-evidence history exists before 2026-10-03 (README: first-publish
  coverage 0.3%). It turns the "all strong evidence sits at build time" problem into a real history.
* Spec: the amendment "an archived ATS job page counts as primary evidence" looks **justified for
  Greenhouse `published_at`**. It is byte-for-byte the ATS's own field (342/342 exact matches), and only the
  transport is an archive, so it could be `ats_native` with `available_at` = capture time. For **Lever
  `datePosted`** it fits as `page_structured` (already primary under §3), with n = 6 matches only. For
  **Ashby**, keep §3's 2026-10-07 rule. An archived `publishedDate` is last-published and an archive capture
  must never satisfy the absence leg. It could at most date republishes as `last_published` claims.
* Keep these out: Ashby shells as closures, Lever 200 as "listed", Greenhouse 302 as closed on careers-site tenants.

**NO-GO:** a learned closure or "real, active hire" signal built on this history. Company habit is the
only candidate and is unstable. Posting-level signals do not beat age. The archive adds sample but not signal.

**Effort for the full version** (351 companies), about 4–6 days:

1. **CDX ingest for all tenants** (about 450 prefixes): ~1 h of throttled CDX. Add a table
   `archived_job_pages` (new schema version), reusing this pilot's normaliser, parsers and
   per-company rule validation. 1 day.
2. **One fetch per mapped Greenhouse/Lever posting** for the date: about 15–20k fetches. At the measured
   ~2.3 s per fetch, that is roughly 10–13 h as a resumable background job, then incremental nightly
   (about 100 new fetches per day). 0.5 day.
3. **Replay builder**: emit `first_published` claims (`available_at` = capture), with the
   first-observed guard and the leakage checker extended to the new source; tests. 1.5–2 days.
4. **Spec amendment** (user decision) and policy version bump; rebuild v5 datasets; rerun A/B/R and eval.
   1 day plus run time.
5. Optional: Common Crawl index for Greenhouse (+30% hosted-Greenhouse postings), with WARC byte-range
   reads. 1 day.

## Reproduce

```bash
uv run python scripts/pilot_wayback.py cdx                 # 29 CDX queries (cached)
uv run python scripts/pilot_wayback.py fetch --cap 3300    # resumable; cached under data/pilot/raw/http
uv run python scripts/pilot_wayback.py infer               # CDX-status states for unfetched captures
uv run python scripts/pilot_wayback.py cc --pilot-db data/pilot/cc_index.db   # optional CC index
uv run python scripts/pilot_wayback.py analyze --out data/pilot/analysis_final.json
```

All raw responses are cached, so a rerun makes no network requests. The analysis reads `data/rli.db`
read-only. The `analyze` step depends on rli.db's current contents (postings, board captures and runs as of
2026-10-08 ~12:25 local).
