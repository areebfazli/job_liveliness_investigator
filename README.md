# Job-Liveness Investigator

> **Is this job worth applying to right now, and what should I do next?**

## What it is

Many job postings online are not what they seem. Some were filled months ago
and never taken down. Some are reposted again and again. Some belong to
companies that have just frozen hiring. Job seekers waste hours tailoring
applications for jobs that were never really open.

`rli` takes a job link and checks the evidence before you spend that time:

- **Is the posting still live** on the company's own hiring system?
- **How fresh is it?** When was it published or last refreshed?
- **Has it been reposted** over and over, or open for a very long time?
- **What is the company doing?** Is it hiring more people on the same team, or are there layoffs or a hiring freeze in the news?

It then gives one of four answers:

| Answer | Meaning |
|---|---|
| **apply_now** | Strong, fresh evidence the job is real and wanted. Put in real effort. |
| **quick_apply** | Probably open, but the evidence is thin. Apply with little tailoring. |
| **wait** | Something is unresolved, for example news of a hiring freeze. Check again in N days. |
| **skip** | Closed, or a long-running repost with signs the company is not really hiring. |

Every answer comes with short reasons, and each reason links to the dated
source it is based on. The tool never claims a job is "fake". It only reports
what the evidence shows and how strong that evidence is.

## How it works

1. **Collect.** Every day it records the job boards of 351 tech companies that
   use Greenhouse, Ashby or Lever. It also pulls older copies of those boards
   from the Wayback Machine, plus dated company news (funding, layoffs, freezes).
   This history is what lets it spot reposts and long-open jobs.
2. **Investigate.** For a given link it runs small checks called *probes*, such
   as "is it still on the board?", "has it been reposted?", "is the team
   hiring?" and "any company news?". Each probe produces timestamped evidence.
3. **Decide.** A fixed, rule-based policy turns the evidence into one of the
   four answers. The decision is always made by these rules, never by an AI.
4. **Explain.** The answer comes with reasons that cite the evidence.

There are four ways to choose which probes to run. They are compared in the
evaluation:

- **A: full.** Runs every probe. This is the most thorough and most expensive option, and it is the reference the others are measured against.
- **B: rules.** A fixed checklist decides which probes to run.
- **C: agent.** An LLM proposes which probes to run, then writes the explanation. A deterministic controller enforces budgets and allowed tools, and stops once nothing left could change the answer.
- **R: controller without the LLM.** The same controller as C, but it simply runs every probe that could still change the answer. It makes no AI calls.

The question the project answers is whether the agent (C) reaches the same
answers as the full system (A) while running fewer expensive checks than the
rules (B), and whether the LLM adds anything over the same controller without
it (R).

## Key numbers

Status as of 2026-10-07.

**Data collected**

| | |
|---|---|
| Companies tracked | 351 |
| Job postings seen | 39,696 |
| Posting closures observed | ~21,000 |
| Days of own daily collection | 23 (since 2026-09-07) |
| Wayback board captures | 1,452 |
| Dated company events | 773 |

**Evaluation status: no valid result yet.** The first full evaluation
(2026-09-30) reported that the agent passed its gate. Two independent reviews
then found that result does not hold:

| Finding | What it means |
|---|---|
| A no-LLM run of the same controller reproduced the agent on 10,587 of 10,588 cases | The savings came from the controller's rules, not the LLM. The agent is not shown to add anything. |
| Only 46 of 7,618 time-split cases (0.6%) depend on any probe result | The 100% agreement with the full system was close to automatic. |
| 74 held-back test companies (2,000 cases) were also in the time split's tuning data | The company-split result is not an independent test. |
| Some evidence was stamped earlier than it was fetched (by up to ~18 h) | The "0 leaks" result was wrong. The leak checker now catches this, and the old datasets fail it. |
| Ashby's date is "last published", not "first published" | Re-posted jobs looked new. |
| 286 postings were scored, not 300 | The time split misses its 300-posting target. |
| Every strong-evidence case sits at the moment the dataset was built | There is no real history of strong evidence yet. Only 0.8% of time-split cases had a first-publish date. |

All of these are fixed in the code (2026-10-06/07). The datasets will be rebuilt
on correctly dated data and A, B, C and R rerun. The old reports in `reports/`
are kept for the record but should not be cited.

**What is still unproven**

- **Whether the advice is right.** No real application outcomes are logged yet, so **product value is unproven**.
- **Whether the LLM adds value** over the same controller without it (R). If it does not, the spec says to prefer the simpler system.
- **The policy is not frozen yet.** The time-based held-out test split has not been read.

## Run it yourself

**Requirements:** Linux or macOS, Python 3.12, [uv](https://docs.astral.sh/uv/),
and internet access. The agent (System C) also needs an LLM endpoint; see step 5.

### 1. Install and create the database

```bash
git clone git@github.com:areebfazli/job_liveliness_investigator.git
cd job_liveliness_investigator
uv sync
uv run rli init-db --path ./data/rli.db
uv run rli load-targets --targets scripts/targets.csv --db ./data/rli.db
uv run rli load-events --path data/events/company_events.csv --db ./data/rli.db
```

### 2. Collect data

```bash
uv run rli snapshot --db ./data/rli.db                       # today's job boards (~4 min)
uv run rli archive backfill --months 12 --db ./data/rli.db   # older copies from Wayback (slow)
uv run rli history rebuild --db ./data/rli.db                # derive closures and reposts
```

To collect every day automatically, run `scripts/setup_laptop_timer.sh`. It
installs a systemd timer that runs at 00:05 and 12:05 and catches up after
sleep. For an always-on machine, use `scripts/setup_cloud_vm.sh`. Both are
described in `docs/deploy-snapshot.md`.

### 3. Check a job link

```bash
uv run rli run --system B --url https://boards.greenhouse.io/<company>/jobs/<id> --db ./data/rli.db
uv run rli agent run --url https://boards.greenhouse.io/<company>/jobs/<id> --db ./data/rli.db   # agent (C)
```

### 4. Web page and API

```bash
RLI_DB_PATH=./data/rli.db uv run python -m rli.api    # then open http://localhost:8000/
```

- `POST /investigate {url, system}` returns the answer. If the LLM is unreachable, it falls back to the rules (B) and marks the response `degraded: true`.
- `POST /outcomes` logs what happened after you applied: `applied`, `reply`, `screen`, `interview`, `offer`, `rejection` or `silence`. These outcomes are what can eventually show whether the advice works.
- `/watch` lets you watch a job and have it rechecked when it is due.
- The server runs on localhost only, unless you set `RLI_API_TOKEN`; then every request needs that token. `RLI_API_RPM` sets the per-IP rate limit.

### 5. LLM for the agent (System C)

C works with any OpenAI-compatible endpoint. Set it in `config.toml` under `[llm]`.
The default is Mistral:

```bash
echo 'MISTRAL_API_KEY=...' > .env    # free key at console.mistral.ai; .env is gitignored
```

The posting title, URL, company name, evidence snippets and news headlines
are sent to that provider, and free tiers may train on them. To keep
everything local, use Ollama (`base_url = "http://localhost:11434/v1"`) with a
model that supports structured JSON output.

### 6. Reproduce the evaluation

```bash
uv run rli replay build --dataset my-set --split dev --split-kind temporal --grid-days 7 --db ./data/rli.db
# company holdout: same command with --split-kind company, company-disjoint from my-set
uv run rli replay build --dataset my-company-set --split dev --split-kind company --grid-days 7 --exclude-companies-from my-set --db ./data/rli.db
uv run rli replay run --system A --dataset my-set --db ./data/rli.db
uv run rli replay run --system B --dataset my-set --db ./data/rli.db
WORKERS=4 TOTAL_RPM=160 scripts/replay_c_parallel.sh my-set   # agent, 4 workers in parallel
uv run rli replay check --dataset my-set --db ./data/rli.db   # must report 0 leaks
uv run rli eval run --dataset my-set --with-c --out reports/evaluation.md --db ./data/rli.db
```

`replay build` is the only replay step that uses the network. Every later step
is replayed offline, with live calls blocked. Each case's split is frozen when
the dataset is built, so evaluation never re-splits a corpus that has grown since.

### 7. Tests

```bash
uv run pytest -q && uv run ruff check .    # 1,530 tests
```

## Built-in safety rules

- **No looking ahead.** Evidence is stamped with when it became available. Replay sees only data from on or before the test date, and every replay run is audited for leaks.
- **The AI never decides.** It only picks probes and writes the explanation. Its output must match a schema, it can call only allow-listed sites, it has hard budget and step limits, and it cannot repeat the same call.
- **Job and news text is treated as data**, never as instructions to the AI.
- **No guessing.** A failed or missing capture is recorded as a gap, never as "job closed". Closure dates are kept as ranges, never invented.
- **Every reason must cite real evidence**, and this is checked.
- **Out of scope:** no "ghost job" verdicts, no LinkedIn scraping, no crowd scores, no made-up 0–100 scores.

## More detail

| File | What's in it |
|---|---|
| `spec.md` | Full requirements, including the action policy and its amendments |
| `PLAN.md` | Build order |
| `PROGRESS.md` | Running log of status and decisions |
| `config.toml` | All thresholds, budgets, rate limits and allowlists |
| `reports/` | Generated evaluation reports |
| `docs/` | Deployment and demo walkthroughs |

```
rli/  net · resolvers · snapshots · archive · history · events · probes
      policy · eval · agent · llm · replay · api · ui · cli.py
```
