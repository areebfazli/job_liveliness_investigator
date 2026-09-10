"""Synthetic corpus, HTTP mocks and LLM scripting shared by `tests/test_agent*.py`.

Named `test_agent_helpers` so it sits inside the `tests/test_agent*.py` glob
(the suite that owns `rli/agent`), exactly as `tests/test_eval_helpers.py`
does for `rli/eval` and `tests/test_history_helpers.py` for `rli/history`. It
contains no tests of its own.

The corpus builders wrap `tests/test_eval_helpers.py` rather than restating
its SQL: the agent suite needs the same postings/captures a System A or B
test needs, plus ONE extra fact — a board history that is deep enough to be
usable while leaving `repost_pattern` UNKNOWN. That combination is what makes
the history-gated dynamic probes (`repost_history`, `requirements_drift`)
eligible at all, and it is not obvious. With the `tests/test_eval_system_b.py`
corpus, `rli.history.features` classifies a posting that was never observed
absent as `repost_pattern='none'`; the input is then POPULATED, so
`could_change_action` drops it, so `eligible_probes` refuses both history
probes and System C can only ever run `company_events`. `seed_reposted_history`
therefore closes the posting and links it to a successor whose match carries
no description component — the one branch of
`rli.history.features._classify_repost_pattern` that yields UNKNOWN on thick
history ("a repost link exists but neither side carries a description_hash").

`scripted_llm` exists because System C makes calls against TWO prompt
templates and a bare `rli.llm.client.ScriptedClient` sequence would couple
every test to the interleaving of the two. Dispatching on
`prompt.template_id` lets a test script an investigator sequence and an
explanation independently, so adding a loop iteration to a scenario cannot
silently shift which canned object the explanation call receives.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx
from pydantic import BaseModel
from test_eval_helpers import COMPANY, add_capture, add_posting, job
from test_eval_helpers import run_steps as _eval_run_steps

from rli.agent.explanation import ExplanationOutput, ReasonDraft
from rli.agent.investigator import InvestigatorOutput, ProbeCandidate
from rli.config import Config
from rli.llm.client import Prompt, ScriptedClient
from rli.llm.prompts import TEMPLATE_EXPLANATION, TEMPLATE_INVESTIGATOR
from rli.models.decision import Decision
from rli.models.time import to_utc_z

# The Greenhouse tenant every fixture in this suite uses; it must match the
# `tenant` default in `test_eval_helpers.add_posting`, because that is what
# `rli.eval.case` resolves the posting identity against.
TENANT = "acme"

# A page with no JSON-LD, so `page_structured` never contributes a second
# publish date and the fixtures' `publish_recency` is a function of the
# Greenhouse `first_published` field alone.
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"

# A path that deliberately does not exist. `rli.probes.company_events`
# documents a missing collection-status file as an empty map ("no company has
# been investigated yet" -> UNKNOWN, and no `company_events_searched` claim),
# so passing this pins every run in this suite to that state instead of to
# whatever `data/events/collection_status.csv` happens to hold in the working
# checkout. It reaches the probe on the `ProbeContext`, via
# `rli.eval.runner.open_system_runner`.
NO_COLLECTION_STATUS = Path(__file__).with_name("_no_collection_status.csv")

# `rli.llm.client.compute_cost_usd` prices a call from `[llm.prices]`, and an
# id missing from that table costs 0.0 without raising. A scripted client must
# therefore use a PRICED id, or every cost-cap assertion would pass vacuously
# against a run that spent nothing — which rules out the default local model,
# whose price is legitimately 0.0.
MODEL_ID = "gemini-2.5-pro"

# What `scripted_llm` answers with once its investigator script runs out; see
# that function's docstring for why exhaustion is a STOP and not an error.
EXHAUSTED_STOP_REASON = "scripted investigator sequence exhausted"


# ---------------------------------------------------------------------------
# Greenhouse HTTP mocking (adapted from tests/test_eval_system_b.py)
# ---------------------------------------------------------------------------


def gh_job(job_id: str, *, now: datetime, first_published_days_ago: int) -> dict[str, Any]:
    """One Greenhouse Job Board API job payload, published `n` days before `now`."""
    return {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/{TENANT}/jobs/{job_id}",
        "first_published": (now - timedelta(days=first_published_days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "content": "<p>Build things.</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }


def job_url(job_id: str) -> str:
    """The human-facing board URL a test hands to a system as its input url."""
    return f"https://boards.greenhouse.io/{TENANT}/jobs/{job_id}"


def mock_greenhouse(
    job_id: str,
    payload: dict[str, Any],
    *,
    job_api_side_effect: Callable[[httpx.Request], httpx.Response] | None = None,
) -> None:
    """Route the three URLs a Greenhouse case touches, inside an active `respx.mock`.

    `job_api_side_effect` replaces the single-job API route's canned 200. It
    is the only controllable failure surface in this fixture, and it is shared
    by `resolve_posting` (always-run) and `requirements_drift` (dynamic) —
    which is exactly what makes a "succeed for the resolver, then fail" side
    effect able to drive the loop's bounded probe retry.
    """
    api = f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{job_id}"
    if job_api_side_effect is None:
        respx.get(api).mock(return_value=httpx.Response(200, json=payload))
    else:
        respx.get(api).mock(side_effect=job_api_side_effect)
    respx.get(job_url(job_id)).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [payload]})
    )


def mock_greenhouse_missing(job_id: str) -> None:
    """Route the same three URLs for a posting the ATS no longer serves.

    A 404 from the job API plus a board listing that does not contain the job
    is how `rli.eval.case` arrives at `posting_state='closed'` — the state
    spec.md §5's unconditional `P1_closed` branch reads, and therefore the
    only way to build a case whose action cannot move.
    """
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{job_id}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    respx.get(job_url(job_id)).mock(return_value=httpx.Response(404, text="gone"))
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )


# ---------------------------------------------------------------------------
# History seeding
# ---------------------------------------------------------------------------


def seed_history(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> str:
    """50 days of board history for an open posting; returns its `posting_id`.

    Usable (>= `[thresholds].min_history_days`) but not long-lived
    (< `[thresholds].long_lived_days`), the same window
    `tests/test_eval_system_a.py` and `tests/test_eval_system_b.py` use. The
    posting is never observed absent, so `repost_pattern` resolves to `'none'`
    and BOTH history probes are ineligible — use `seed_reposted_history` for a
    corpus where they can run.
    """
    posting_id = add_posting(
        conn,
        job_id=job_id,
        first_observed=now - timedelta(days=60),
        last_seen_open=now - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, now - timedelta(days=offset), [job(job_id)], company_id=COMPANY)
    return posting_id


def seed_reposted_history(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> str:
    """Thick history that leaves `repost_pattern` UNKNOWN; returns the `posting_id`.

    The one corpus in which System C can actually exercise a history-gated
    dynamic probe. See the module docstring for why the obvious corpus cannot:
    a posting that was never observed absent is classified `'none'`, which
    populates the input and makes `repost_history` / `requirements_drift`
    permanently ineligible.

    So the posting is closed (`first_seen_absent`), a successor posting is
    added, and a `repost_links` row records a match scored on title and team
    only. `rli.history.features._classify_repost_pattern` treats a link whose
    `component_scores` carry no `description` entry as UNKNOWN, because
    "repeated unchanged" is a positive claim about content that a missing hash
    cannot support. `has_usable_history` is unaffected (it reads the company's
    capture window), so the probes stay eligible while the question stays open.

    `first_observed` is deliberately older than `thresholds.long_lived_days`.
    The §5 policy branch these probes feed (P4, "repeated unchanged repost +
    long-lived history") requires `long_lived is True`, and since spec.md §5's
    Amendment 2026-09-10 that is the plain comparison `age_days >=
    long_lived_days` — the old "UNKNOWN while our own history is short" hedge
    is gone (see `rli.history.features`). A 60-day-old posting would now
    report `long_lived=False`, which makes P4 unreachable, which makes
    `repost_pattern` irrelevant to the action, which makes `repost_history`
    and `requirements_drift` INELIGIBLE — and this corpus exists precisely so
    that they are eligible.
    """
    old_posting_id = add_posting(
        conn,
        job_id=job_id,
        first_observed=now - timedelta(days=400),
        last_seen_open=now - timedelta(days=20),
        first_seen_absent=now - timedelta(days=15),
    )
    new_posting_id = add_posting(conn, job_id=f"{job_id}9", first_observed=now - timedelta(days=14))
    for offset in (60, 45, 30, 10):
        add_capture(
            conn,
            now - timedelta(days=offset),
            [job(job_id), job(f"{job_id}9")],
            company_id=COMPANY,
        )
    conn.execute(
        """
        INSERT INTO repost_links (
            company_id, old_posting_id, new_posting_id,
            combined_score, component_scores, matched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            COMPANY,
            old_posting_id,
            new_posting_id,
            0.9,
            # No "description" key: the branch that yields UNKNOWN.
            json.dumps({"scores": {"title": 0.98, "team": 0.9}}),
            to_utc_z(now),
        ),
    )
    conn.commit()
    return old_posting_id


# ---------------------------------------------------------------------------
# Scripting the two prompt templates
# ---------------------------------------------------------------------------


def candidate(probe: str, **args: Any) -> ProbeCandidate:
    """One `ProbeCandidate`, with `args` exactly as proposed (valid or not)."""
    return ProbeCandidate(probe=probe, args=dict(args), argument="because")


def propose(probe: str, **args: Any) -> InvestigatorOutput:
    """An investigator turn that proposes exactly one probe."""
    return InvestigatorOutput(candidates=[candidate(probe, **args)])


def cited_explanation(
    text: str, *evidence_ids: str, hypotheses: tuple[str, ...] = ()
) -> ExplanationOutput:
    """An explanation whose single reason cites `evidence_ids`.

    Cited on purpose: `rli.agent.explanation` drops any reason left without a
    valid citation and falls back to the deterministic reasons, so an UNCITED
    scripted explanation would silently test the fallback path instead of the
    LLM-authored one.
    """
    return ExplanationOutput(
        reason=[ReasonDraft(text=text, evidence_ids=list(evidence_ids))],
        hypotheses=list(hypotheses),
    )


def scripted_llm(
    investigator: Sequence[InvestigatorOutput | Exception] = (),
    *,
    explanation: ExplanationOutput | Exception | None = None,
    cfg: Config | None = None,
    model_id: str = MODEL_ID,
    input_tokens: int = 8_000,
    output_tokens: int = 1_000,
) -> ScriptedClient:
    """A `ScriptedClient` that dispatches on `prompt.template_id`, never on call order.

    `investigator` is consumed in order, one entry per investigator call; an
    `Exception` entry is raised (which is how a test scripts an `LLMError` or
    an `LLMSchemaError`). `explanation` answers every explanation call and is
    NOT consumed, because System C makes exactly one.

    **Exhausting the investigator script yields a STOP**
    (`InvestigatorOutput(stop=True)`), not an error. The alternative —
    `ScriptedClient`'s own "exhausted" `LLMError` — would turn an off-by-one
    in a test's script into a `controller_decision:stop:investigator_error`
    row, i.e. into the scenario that `test_agent_loop.py` tests deliberately
    elsewhere. A scenario would then silently become a different scenario and
    still pass its ordering assertions. A STOP is the neutral terminator: a
    test that wants an investigator failure scripts the exception explicitly.

    `model_id` defaults to a PRICED id (see `MODEL_ID`) and `cfg` is what
    turns the token counts into dollars — a client built without `cfg` reports
    `cost_usd=0.0` for every call, which no budget assertion can distinguish
    from a run that was correctly stopped.

    The returned client records every call in `ScriptedClient.calls`; use
    `template_calls` to read one template's prompts back out.
    """
    remaining = list(investigator)

    def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        if prompt.template_id == TEMPLATE_INVESTIGATOR:
            if not remaining:
                return InvestigatorOutput(stop=True, stop_reason=EXHAUSTED_STOP_REASON)
            item = remaining.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if prompt.template_id == TEMPLATE_EXPLANATION:
            if isinstance(explanation, Exception):
                raise explanation
            return explanation if explanation is not None else ExplanationOutput()
        raise AssertionError(f"unexpected prompt template_id {prompt.template_id!r}")

    return ScriptedClient(
        responder,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cfg=cfg,
    )


def template_calls(llm: ScriptedClient, template_id: str) -> list[Prompt]:
    """The prompts `llm` was actually asked for one template, in call order."""
    return [prompt for prompt, _schema in llm.calls if prompt.template_id == template_id]


# ---------------------------------------------------------------------------
# Trace reading
# ---------------------------------------------------------------------------


def run_steps(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """This run's `run_steps` rows ordered by `step_index`.

    Delegates to `test_eval_helpers.run_steps` rather than repeating the
    query: the agent suite and the eval suite must read the one canonical
    trace (spec.md §7) the same way, or an ordering assertion here could pass
    against a differently-sorted view of the same table.
    """
    return _eval_run_steps(conn, run_id)


def steps_matching(steps: Sequence[sqlite3.Row], prefix: str) -> list[sqlite3.Row]:
    """The rows whose `decision_type` starts with `prefix`, order preserved.

    A prefix, not an equality test, because the vocabulary
    `rli.agent.loop` writes is colon-qualified
    (`controller_decision:stop:step_cap`, `candidate_rejected:duplicate`) and
    a model row additionally carries the `:tokens=i/o` suffix.
    """
    return [row for row in steps if str(row["decision_type"]).startswith(prefix)]


def decision_fingerprint(decision: Decision) -> dict[str, Any]:
    """`decision.model_dump(mode="json")` with each evidence item's `run_id` dropped.

    Everything else about a `Decision` is a function of the case, so two runs
    over the same corpus at the same `now` must agree field for field — but
    `EvidenceItem.run_id` names the run that collected the item and is
    different by construction. Evidence IDS (`e1`, `e2`, ...) are NOT stripped:
    they are assigned per run in append order, so identical runs assign
    identical ids and a renumbering would be a real regression worth failing on.
    """
    payload = decision.model_dump(mode="json")
    for item in payload.get("evidence", []):
        item.pop("run_id", None)
    return payload
