"""Regression tests for the 2026-09-21 `rli history rebuild` OOM incident.

Two properties are pinned here, on a synthetic corpus big enough that the
per-company and whole-corpus code paths can actually disagree:

1. **Equivalence.** The rebuild now processes one company at a time and
   keeps only the candidates that pass the thresholds. That is a memory
   change, not a behaviour change, and the tests below assert it against a
   REFERENCE IMPLEMENTATION of the pre-fix algorithm (`_reference_ranking`)
   — every interval built at once, every within-company pair scored through
   `score_pair`, one global sort, one global greedy assignment.
2. **Atomicity.** `rebuild` clears a company's derived state before it
   rewrites it. The incident's damage was not the OOM itself but that the
   kill landed in that window and left `repost_links` empty across the whole
   corpus. A company is now cleared and rewritten inside one transaction, so
   an interruption leaves every company either fully old or fully new.

The corpus deliberately includes the shapes that make the two paths able to
diverge: several companies, REUSED job ids across companies (so a
company-blind key would collide), assignment contention (many
same-title postings closing and reopening together), coexisting pairs,
same-url versions, junk titles, and candidates just inside and just outside
both ends of the gap window.
"""

from __future__ import annotations

import sqlite3

import pytest
from test_history_helpers import add_capture, add_posting, at, job

from rli.config import Config
from rli.history.cli import rebuild
from rli.history.closures import build_intervals
from rli.history.matching import (
    MatchCandidate,
    _score_company,
    assign_one_to_one,
    link_reposts,
    rank_matches,
    score_pair,
)
from rli.models.time import to_utc_z

# Companies are visited in `company_id` order, which these names make
# explicit — the interruption test depends on knowing that order.
COMPANIES = ("aaa.com", "bbb.com", "ccc.com", "ddd.com")

TEAMS = ("Infrastructure", "Payments", "Growth")
LOCATIONS = ("San Francisco, CA", "Remote (US)", "Berlin, DE")
ROLES = (
    "Senior Backend Engineer",
    "Staff Data Engineer",
    "Product Designer",
    "Engineering Manager, Payments",
)


def _posting_facts(index: int) -> dict[str, str]:
    """The columns `postings` has (no description hash lives there)."""
    return {
        "title": ROLES[index % len(ROLES)],
        "team": TEAMS[index % len(TEAMS)],
        "location": LOCATIONS[index % len(LOCATIONS)],
    }


def _facts(index: int) -> dict[str, str]:
    return {
        **_posting_facts(index),
        "description_hash": f"sha256:{index % len(ROLES):04d}",
    }


def _listing(job_id: str, index: int, *, url: str | None = None):
    return job(job_id, url=url, **_facts(index))


def _tenant(company_id: str) -> str:
    """A distinct ATS tenant per company.

    `posting_id` is `"{ats}:{tenant}:{job_id}"`, and these companies reuse
    job ids on purpose, so without this they would collide on the primary
    key instead of exercising the company-scoped keys under test.
    """
    return company_id.split(".")[0]


def _add_posting(conn: sqlite3.Connection, *, job_id: str, company_id: str, **facts: str) -> str:
    return add_posting(
        conn, job_id=job_id, company_id=company_id, tenant=_tenant(company_id), **facts
    )


def _build_company(conn: sqlite3.Connection, company_id: str, *, roles: int = 6) -> None:
    """One company's history: `roles` postings that close and are reposted.

    The job ids are the SAME in every company (`old0`, `new0`, ...), which is
    what a company-blind assignment key would get wrong.
    """
    for index in range(roles):
        _add_posting(conn, job_id=f"old{index}", company_id=company_id, **_posting_facts(index))
        _add_posting(conn, job_id=f"new{index}", company_id=company_id, **_posting_facts(index))

    # Day 0 and 1: every "old" posting is open, and they are all listed in
    # the same captures, so each is a coexisting role of every other.
    open_jobs = [_listing(f"old{i}", i) for i in range(roles)]
    add_capture(conn, at(0), open_jobs, company_id=company_id)
    add_capture(conn, at(1), open_jobs, company_id=company_id)
    # Day 2: all gone. Day 4: every role comes back under a new id, so the
    # one-to-one assignment has `roles`^2 mutually plausible pairings to
    # resolve (identical titles repeat every len(ROLES) roles).
    add_capture(conn, at(2), [], company_id=company_id)
    add_capture(
        conn,
        at(4),
        [_listing(f"new{i}", i) for i in range(roles)],
        company_id=company_id,
    )

    # A version: same job id across two captures with a changed hash, plus a
    # second job id sharing its canonical url. Neither may ever be linked.
    _add_posting(conn, job_id="ver1", company_id=company_id, title="Solutions Architect")
    _add_posting(conn, job_id="ver2", company_id=company_id, title="Solutions Architect")
    shared_url = f"https://{company_id}/jobs/solutions-architect/"
    add_capture(
        conn,
        at(5),
        [job("ver1", title="Solutions Architect", url=shared_url)],
        company_id=company_id,
    )
    add_capture(conn, at(6), [], company_id=company_id)
    add_capture(
        conn,
        at(7),
        [job("ver2", title="Solutions Architect", url=shared_url)],
        company_id=company_id,
    )

    # Junk titles: a scraped page of "Apply" links. Gate (7) must drop both
    # sides, so these never reach the scorer however well they would score.
    _add_posting(conn, job_id="junk1", company_id=company_id, title="Apply")
    _add_posting(conn, job_id="junk2", company_id=company_id, title="Apply")
    add_capture(conn, at(8), [job("junk1", title="Apply")], company_id=company_id)
    add_capture(conn, at(9), [], company_id=company_id)
    add_capture(conn, at(10), [job("junk2", title="Apply")], company_id=company_id)

    # Just outside the upper end of the gap window (max_gap_days = 120) and
    # just inside it, for the same closed role.
    _add_posting(conn, job_id="far1", company_id=company_id, title="Release Engineer")
    _add_posting(conn, job_id="far2", company_id=company_id, title="Release Engineer")
    add_capture(conn, at(11), [job("far1", title="Release Engineer")], company_id=company_id)
    add_capture(conn, at(12), [], company_id=company_id)
    add_capture(conn, at(400), [job("far2", title="Release Engineer")], company_id=company_id)


@pytest.fixture
def corpus(conn: sqlite3.Connection) -> sqlite3.Connection:
    for company_id in COMPANIES:
        _build_company(conn, company_id)
    return conn


# ---------------------------------------------------------------------------
# The pre-fix algorithm, kept here as the thing the fix must reproduce
# ---------------------------------------------------------------------------


def _reference_ranking(conn: sqlite3.Connection, cfg: Config) -> list[MatchCandidate]:
    """The whole-corpus ranking exactly as it was computed before the fix.

    Intentionally the naive version: build every interval in the database,
    score every within-company pair through the public `score_pair`, sort
    the lot once, and assign greedily over that single global list. If the
    memory-bounded path ever disagrees with this, the fix changed results.
    """
    intervals = build_intervals(conn, None)

    by_company: dict[str, list] = {}
    for interval in intervals:
        by_company.setdefault(interval.company_id, []).append(interval)

    candidates: list[MatchCandidate] = []
    for company_intervals in by_company.values():
        disappeared = [i for i in company_intervals if i.first_seen_absent is not None]
        for old in disappeared:
            for new in company_intervals:
                scored = score_pair(old, new, cfg)
                if scored is not None:
                    candidates.append(scored)

    candidates.sort(key=lambda c: (-c.combined, c.old_job_id, c.new_job_id))
    return assign_one_to_one(candidates)


def _identity(candidate: MatchCandidate) -> tuple[str, str, str]:
    """A candidate's identity, company included — unique across the corpus."""
    return (candidate.company_id, candidate.old_job_id, candidate.new_job_id)


def _as_multiset(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    return sorted(candidates, key=_identity)


def test_the_corpus_actually_exercises_the_interesting_cases(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    """Guard the guard: a corpus that scores nothing would pass everything."""
    reference = _reference_ranking(corpus, cfg)

    assert len(reference) > 100, "not enough scored pairs to be a meaningful test"
    assert sum(1 for c in reference if c.is_match) >= len(COMPANIES)
    assert any(c.reject_reason == "assignment" for c in reference), "no contention"
    assert any(c.reject_reason == "thresholds" for c in reference), "nothing near-misses"
    assert {c.company_id for c in reference} == set(COMPANIES)
    # The gates really did drop the pairs they are supposed to drop.
    job_ids = {c.old_job_id for c in reference} | {c.new_job_id for c in reference}
    pairs = {(c.old_job_id, c.new_job_id) for c in reference}
    # Gate (7) drops a junk-titled interval outright, on either side.
    assert not job_ids & {"junk1", "junk2"}, "junk titles were scored"
    # Gate (2) drops only the same-url PAIR; `ver1` may still be scored
    # against unrelated jobs, and is — which is what makes this a real test
    # of the gate rather than of the corpus.
    assert "ver1" in job_ids
    assert ("ver1", "ver2") not in pairs, "a same-url version was scored"
    # Gate (5): `far2` appears 388 days after `far1` closed (max_gap_days=120).
    assert "far1" in job_ids
    assert "far2" not in job_ids, "a candidate beyond max_gap_days was scored"


def test_bounded_ranking_equals_the_whole_corpus_reference(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    """`rank_matches` is unchanged by processing one company at a time."""
    reference = _reference_ranking(corpus, cfg)
    actual = rank_matches(corpus, cfg)

    assert _as_multiset(actual) == _as_multiset(reference)
    # ... and in the same rank order. Compared on scores rather than on the
    # list itself because two candidates of DIFFERENT companies may tie on
    # the whole sort key (the job ids repeat across companies), and which of
    # those two comes first is arbitrary in both implementations.
    assert [c.combined for c in actual] == [c.combined for c in reference]


def test_matches_only_equals_filtering_the_full_ranking(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    """Dropping threshold-rejected candidates early cannot change a verdict.

    `assign_one_to_one` never marks a side used for a candidate that failed
    the thresholds, so those candidates are invisible to every other
    candidate's decision — which is what makes the memory saving exact.
    """
    reference = [c for c in _reference_ranking(corpus, cfg) if c.is_match]
    actual = rank_matches(corpus, cfg, matches_only=True)

    assert _as_multiset(actual) == _as_multiset(reference)
    assert [c.combined for c in actual] == [c.combined for c in reference]


@pytest.mark.parametrize("top_n", [0, 1, 5, 25, 10_000])
def test_top_n_equals_slicing_the_full_ranking(
    corpus: sqlite3.Connection, cfg: Config, top_n: int
) -> None:
    """`top_n` bounds memory without changing which rows come back."""
    expected = rank_matches(corpus, cfg)[:top_n]
    actual = rank_matches(corpus, cfg, top_n=top_n)

    assert [c.combined for c in actual] == [c.combined for c in expected]
    assert _as_multiset(actual) == _as_multiset(expected)


def test_top_n_with_matches_only_equals_slicing_the_matches(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    expected = rank_matches(corpus, cfg, matches_only=True)[:3]
    actual = rank_matches(corpus, cfg, matches_only=True, top_n=3)

    assert _as_multiset(actual) == _as_multiset(expected)


def test_scored_count_covers_discarded_candidates(corpus: sqlite3.Connection, cfg: Config) -> None:
    """The run summary still counts every pair the gates admitted.

    Retaining only the passing candidates must not quietly turn
    `scored=1614904` into `scored=5910` in the rebuild's output — the
    counter is how a run is audited.
    """
    reference = _reference_ranking(corpus, cfg)
    for company_id in COMPANIES:
        intervals = build_intervals(corpus, company_id)
        expected = [c for c in reference if c.company_id == company_id]

        kept, scored = _score_company(intervals, cfg, passing_only=True)

        assert scored == len(expected)
        # Identity only: `_score_company` returns candidates before the
        # assignment runs, so `is_match` / `reject_reason` are still
        # provisional here while the reference's are final.
        assert {_identity(c) for c in kept} == {
            _identity(c) for c in expected if c.passes_thresholds
        }
        assert all(c.passes_thresholds for c in kept)
        by_identity = {_identity(c): c for c in expected}
        for candidate in kept:
            assert candidate.combined == by_identity[_identity(candidate)].combined
            assert candidate.components == by_identity[_identity(candidate)].components


def test_the_consuming_assignment_matches_the_public_one(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    """`_assign_consuming` differs from `assign_one_to_one` only in memory.

    It empties the list it is given, which the public function must NOT do —
    they are one keystroke apart and a caller that reused its input after
    calling the wrong one would silently see an empty ranking.
    """
    from rli.history.matching import _assign_consuming, _rank_key

    candidates, _ = _score_company(build_intervals(corpus, "aaa.com"), cfg)
    candidates.sort(key=_rank_key)
    assert candidates, "nothing to assign"

    public_input = list(candidates)
    public = assign_one_to_one(public_input)
    consuming_input = list(candidates)
    consuming = _assign_consuming(consuming_input)

    assert consuming == public
    assert public_input == candidates, "assign_one_to_one must not mutate its input"
    assert consuming_input == [], "_assign_consuming must release its input"


def test_the_coexistence_bitmask_agrees_with_the_snapshot_id_sets(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    """Gate (6)'s fast path and its plain-language definition never differ.

    `_score_company` compares presence as a bitmask over a per-comparison
    registry of snapshot ids; `PostingInterval.coexists_with` intersects the
    id tuples directly. They must agree for every pair in the corpus.
    """
    from rli.history.matching import _coexists, _prepare_interval

    for company_id in COMPANIES:
        intervals = build_intervals(corpus, company_id)
        bits: dict[int, int] = {}
        rows = [_prepare_interval(i, cfg.matching, bits) for i in intervals]
        prepared = [r for r in rows if r is not None]
        assert len(prepared) >= 2

        for left in prepared:
            for right in prepared:
                assert _coexists(left, right) == left.interval.coexists_with(right.interval)


# ---------------------------------------------------------------------------
# What actually gets written
# ---------------------------------------------------------------------------


def _links(conn: sqlite3.Connection) -> set[tuple[str, str, str, float]]:
    return {
        tuple(row)
        for row in conn.execute(
            """
            SELECT company_id, old_posting_id, new_posting_id, combined_score
            FROM repost_links
            """
        )
    }


def _expected_links(conn: sqlite3.Connection, cfg: Config) -> set[tuple[str, str, str, float]]:
    return {
        (c.company_id, c.old_posting_id, c.new_posting_id, c.combined)
        for c in _reference_ranking(conn, cfg)
        if c.is_match and c.old_posting_id is not None and c.new_posting_id is not None
    }


def test_rebuild_writes_exactly_the_reference_links(
    corpus: sqlite3.Connection, cfg: Config
) -> None:
    rebuild(corpus)

    assert _links(corpus) == _expected_links(corpus, cfg)
    assert _links(corpus), "the corpus produced no links at all"


def test_rebuild_is_a_fixed_point_on_a_multi_company_corpus(
    corpus: sqlite3.Connection,
) -> None:
    """Per-company transactions must not cost `rebuild` its idempotence."""
    first = rebuild(corpus)
    state = _links(corpus)
    postings = [
        tuple(row)
        for row in corpus.execute(
            "SELECT posting_id, replacement_job_id, reappeared_at FROM postings ORDER BY posting_id"
        )
    ]

    second = rebuild(corpus)

    assert _links(corpus) == state
    assert [
        tuple(row)
        for row in corpus.execute(
            "SELECT posting_id, replacement_job_id, reappeared_at FROM postings ORDER BY posting_id"
        )
    ] == postings
    # Same links written, same decisions. (The closure summary's "how many
    # columns moved" counters legitimately differ: the first run tightened
    # lifecycle columns that the second finds already tight.)
    assert second[0] == first[3].replacements_set
    assert second[1] == first[3].links_written
    assert second[3].describe() == first[3].describe()


def test_link_reposts_alone_matches_the_reference(corpus: sqlite3.Connection, cfg: Config) -> None:
    """`link_reposts` is company-scoped internally; its result is not."""
    from rli.history.closures import apply_to_postings

    apply_to_postings(corpus)
    summary = link_reposts(corpus, cfg)

    assert _links(corpus) == _expected_links(corpus, cfg)
    assert summary.candidates_scored == len(_reference_ranking(corpus, cfg))


# ---------------------------------------------------------------------------
# Atomicity: an interrupted rebuild never leaves a company empty
# ---------------------------------------------------------------------------


def _company_state(conn: sqlite3.Connection, company_id: str) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT old_posting_id, new_posting_id, combined_score, matched_at
            FROM repost_links WHERE company_id = ?
            ORDER BY old_posting_id, new_posting_id
            """,
            (company_id,),
        )
    ]


def _replacements(conn: sqlite3.Connection, company_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM postings WHERE company_id = ? AND replacement_job_id IS NOT NULL",
        (company_id,),
    ).fetchone()[0]


def test_an_exception_mid_rebuild_leaves_every_company_whole(
    corpus: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, in miniature.

    Companies are visited in `company_id` order. `bbb.com` blows up AFTER
    its derived state has been cleared and while it is being rewritten —
    precisely where the OOM kill landed on 2026-09-21. Afterwards:

    * `aaa.com`, which committed before the failure, holds its NEW links;
    * `bbb.com` holds its OLD links, untouched — not an empty table;
    * `ccc.com` / `ddd.com`, never reached, hold their old links too.
    """
    import rli.history.cli as cli

    first_run = at(100)
    rebuild(corpus, now=first_run)
    before = {company_id: _company_state(corpus, company_id) for company_id in COMPANIES}
    assert all(before[company_id] for company_id in COMPANIES)

    # Make the old rows distinguishable from anything a rewrite would
    # produce, so "the old links survived" cannot be confused with "the new
    # links happen to be identical".
    corpus.execute(
        "UPDATE repost_links SET combined_score = 0.125 WHERE company_id = ?", ("bbb.com",)
    )
    corpus.commit()
    tampered = _company_state(corpus, "bbb.com")
    assert all(row[2] == 0.125 for row in tampered)

    real_link_reposts = cli.link_reposts

    def exploding_link_reposts(conn, cfg, company_id=None, **kwargs):
        if company_id == "bbb.com":
            raise RuntimeError("simulated OOM kill mid-rewrite")
        return real_link_reposts(conn, cfg, company_id, **kwargs)

    monkeypatch.setattr(cli, "link_reposts", exploding_link_reposts)

    second_run = at(200)
    with pytest.raises(RuntimeError, match="simulated OOM"):
        rebuild(corpus, now=second_run)

    # aaa.com committed before the failure: re-derived, same links, new run.
    assert [row[:3] for row in _company_state(corpus, "aaa.com")] == [
        row[:3] for row in before["aaa.com"]
    ]
    assert {row[3] for row in _company_state(corpus, "aaa.com")} == {to_utc_z(second_run)}
    assert _replacements(corpus, "aaa.com") > 0

    # bbb.com was cleared and then failed: the clear was rolled back.
    assert _company_state(corpus, "bbb.com") == tampered
    assert _replacements(corpus, "bbb.com") > 0

    # The companies after it were never touched.
    for company_id in ("ccc.com", "ddd.com"):
        assert _company_state(corpus, company_id) == before[company_id]
        assert _replacements(corpus, company_id) > 0


def test_the_failed_company_is_repaired_by_the_next_rebuild(
    corpus: sqlite3.Connection, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted rebuild leaves the database rebuildable, not wedged."""
    import rli.history.cli as cli

    rebuild(corpus, now=at(100))
    real_link_reposts = cli.link_reposts

    def exploding_link_reposts(conn, cfg, company_id=None, **kwargs):
        if company_id == "bbb.com":
            raise RuntimeError("simulated OOM kill mid-rewrite")
        return real_link_reposts(conn, cfg, company_id, **kwargs)

    monkeypatch.setattr(cli, "link_reposts", exploding_link_reposts)
    with pytest.raises(RuntimeError):
        rebuild(corpus, now=at(200))

    monkeypatch.undo()
    rebuild(corpus, now=at(300))

    assert _links(corpus) == _expected_links(corpus, cfg)


def test_a_failed_rebuild_leaves_the_connection_usable(
    corpus: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-company transaction must not leak an open transaction or a
    borrowed `isolation_level` when it unwinds."""
    import rli.history.cli as cli

    isolation_before = corpus.isolation_level

    def exploding_link_reposts(conn, cfg, company_id=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "link_reposts", exploding_link_reposts)
    with pytest.raises(RuntimeError):
        rebuild(corpus)

    assert corpus.isolation_level == isolation_before
    assert corpus.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # No transaction is left open, so an ordinary write still works.
    add_posting(corpus, job_id="afterwards", company_id="aaa.com", title="Later Role")
    assert corpus.execute("SELECT COUNT(*) FROM postings").fetchone()[0] > 0


def test_company_scoped_rebuild_still_touches_only_that_company(
    corpus: sqlite3.Connection,
) -> None:
    rebuild(corpus, now=at(100))
    others = {c: _company_state(corpus, c) for c in COMPANIES if c != "ccc.com"}

    rebuild(corpus, "ccc.com", now=at(200))

    assert {row[3] for row in _company_state(corpus, "ccc.com")} == {to_utc_z(at(200))}
    for company_id, state in others.items():
        assert _company_state(corpus, company_id) == state
