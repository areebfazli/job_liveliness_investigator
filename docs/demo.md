# Demos

Three demo scripts per spec.md §8 Phase 5 / PLAN.md M7: a clear case, an
ambiguous case, and a failure/recovery case. Every command below was
actually run against a scratch database (never `data/rli.db`) while writing
this document; output shown is real output, lightly trimmed for length,
not a mockup. Live-data demos (1) will drift over time as real postings
open/close — the shapes described are what to expect, not a guarantee of
the exact values shown.

All commands assume you are in the repo root and have run `uv sync`. Use a
scratch path for `--db` / `RLI_DB_PATH`, e.g. under `/tmp`, so nothing here
touches `data/rli.db`.

```bash
export DB=/tmp/rli-demo.db
uv run rli init-db --path "$DB"
```

## 1. Clear case: a healthy, recently-posted, still-open role

**Finding a real target:** `scripts/targets.csv` lists verified ATS
targets. Any row with `ats=greenhouse` gives a `tenant` you can query
directly:

```bash
grep greenhouse scripts/targets.csv | head -3
# Affirm,affirm.com,greenhouse,affirm,...
curl -s "https://boards-api.greenhouse.io/v1/boards/affirm/jobs?content=false" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['jobs'][0])"
```

This demo used **Affirm** (`tenant=affirm`), job id `7850544003`
("Administrative Assistant IV", first published 2026-08-19, 19 days before
this demo was run), i.e. the URL
`https://boards.greenhouse.io/affirm/jobs/7850544003`.

### CLI form

```bash
uv run rli run --system B --url "https://boards.greenhouse.io/affirm/jobs/7850544003" --db "$DB"
```

Actual output (trimmed to the decision; the run also printed 4 evidence
items, all `source_quality: ats_native`):

```json
{
  "posting_state": "open",
  "recommended_action": "quick_apply",
  "recheck_after_days": null,
  "evidence_quality": "strong",
  "hypotheses": [],
  "reason": [
    {"text": "The posting was still listed on the company's job board when we checked.", "evidence_ids": ["e1"]},
    {"text": "The role was first published 19 days ago, according to ats-native evidence.", "evidence_ids": ["e2"]}
  ],
  "evidence": ["... 4 items, all ats_native, from resolve_posting + board_snapshot ..."]
}
```

`recommended_action` came out `quick_apply` rather than `apply_now` because
this scratch database has no board history for Affirm yet (a fresh
`init-db`, not the long-running snapshot corpus) — System B's `R1` route
falls through to the policy's default when history-gated inputs are
`Unknown`, per spec.md §4 ("missing history never means flat hiring"). On a
database with real running history, `evidence_quality: strong` +
`posting_state: open` + a genuinely recent, uncontested publish date can
instead reach `apply_now`. Either way, the **shape** is what to expect for
a clear case: `posting_state: open`, `evidence_quality: strong`, at least
one `reason` citing a `resolve_posting` evidence item, and an action of
`apply_now` or `quick_apply` — never `wait` or `skip`.

### API form

```bash
RLI_DB_PATH="$DB" RLI_WATCH_STORE_PATH=/tmp/rli-demo-watches.json uv run python -m rli.api &
sleep 2
curl -s -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://boards.greenhouse.io/affirm/jobs/7850544003", "system": "B"}'
kill %1
```

Actual output (same decision fields as the CLI form, plus the API's own
envelope fields):

```json
{
  "posting_state": "open",
  "recommended_action": "quick_apply",
  "recheck_after_days": null,
  "evidence_quality": "strong",
  "hypotheses": [],
  "reason": ["... same two reasons as above ..."],
  "evidence": ["... same 4 items ..."],
  "run_id": "937277da7c3641b0b82f31f4222ae663",
  "system_used": "B",
  "degraded": false,
  "degraded_reason": null
}
```

## 2. Ambiguous case: mixed evidence

A live posting old enough, or with just the right archive/company-event
history, to land on `mixed`/`weak` evidence is not reliably found on
demand — it depends on what the long-running snapshot corpus happens to
hold at demo time. The reproducible way to show this shape is the same
pattern `tests/test_eval_system_b.py` and `tests/test_api.py` use: mock a
Greenhouse job whose ATS-native `first_published` disagrees with its own
career page's JSON-LD `datePosted` by more than `config.toml`'s
`contradiction_days` threshold. `rli.policy.quality.quality_of` treats two
disagreeing dated sources as a contradiction — evidence_quality: `mixed`
(see `tests/test_policy_quality.py::test_archive_publish_evidence...` and
`::test_contradiction_threshold_is_configuration` for the underlying unit
tests this mirrors).

This runs entirely offline — `respx.mock` intercepts the outbound HTTP
calls, so no real network access happens and the result is fully
deterministic and reproducible.

### CLI-equivalent form (direct Python, no server)

Save as `ambiguous_demo.py` and run with `uv run python ambiguous_demo.py`:

```python
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from rli.config import load_config
from rli.db import init_db, connect
from rli.eval.system_b import run_system_b

NOW = datetime(2026, 9, 8, tzinfo=UTC)
JOB_ID, TENANT = "9001", "acme"
URL = f"https://boards.greenhouse.io/{TENANT}/jobs/{JOB_ID}"

gh_job = {
    "id": int(JOB_ID),
    "title": "Senior Backend Engineer",
    "absolute_url": URL,
    "first_published": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}
jsonld_html = f"""<html><head><script type="application/ld+json">{json.dumps({
    "@context": "https://schema.org/", "@type": "JobPosting",
    "title": "Senior Backend Engineer",
    "datePosted": (NOW - timedelta(days=120)).strftime("%Y-%m-%d"),
})}</script></head><body>Senior Backend Engineer</body></html>"""

init_db("ambiguous_demo.db")
conn = connect("ambiguous_demo.db")
cfg = load_config()

with respx.mock:
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=gh_job))
    respx.get(URL).mock(return_value=httpx.Response(200, text=jsonld_html))
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]}))
    result = run_system_b(conn, cfg, URL, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

print(json.dumps(result.decision.model_dump(mode="json"), indent=2, default=str))
```

Actual output:

```json
{
  "posting_state": "open",
  "recommended_action": "quick_apply",
  "recheck_after_days": null,
  "evidence_quality": "mixed",
  "hypotheses": [],
  "reason": [
    {"text": "The posting was still listed on the company's job board when we checked.", "evidence_ids": ["e1"]},
    {"text": "The role was first published 5 days ago, according to ats-native evidence.", "evidence_ids": ["e2"]}
  ],
  "evidence": [
    "e1: resolve_posting/posting_state=open (ats_native)",
    "e2: resolve_posting/first_published=2026-09-03 (ats_native)",
    "e3: resolve_posting/first_published=2026-05-11 (page_structured) <- the conflicting date",
    "e4: board_snapshot/board_present=9001 (ats_native)"
  ]
}
```

`evidence_quality: mixed` with `recommended_action: quick_apply` is the
expected shape for conflicting evidence per spec.md §5's policy table
(`open + mixed/weak evidence -> quick_apply`).

**Note on hypotheses:** run this same case through System C
(`uv run rli agent run --url ... --db ...` or `POST /investigate` with
`system: "C"`) and, with a reachable LLM endpoint configured (a local
Ollama or any other OpenAI-compatible server — see the README's "LLM
setup"), the investigator may populate `hypotheses` with plausible
interpretations of the conflict (e.g. "the career page may show a different
posting date than the ATS record"). **With no LLM endpoint reachable,
`hypotheses` will always be empty** — this is an honest, current limitation
of this environment (see the main README's Limitations section), not a bug
in the ambiguous case itself.

### API form

Same mocked evidence, driven through the HTTP layer with FastAPI's
`TestClient` (in-process — no separate `uv run python -m rli.api` process
needed, since `TestClient` drives the ASGI app directly and `respx` still
intercepts the outbound calls the request handler makes):

```python
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx
from fastapi.testclient import TestClient

from rli.api.app import create_app

NOW = datetime(2026, 9, 8, tzinfo=UTC)
JOB_ID, TENANT = "9002", "acme"
URL = f"https://boards.greenhouse.io/{TENANT}/jobs/{JOB_ID}"

gh_job = {
    "id": int(JOB_ID), "title": "Senior Backend Engineer", "absolute_url": URL,
    "first_published": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}], "offices": [{"name": "Remote"}],
}
jsonld_html = f"""<html><head><script type="application/ld+json">{json.dumps({
    "@context": "https://schema.org/", "@type": "JobPosting",
    "title": "Senior Backend Engineer",
    "datePosted": (NOW - timedelta(days=120)).strftime("%Y-%m-%d"),
})}</script></head><body>Senior Backend Engineer</body></html>"""

app = create_app(db_path="ambiguous_demo_api.db", watch_store_path="ambiguous_demo_watches.json")

with TestClient(app) as client, respx.mock:
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=gh_job))
    respx.get(URL).mock(return_value=httpx.Response(200, text=jsonld_html))
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]}))
    resp = client.post("/investigate", json={"url": URL, "system": "B"})

print(resp.status_code, json.dumps(resp.json(), indent=2))
```

Actual output: `STATUS: 200`, `evidence_quality: "mixed"`,
`recommended_action: "quick_apply"`, `system_used: "B"`, `degraded: false`
— identical decision fields to the CLI-equivalent form above, plus the same
`run_id`/`system_used`/`degraded`/`degraded_reason` envelope fields as demo
1's API form.

## 3. Failure/recovery case: graceful handling of bad or unreachable input

All three sub-cases below return a well-formed HTTP response — none of them
crash the server. Start the API once for all three:

```bash
RLI_DB_PATH=/tmp/rli-demo-fail.db RLI_WATCH_STORE_PATH=/tmp/rli-demo-fail-watches.json \
  uv run python -m rli.api &
sleep 2
```

### 3a. A nonexistent Greenhouse job id (404 from the ATS)

```bash
curl -s -o /tmp/resp.json -w "STATUS:%{http_code}\n" \
  -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://boards.greenhouse.io/affirm/jobs/1", "system": "B"}'
cat /tmp/resp.json
```

Actual result: **`STATUS:200`** (not an error — a missing job id is a
finding, not a fault):

```json
{
  "posting_state": "closed",
  "recommended_action": "skip",
  "recheck_after_days": null,
  "evidence_quality": "weak",
  "hypotheses": [],
  "reason": [
    {"text": "The posting was no longer listed on the company's job board when we checked.", "evidence_ids": ["e1"]},
    {"text": "A board snapshot taken since then did not list this posting.", "evidence_ids": ["e2"]}
  ],
  "evidence": [
    {"id": "e1", "probe": "resolve_posting", "claim_type": "posting_state", "value": "closed", "source_quality": "ats_native"},
    {"id": "e2", "probe": "board_snapshot", "claim_type": "board_absent", "value": "1", "raw_excerpt": "205 job(s) listed, none with id 1", "source_quality": "ats_native"}
  ],
  "system_used": "B",
  "degraded": false,
  "degraded_reason": null
}
```

The resolver's 404/absent-from-board result becomes `posting_state:
closed`, `evidence_quality: weak`, `recommended_action: skip` — the same
graceful degradation path a genuinely-closed posting takes (spec.md §5:
`closed -> skip`), never a crash or a 5xx.

CLI equivalent (identical decision, via `rli run` instead of the API):

```bash
uv run rli run --system B --url "https://boards.greenhouse.io/affirm/jobs/1" --db "$DB"
# stderr: run_id=... system=B probes=(none) route=R0_terminal
```

### 3b. System C requested (or defaulted) with no reachable LLM endpoint

```bash
# With nothing serving [llm].base_url (no `ollama serve`, no API key for a
# remote endpoint), System C is unavailable and the API says so.
curl -s -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://boards.greenhouse.io/affirm/jobs/7850544003"}'
```

(`system` is omitted, so it defaults to `"C"`.) Actual result:
**`STATUS:200`**, deterministic and offline-reproducible (no live network
flakiness — this path never reaches a model):

```json
{
  "posting_state": "open",
  "recommended_action": "quick_apply",
  "evidence_quality": "strong",
  "...": "... full spec.md §1 decision fields ...",
  "system_used": "B",
  "degraded": true,
  "degraded_reason": "LLM endpoint http://localhost:11434/v1 is not reachable: ...; ran System B instead of System C"
}
```

`system_used: "B"` and `degraded: true` confirm the fallback actually
happened rather than silently returning a System-C-shaped but empty
response. The exact `degraded_reason` text is whatever the endpoint probe
reported — an unreachable endpoint, or a missing API key for a non-local
`base_url`. `GET /health` reports the CONFIGURED endpoint ahead of time
without probing it (so it always answers instantly):
`{"status": "ok", "llm_configured": true, "llm_base_url":
"http://localhost:11434/v1", "llm_model_id": "qwen3:8b"}`.

### 3c. A disallowed URL

Two independent rejections, both schema/allowlist violations caught before
any network call (`rli.net.check_allowed`), both returning **HTTP 422**:

```bash
# http:// instead of https://
curl -s -o /tmp/resp.json -w "STATUS:%{http_code}\n" \
  -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "http://boards.greenhouse.io/affirm/jobs/7850544003"}'
cat /tmp/resp.json
# STATUS:422
# {"detail":"scheme 'http' is not https"}

# IP-literal host instead of a named public host
curl -s -o /tmp/resp.json -w "STATUS:%{http_code}\n" \
  -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://127.0.0.1/jobs/1"}'
cat /tmp/resp.json
# STATUS:422
# {"detail":"host is an IP literal (only named public hosts are allowed)"}
```

Both are exact, real output from this environment. Stop the demo server
when done:

```bash
kill %1
```
