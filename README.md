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

There are three ways to choose which probes to run. They are compared in the
evaluation:

- **A: full.** Runs every probe. This is the most thorough and most expensive option, and it is the reference the others are measured against.
- **B: rules.** A fixed checklist decides which probes to run.
- **C: agent.** An LLM decides which probes are worth running, then writes the explanation. A deterministic controller enforces budgets and allowed tools, and stops the agent once nothing left could change the answer.

The question the project answers is whether the agent (C) reaches the same
answers as the full system (A) while running fewer expensive checks than the
rules (B).

## Key numbers

Status as of 2026-10-04.

**Data collected**

| | |
|---|---|
| Companies tracked | 351 |
| Job postings seen | 39,696 |
| Posting closures observed | ~21,000 |
| Days of own daily collection | 23 (since 2026-09-07) |
| Wayback board captures | 1,452 |
| Dated company events | 773 |

**Evaluation (2026-09-30).** This is a point-in-time replay. Each test case is
a job at a past date, and the system sees only what was knowable on that date.

| | Time split (dev) | Company split (dev) |
|---|---|---|
| Postings / companies / cases | 300 / 246 / 7,905 | 200 / 138 / 2,683 |
| Agent's expensive probes per case vs rules (C vs B) | 0.98 vs 1.43, **ratio 0.68** | 0.98 vs 1.55, **ratio 0.63** |
| Agent's answers matching the full system | **100%** | **100%** |
| Future-data leaks | 0 | 0 |
| **Agent gate** (needs ratio ≤ 0.70 and agreement within 2 points of B) | **PASS** | **PASS** |

For the agent, C ran on Mistral `ministral-8b` at no cost on the free tier. Full
reports are in `reports/evaluation.md` and `reports/evaluation_company_split.md`.

**Answers on recent cases**, from after daily collection began (all three
systems agree):

| | apply_now | quick_apply | wait | skip |
|---|---|---|---|---|
| Time split (503 cases) | 45 | 313 | 4 | 141 |
| Company split (465 cases) | 60 | 327 | 25 | 53 |

**What these numbers do not show yet**

- **Whether the advice is right.** Matching the full system shows the agent is consistent, not that its answers are correct. That needs real application outcomes, and none are logged yet, so **product value is unproven**.
- **Most cases are rated "weak"**, so `quick_apply` dominates. The main cause was that publish dates were not being saved. This was fixed on 2026-10-03, and dated evidence is building up now. Datasets will be rebuilt and rerun on it.
- **The rules (B) also match the full system 100%.** The agent wins on cost (fewer checks), not on accuracy.
- **The policy is not frozen yet**, and the held-out test split has not been touched.

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
uv run rli replay run --system A --dataset my-set --db ./data/rli.db
uv run rli replay run --system B --dataset my-set --db ./data/rli.db
WORKERS=4 TOTAL_RPM=160 scripts/replay_c_parallel.sh my-set   # agent, 4 workers in parallel
uv run rli replay check --dataset my-set --db ./data/rli.db   # must report 0 leaks
uv run rli eval run --dataset my-set --with-c --out reports/evaluation.md --db ./data/rli.db
```

`replay build` is the only replay step that uses the network. Every later step
is replayed offline, with live calls blocked.

### 7. Tests

```bash
uv run pytest -q && uv run ruff check .    # 1,446 tests
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
