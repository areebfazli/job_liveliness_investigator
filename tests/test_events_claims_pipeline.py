"""The `company_events` claims pipeline: probe -> evidence -> policy inputs.

spec.md §5 sources `material_negative_event`, `freeze_or_pause` and
`last_material_event_at` from the `company_events` probe, and spec.md §9/§1
require every user-facing reason to map to evidence. That makes ONE property
load-bearing across three modules, and this file is where it is pinned down:

    the triple the PROBE reports from the store
      ==
    the triple `rli.policy.inputs` derives from the probe's own CLAIMS

`rli.events.policy_signals.signals_from_facts` is the single rule both sides
run, so agreement should be structural rather than lucky — but "should be" is
what a test is for, and the failure mode when it is not is invisible: a
`wait` decision whose cited evidence says nothing about a layoff.

The other half of this file is the regression guard for the bug that
motivated the refactor. `rli.eval.case.build_case_state` used to read the
event store directly and hand `derive_policy_inputs` a pre-computed triple.
That answered the three policy inputs with NO evidence behind them, which
made `company_events` ineligible under spec.md §4's `populates &
unpopulated` rule, which stopped System C's controller at
`no_unresolved_question` before it ever ran the probe. `test_build_case_state
_leaves_event_inputs_unknown_with_a_collected_layoff` is the test that fails
against that old behaviour: it puts a collected, in-window, material layoff
in the store AND records the company as searched, and still demands UNKNOWN.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import respx
from test_eval_helpers import COMPANY, add_company, add_posting

from rli.config import Config
from rli.eval.case import build_case_state, extend_case_state
from rli.eval.runner import Run, open_probe_runner
from rli.events.policy_signals import (
    CLAIM_EVENTS_SEARCHED,
    COMPANY_EVENTS_PROBE,
    derive_policy_signals,
)
from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    read_collection_status_csv,
    upsert_event,
    write_collection_status_csv,
)
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.policy.action import PolicyThresholds, decide
from rli.policy.inputs import could_change_action, derive_policy_inputs
from rli.policy.quality import evidence_quality_detail
from rli.probes.base import ProbeContext
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe, company_events

NOW = datetime(2026, 9, 7, tzinfo=UTC)
JOB_ID = "7001"
URL = f"https://boards.greenhouse.io/acme/jobs/{JOB_ID}"

#: Inside `negative_event_window_days` (180 by default) and before `NOW`.
EVENT_DATE = date(2026, 8, 20)
STALE_EVENT_DATE = date(2024, 1, 15)


# ---------------------------------------------------------------------------
# Corpus / fixture builders
# ---------------------------------------------------------------------------


def _upsert(conn: sqlite3.Connection, event: CompanyEvent) -> None:
    """`upsert_event` with the `companies` FK satisfied.

    `company_events.company_id` is a foreign key, and no test here is about
    that constraint, so the company row is created on the way in rather than
    being one more line every scenario has to remember.
    """
    add_company(conn, event.company_id)
    upsert_event(conn, event)


def _event(**overrides) -> CompanyEvent:
    fields = dict(
        company_id=COMPANY,
        event_type="layoff",
        event_date=EVENT_DATE,
        available_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        source_url="https://news.example.com/acme-layoff",
        headline="Acme lays off 20% of staff",
        raw_excerpt="Acme said it would cut 20% of staff.",
        source_quality="news",
        materiality="material",
        collected_at=NOW,
    )
    fields.update(overrides)
    return CompanyEvent(**fields)


def _status_csv(tmp_path: Path, *, searched: bool = True, events_found: int = 1) -> str:
    """A collection-status file; `searched=False` writes an EMPTY one.

    An empty file (rather than a missing one) is the more honest fixture for
    "collection has run, but not for this company": both degrade to the same
    empty map, and writing the file proves the test is not accidentally
    exercising the missing-file path.
    """
    path = tmp_path / "collection_status.csv"
    rows = (
        [
            CollectionStatus(
                company_id=COMPANY,
                searched_at=NOW - timedelta(days=1),
                queries_run=5,
                events_found=events_found,
            )
        ]
        if searched
        else []
    )
    write_collection_status_csv(rows, path)
    return str(path)


def _claims_as_evidence(
    conn: sqlite3.Connection,
    ctx_factory: Callable[..., ProbeContext],
    status_csv: str,
    *,
    as_of: datetime = NOW,
) -> list[EvidenceItem]:
    """Run the probe and upgrade its claims exactly as a runner would.

    `EvidenceItem(id=..., run_id=..., probe=..., **claim.model_dump())` is the
    upgrade `rli.probes.base.ProbeClaim` documents, so this reproduces the
    real evidence list without booting a `Run`.
    """
    ctx = ctx_factory(now=lambda: NOW, collection_status_csv=status_csv)
    result = company_events(CompanyEventsArgs(company_id=COMPANY, as_of=as_of), ctx)
    assert result.ok is True
    assert result.data is not None
    return [
        EvidenceItem(id=f"e{index}", run_id="r1", probe=COMPANY_EVENTS_PROBE, **claim.model_dump())
        for index, claim in enumerate(result.data["evidence"], start=1)
    ]


def _triple(inputs: PolicyInputs) -> tuple[object, object, object]:
    return (
        inputs.material_negative_event,
        inputs.freeze_or_pause,
        inputs.last_material_event_at,
    )


# ---------------------------------------------------------------------------
# 1. The regression guard: the always-run pair does not answer these
# ---------------------------------------------------------------------------

GH_JOB = {
    "id": int(JOB_ID),
    "title": "Backend Engineer",
    "absolute_url": URL,
    "first_published": "2025-04-05T00:00:00Z",
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


def _mock_greenhouse() -> None:
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [GH_JOB]})
    )


@respx.mock
def test_build_case_state_leaves_event_inputs_unknown_with_a_collected_layoff(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """The regression test for the bug this pipeline was rebuilt around.

    Everything the OLD code needed to answer the three inputs is present: a
    material, in-window layoff in the `company_events` table, and a
    collection-status row saying this company was searched a day before the
    run clock. `build_case_state` must still leave all three UNKNOWN — the
    answer belongs to the `company_events` probe, and until it runs there is
    no evidence to hang the answer on (spec.md §5/§9).

    Concretely, all three must remain UNRESOLVED QUESTIONS, because that is
    what keeps `CompanyEventsProbe` eligible under spec.md §4's `populates &
    unpopulated` rule and therefore keeps System C's controller from
    stopping at `no_unresolved_question` before it ever looks.
    """
    add_posting(conn, job_id=JOB_ID, first_observed=NOW - timedelta(days=520), last_seen_open=NOW)
    _upsert(conn, _event())
    status_csv = _status_csv(tmp_path)
    _mock_greenhouse()

    # The fixture is HOT: reading the store directly — which is exactly what
    # the deleted `rli.eval.case._event_signals` did, with this same status
    # file — answers all three. So the assertions below are not vacuously
    # true; they are the difference between the old behaviour and this one,
    # and this test fails against the old code.
    assert _store_triple(conn, status_csv) == (True, False, datetime(2026, 8, 20, tzinfo=UTC))

    with Run(conn, cfg, input_url=URL, system="A", config_hash="cfg:test", started_at=NOW) as run:
        with open_probe_runner(
            conn,
            cfg,
            run,
            NOW,
            sleep=lambda _s: None,
            use_tool_cache=False,
            collection_status_csv=status_csv,
        ) as probes:
            case = build_case_state(conn, cfg, url=URL, now=NOW, probes=probes)

            assert _triple(case.inputs) == (UNKNOWN, UNKNOWN, UNKNOWN)
            assert {
                "material_negative_event",
                "freeze_or_pause",
                "last_material_event_at",
            } <= case.unpopulated
            # Nothing in the always-run evidence came from `company_events`.
            assert not [c for c in case.evidence if c.probe == COMPANY_EVENTS_PROBE]

            # ... and the probe, once run, DOES answer them — from claims.
            extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert case.inputs.material_negative_event is True
    assert case.inputs.last_material_event_at == datetime(2026, 8, 20, tzinfo=UTC)
    layoffs = [
        c for c in case.evidence if c.probe == COMPANY_EVENTS_PROBE and c.claim_type == "layoff"
    ]
    assert len(layoffs) == 1


# ---------------------------------------------------------------------------
# 2. probe -> claims -> the three policy inputs
# ---------------------------------------------------------------------------


def test_material_layoff_reaches_the_policy_through_claims(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _upsert(conn, _event())
    evidence = _claims_as_evidence(conn, ctx_factory, _status_csv(tmp_path))

    inputs = derive_policy_inputs(evidence, None, NOW)

    assert inputs.material_negative_event is True
    assert inputs.freeze_or_pause is False
    assert inputs.last_material_event_at == datetime(2026, 8, 20, tzinfo=UTC)


def test_hiring_freeze_reaches_the_policy_through_claims(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _upsert(
        conn,
        _event(
            event_type="hiring_freeze",
            headline="Acme announces a hiring freeze",
            source_url="https://news.example.com/acme-freeze",
        ),
    )
    evidence = _claims_as_evidence(conn, ctx_factory, _status_csv(tmp_path))

    inputs = derive_policy_inputs(evidence, None, NOW)

    assert inputs.freeze_or_pause is True
    assert inputs.material_negative_event is False
    assert inputs.last_material_event_at is None


def test_searched_with_zero_events_is_an_evidence_backed_negative(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    """ "Checked, nothing found" is `False`, and the `False` rests on a claim.

    The second half is the point: drop the `company_events_searched` claim
    and the same evidence list must fall back to UNKNOWN. Without that
    claim, `False` would be an assertion drawn from the ABSENCE of evidence,
    which spec.md §9 does not permit — and which is also what the
    point-in-time replay gate relies on, since dropping this one claim is
    exactly how it expresses "the search post-dates T".
    """
    evidence = _claims_as_evidence(conn, ctx_factory, _status_csv(tmp_path, events_found=0))

    assert [c.claim_type for c in evidence] == [CLAIM_EVENTS_SEARCHED]

    inputs = derive_policy_inputs(evidence, None, NOW)
    assert _triple(inputs) == (False, False, None)
    assert (
        not {
            "material_negative_event",
            "freeze_or_pause",
            "last_material_event_at",
        }
        & inputs.unpopulated()
    )

    without_search = [c for c in evidence if c.claim_type != CLAIM_EVENTS_SEARCHED]
    assert _triple(derive_policy_inputs(without_search, None, NOW)) == (
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
    )


def test_dropping_the_searched_claim_unanswers_a_true_signal(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    """Even WITH a layoff claim in hand, no search claim means not-yet-checked.

    This is the replay case: at a `T` before the collection ran, the gate
    keeps the news article (its `available_at` is the article's publish time)
    but drops the collection-status claim. The honest reading is then "we had
    not investigated this company at `T`", not "we knew about a layoff".
    """
    _upsert(conn, _event())
    evidence = _claims_as_evidence(conn, ctx_factory, _status_csv(tmp_path))
    assert any(c.claim_type == "layoff" for c in evidence)

    without_search = [c for c in evidence if c.claim_type != CLAIM_EVENTS_SEARCHED]

    assert _triple(derive_policy_inputs(without_search, None, NOW)) == (
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
    )


# ---------------------------------------------------------------------------
# 3. The "cannot disagree" property
# ---------------------------------------------------------------------------


def _store_triple(conn: sqlite3.Connection, status_csv: str, *, as_of: datetime = NOW):
    path = Path(status_csv)
    status = read_collection_status_csv(path) if path.is_file() else {}
    return derive_policy_signals(
        conn,
        COMPANY,
        as_of,
        status,
        window_days=PolicyThresholds.coerce(None).negative_event_window_days,
    )


def _assert_paths_agree(
    conn: sqlite3.Connection,
    ctx_factory,
    status_csv: str,
    *,
    as_of: datetime = NOW,
) -> tuple[object, object, object]:
    """Assert the store path and the claims path answer identically.

    Returns the agreed triple so each caller can additionally pin what the
    answer actually IS — agreement alone would be satisfied by both sides
    being wrong in the same way.
    """
    from_store = _store_triple(conn, status_csv, as_of=as_of)
    evidence = _claims_as_evidence(conn, ctx_factory, status_csv, as_of=as_of)
    from_claims = _triple(derive_policy_inputs(evidence, None, as_of))
    assert from_claims == from_store
    return from_store


def test_paths_agree_on_a_material_layoff(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _upsert(conn, _event())
    triple = _assert_paths_agree(conn, ctx_factory, _status_csv(tmp_path))
    assert triple == (True, False, datetime(2026, 8, 20, tzinfo=UTC))


def test_paths_agree_on_a_minor_layoff(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _upsert(conn, _event(materiality="minor", headline="Acme lays off 3 people"))
    triple = _assert_paths_agree(conn, ctx_factory, _status_csv(tmp_path))
    assert triple == (False, False, None)


def test_paths_agree_on_a_hiring_freeze(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _upsert(conn, _event(event_type="hiring_pause", headline="Acme pauses hiring"))
    triple = _assert_paths_agree(conn, ctx_factory, _status_csv(tmp_path))
    assert triple == (False, True, None)


def test_paths_agree_when_searched_with_no_events(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    triple = _assert_paths_agree(conn, ctx_factory, _status_csv(tmp_path, events_found=0))
    assert triple == (False, False, None)


def test_paths_agree_when_never_searched(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    """An event in the store, but no collection-status row: UNKNOWN on both sides."""
    _upsert(conn, _event())
    triple = _assert_paths_agree(conn, ctx_factory, _status_csv(tmp_path, searched=False))
    assert triple == (UNKNOWN, UNKNOWN, UNKNOWN)


def test_paths_agree_on_an_event_outside_the_window(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    """The claim is still emitted as evidence; only the SIGNAL is windowed."""
    _upsert(
        conn,
        _event(
            event_date=STALE_EVENT_DATE,
            available_at=datetime(2024, 1, 15, tzinfo=UTC),
            source_url="https://news.example.com/acme-2024",
            headline="Acme cut 30% of staff in 2024",
        ),
    )
    status_csv = _status_csv(tmp_path)
    triple = _assert_paths_agree(conn, ctx_factory, status_csv)
    assert triple == (False, False, None)

    evidence = _claims_as_evidence(conn, ctx_factory, status_csv)
    assert [c.claim_type for c in evidence] == [CLAIM_EVENTS_SEARCHED, "layoff"]


# ---------------------------------------------------------------------------
# 4. What the UNKNOWNs mean for the controller and for the pre-probe decision
# ---------------------------------------------------------------------------


def _open_recent_inputs(**overrides) -> PolicyInputs:
    """A posting the always-run pair alone would call `open` + `recent`."""
    base = dict(
        posting_state="open",
        publish_recency="recent",
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    base.update(overrides)
    return PolicyInputs(**base)


def test_unknown_event_inputs_stay_relevant_to_the_controller() -> None:
    """spec.md §4's stop rule must NOT close the event questions prematurely.

    `could_change_action` is a brute-force enumeration through the real
    policy, so this is asserted rather than read off the branch table: with
    the three event inputs unresolved, each must still be reported as able to
    move the action — otherwise `rli.probes.registry.eligible_probes` drops
    `company_events` and the controller stops without ever looking, which is
    the bug this whole change is about.

    Both directions are covered by ONE case: from `apply_now`, a `True`
    `material_negative_event` blocks P5, and a `True` plus a date after the
    last refresh reaches P3c's `wait`.
    """
    inputs = _open_recent_inputs()
    action = decide(inputs, "strong", NOW, None, long_lived=False, last_refreshed_at=None)
    assert action.recommended_action == "apply_now"
    assert action.branch == "P5_open_recent_strong"

    changeable = could_change_action(
        inputs,
        action.recommended_action,
        quality="strong",
        long_lived=False,
        last_refreshed_at=None,
        now=NOW,
    )
    assert {
        "material_negative_event",
        "freeze_or_pause",
        "last_material_event_at",
    } <= changeable


def test_leaving_the_event_inputs_unknown_cannot_fabricate_a_wait() -> None:
    """The pre-probe decision is unchanged by this refactor.

    P5 requires `material_negative_event is not True` (UNKNOWN passes) and
    P3c requires `is True` (UNKNOWN does not), so replacing a known `False`
    with UNKNOWN moves neither branch. That is what makes it safe to defer
    all three answers to the probe: the worst case is that the controller
    keeps a question open, never that the policy invents bad news.
    """
    for material in (False, UNKNOWN):
        inputs = _open_recent_inputs(
            material_negative_event=material,
            freeze_or_pause=material,
            last_material_event_at=None if material is False else UNKNOWN,
        )
        outcome = decide(inputs, "strong", NOW, None, long_lived=False, last_refreshed_at=None)
        assert outcome.branch == "P5_open_recent_strong"
        assert outcome.recommended_action == "apply_now"


def test_evidence_quality_does_not_read_the_event_inputs() -> None:
    """`rli.policy.quality` scores the POSTING's evidence, not the company's.

    Asserted rather than assumed, because if the verdict did move with these
    inputs then deferring them to the probe would silently change every
    system's `evidence_quality` distribution (spec.md §6) as a side effect.
    """
    evidence: list[EvidenceItem] = []
    known = _open_recent_inputs(
        material_negative_event=False, freeze_or_pause=False, last_material_event_at=None
    )
    unknown = _open_recent_inputs()

    assert (
        evidence_quality_detail(evidence, known, [], None).quality
        == evidence_quality_detail(evidence, unknown, [], None).quality
    )
