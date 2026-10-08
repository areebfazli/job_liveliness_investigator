"""Analysis for the archived job-page pilot (see rli/pilot/wayback_pages.py).

Reads `data/rli.db` READ-ONLY and the pilot DB. Produces one JSON-able dict
with four parts:

* `validation` — do the page parsers / CDX-status inferences agree with
  our own board captures and our own ATS dates where both exist?
* `coverage` — per ATS / company: how many of our postings gain archived
  page observations, publish dates, closures; replay cases that would gain a
  first-publish date available at T.
* `signal` — point-in-time closure prediction at monthly cut dates:
  age-only vs age + company habit + repost + team activity, with grouped
  (by company) cross-validation, DB-only vs DB + archived pages.
* `bias` — postings with vs without archived page captures.

Point-in-time rules: every feature at cut `c` uses observations with
`time <= c` only (an archived observation is available at its capture time,
an own date at the capture that carried it). Labels use observations after
`c`. A posting is in the sample at `c` only if its latest observation at or
before `c` is OPEN and no older than `KNOWN_OPEN_LOOKBACK_DAYS`.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from rli.history.closures import load_captures
from rli.models.time import parse_utc
from rli.pilot.wayback_pages import infer_from_cdx

HORIZON_DAYS = 60
KNOWN_OPEN_LOOKBACK_DAYS = 31
OWN_ERA_START = datetime(2026, 9, 7, tzinfo=UTC)
CUTS = [
    datetime(y, m, 1, tzinfo=UTC)
    for y, m in [
        (2025, 11),
        (2025, 12),
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
        (2026, 5),
        (2026, 6),
        (2026, 7),
        (2026, 8),
    ]
]
DATASETS = ("dev-7d-v4", "company-7d-v4")


def _norm_team(team: str | None) -> str | None:
    if not team:
        return None
    t = " ".join(team.lower().replace("&", "and").split())
    return t or None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class Posting:
    key: str
    company_id: str
    ats: str
    job_id: str
    in_db: bool
    team: str | None = None
    title: str | None = None
    db_first_observed: datetime | None = None
    own_first: datetime | None = None
    db_first_seen_absent: datetime | None = None
    # (time, is_open) observations
    db_obs: list[tuple[datetime, bool]] = field(default_factory=list)
    page_obs: list[tuple[datetime, bool]] = field(default_factory=list)
    # (available_at, date, kind)
    db_pub: list[tuple[datetime, datetime, str]] = field(default_factory=list)
    page_pub: list[tuple[datetime, datetime, str]] = field(default_factory=list)
    page_captures: int = 0
    repost_old: list[str] = field(default_factory=list)


def validated_rules(
    pilot: sqlite3.Connection,
    *,
    min_n: int = 15,
    min_n_company: int = 6,
    min_agree: float = 0.95,
) -> tuple[set[str], list[dict]]:
    """CDX inference rules whose fetched-sample agreement clears the bar.

    A rule is validated globally (`rule`, n >= `min_n`) or for one company
    only (`rule@company`, n >= `min_n_company`): a Greenhouse 302 means
    "closed" on most boards but "go to our careers site" on boards that
    redirect open jobs to a custom domain, so it is judged per board.
    """
    rows = pilot.execute(
        """SELECT c.company_id, c.ats, c.host, c.page_kind, c.statuscode, c.length, o.state
        FROM captures c JOIN page_obs o ON o.capture_id = c.id WHERE o.evidence = 'fetched'"""
    ).fetchall()
    tally: dict[str, Counter] = defaultdict(Counter)
    inferred_state: dict[str, str] = {}
    for r in rows:
        state, rule = infer_from_cdx(
            r["ats"], r["host"], r["page_kind"], r["statuscode"], r["length"]
        )
        for key in (rule, f"{rule}@{r['company_id']}"):
            inferred_state[key] = state
            tally[key][r["state"]] += 1
    out, table = set(), []
    for rule, cnt in sorted(tally.items()):
        n = sum(cnt.values())
        st = inferred_state[rule]
        agree = cnt[st] / n if n else 0.0
        need = min_n_company if "@" in rule else min_n
        ok = st != "unknown" and n >= need and agree >= min_agree
        if ok:
            out.add(rule)
        if "@" in rule and st == "unknown":
            continue  # keep the table readable
        table.append(
            {
                "rule": rule,
                "inferred": st,
                "n_fetched": n,
                "parsed": dict(cnt),
                "agreement": round(agree, 3),
                "used": ok,
            }
        )
    return out, table


def load_postings(
    main: sqlite3.Connection, pilot: sqlite3.Connection, log: Callable[[str], None] = print
) -> dict[str, Posting]:
    targets = pilot.execute("SELECT * FROM targets ORDER BY company_id").fetchall()
    rules, _ = validated_rules(pilot)
    postings: dict[str, Posting] = {}
    for t in targets:
        cid = t["company_id"]
        by_job: dict[str, Posting] = {}
        rows = main.execute(
            """SELECT posting_id, ats, ats_job_id, team, title, first_observed,
            first_seen_absent FROM postings WHERE company_id = ? AND ats_job_id IS NOT NULL""",
            (cid,),
        ).fetchall()
        for r in sorted(
            rows, key=lambda r: (r["posting_id"].startswith("archive:"), r["posting_id"])
        ):
            jid = str(r["ats_job_id"]).lower()
            if jid in by_job:
                continue
            p = Posting(
                key=r["posting_id"],
                company_id=cid,
                ats=t["ats"],
                job_id=jid,
                in_db=True,
                team=_norm_team(r["team"]),
                title=r["title"],
                db_first_observed=_dt(r["first_observed"]),
                db_first_seen_absent=_dt(r["first_seen_absent"]),
            )
            by_job[jid] = p
        # Board captures (own + archived boards): presence from any capture,
        # absence only from complete captures after the first sighting.
        captures = load_captures(main, cid)
        first_seen: dict[str, datetime] = {}
        for cap in captures:
            present = {j.lower() for j in cap.jobs}
            for jid in present:
                p = by_job.get(jid)
                if p is None:
                    continue
                p.db_obs.append((cap.captured_at, True))
                first_seen.setdefault(jid, cap.captured_at)
                if cap.source == "own" and p.own_first is None:
                    p.own_first = cap.captured_at
            if cap.is_complete:
                for jid in first_seen:
                    if jid not in present:
                        by_job[jid].db_obs.append((cap.captured_at, False))
        # Own dated captures (GH first_published / Ashby last_published, since 2026-10-03).
        for r in main.execute(
            """SELECT j.job_id, j.first_published, j.last_published, MIN(s.captured_at) AS av
            FROM board_snapshot_jobs j JOIN board_snapshots s ON s.id = j.board_snapshot_id
            WHERE s.company_id = ? AND s.source = 'own'
              AND (j.first_published IS NOT NULL OR j.last_published IS NOT NULL)
            GROUP BY j.job_id, j.first_published, j.last_published""",
            (cid,),
        ):
            p = by_job.get(str(r["job_id"]).lower())
            if p is None:
                continue
            av = _dt(r["av"])
            if r["first_published"] and av:
                p.db_pub.append((av, _dt(r["first_published"]), "own_first_published"))
            if r["last_published"] and av:
                p.db_pub.append((av, _dt(r["last_published"]), "own_last_published"))
        for r in main.execute(
            """SELECT d.posting_id, d.date_posted, d.fetched_at FROM posting_page_dates d
            JOIN postings p ON p.posting_id = d.posting_id
            WHERE p.company_id = ? AND d.status = 'ok'""",
            (cid,),
        ):
            for p in by_job.values():
                if p.key == r["posting_id"] and r["date_posted"] and r["fetched_at"]:
                    p.db_pub.append(
                        (_dt(r["fetched_at"]), _dt(r["date_posted"]), "own_lever_date_posted")
                    )
        # Archived job pages.
        for r in pilot.execute(
            """SELECT o.*, c.page_kind FROM page_obs o JOIN captures c ON c.id = o.capture_id
            WHERE o.company_id = ?""",
            (cid,),
        ):
            jid = r["job_id"].lower()
            p = by_job.get(jid)
            if p is None:
                p = Posting(
                    key=f"page:{cid}:{jid}",
                    company_id=cid,
                    ats=t["ats"],
                    job_id=jid,
                    in_db=False,
                    title=r["title"],
                )
                by_job[jid] = p
            p.page_captures += 1
            at = _dt(r["capture_at"])
            usable = r["evidence"] == "fetched" or (
                r["evidence"] == "cdx_inferred"
                and (r["parser"] in rules or f"{r['parser']}@{cid}" in rules)
            )
            if usable and r["state"] in ("open", "closed"):
                p.page_obs.append((at, r["state"] == "open"))
            if r["published_at"]:
                kind = {
                    "greenhouse": "page_gh_first_published",
                    "lever": "page_lever_date_posted",
                    "ashby": "page_ashby_last_published",
                }[t["ats"]]
                p.page_pub.append((at, _dt(r["published_at"]), kind))
            if p.title is None and r["title"]:
                p.title = r["title"]
        for p in by_job.values():
            p.db_obs.sort()
            p.page_obs.sort()
            p.db_pub.sort()
            p.page_pub.sort()
            postings[p.key] = p
        log(f"loaded {cid}: {len(by_job)} jobs")
    # Repost links (both endpoints in scope).
    keys = set(postings)
    for r in main.execute("SELECT old_posting_id, new_posting_id FROM repost_links"):
        if r["new_posting_id"] in keys and r["old_posting_id"] in keys:
            postings[r["new_posting_id"]].repost_old.append(r["old_posting_id"])
    return postings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _own_state_at(p: Posting, t: datetime, tol_days: float = 1.5) -> str | None:
    """Our own board captures' verdict at time t: bracketing own observations agree."""
    own = [(at, op) for at, op in p.db_obs if at >= OWN_ERA_START]
    if not own:
        return None
    times = [at for at, _ in own]
    i = bisect_right(times, t)
    before = own[i - 1] if i > 0 else None
    after = own[i] if i < len(own) else None
    tol = timedelta(days=tol_days)
    if before and after and t - before[0] <= tol and after[0] - t <= tol:
        if before[1] and after[1]:
            return "open"
        if not before[1] and not after[1]:
            return "closed"
        return None
    if before and t - before[0] <= tol and before[1] is False:
        return "closed"
    if after and after[0] - t <= tol and after[1] is True and before is not None and before[1]:
        return "open"
    return None


def validation(pilot: sqlite3.Connection, postings: dict[str, Posting]) -> dict:
    rules, rule_table = validated_rules(pilot)
    by_key = postings
    # page state vs own state (own era)
    agree: dict[str, Counter] = defaultdict(Counter)
    for r in pilot.execute(
        """SELECT o.*, c.page_kind FROM page_obs o JOIN captures c ON c.id = o.capture_id
        WHERE o.posting_id IS NOT NULL AND o.capture_at >= '2026-09-07'"""
    ):
        p = by_key.get(r["posting_id"])
        if p is None:
            continue
        own = _own_state_at(p, _dt(r["capture_at"]))
        if own is None:
            continue
        label = f"{r['ats']}:{r['evidence']}:{r['parser']}"
        agree[label][f"page={r['state']}/own={own}"] += 1
    # publish date agreement
    gh_diff, lever_diff, ashby = [], [], Counter()
    for p in postings.values():
        own_fp = [d for _, d, k in p.db_pub if k == "own_first_published"]
        own_lp = [d for _, d, k in p.db_pub if k == "own_last_published"]
        own_lv = [d for _, d, k in p.db_pub if k == "own_lever_date_posted"]
        for _, d, k in p.page_pub:
            if k == "page_gh_first_published" and own_fp:
                gh_diff.append(abs((d - own_fp[0]).total_seconds()) / 86400)
            if k == "page_lever_date_posted" and own_lv:
                lever_diff.append(abs((d - own_lv[0]).total_seconds()) / 86400)
            if k == "page_ashby_last_published" and own_lp:
                same = any(abs((d - x).total_seconds()) < 86400 * 1.01 for x in own_lp)
                ashby["match_any_own_last_published_1d" if same else "differs"] += 1

    def _summ(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "exact_<=1min": sum(x <= 1 / 1440 for x in xs),
            "within_1d": sum(x <= 1.0 for x in xs),
            "max_days": round(max(xs), 2),
        }

    return {
        "cdx_rules": rule_table,
        "page_vs_own_state": {k: dict(v) for k, v in sorted(agree.items())},
        "gh_published_at_vs_own_first_published": _summ(gh_diff),
        "lever_date_posted_vs_own_page_date": _summ(lever_diff),
        "ashby_published_date_vs_own_last_published": dict(ashby),
        "validated_rules": sorted(rules),
    }


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

WINDOW_START = datetime(2025, 10, 1, tzinfo=UTC)


def coverage(postings: dict[str, Posting]) -> dict:
    groups: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for p in postings.values():
        alive = p.db_obs and max(t for t, op in p.db_obs if op) >= WINDOW_START if p.in_db else True
        if p.in_db and not any(op for _, op in p.db_obs):
            alive = False
        for g in [(p.ats, "ALL"), (p.ats, p.company_id), ("ALL", "ALL")]:
            c = groups[g]
            if not p.in_db:
                c["page_only_jobs"] += 1
                if p.page_pub:
                    c["page_only_with_date"] += 1
                continue
            if not alive:
                continue
            c["postings"] += 1
            c["own_seen"] += p.own_first is not None
            if p.page_captures:
                c["any_capture"] += 1
            if p.page_obs:
                c["usable_obs"] += 1
            first_db = min((t for t, op in p.db_obs if op), default=None)
            first_page = min((t for t, _ in p.page_obs), default=None)
            if first_page and first_db and first_page < first_db:
                c["page_before_db_first_sighting"] += 1
            if first_page and p.own_first and first_page < p.own_first:
                c["page_before_own_first"] += 1
            if p.page_pub:
                c["publish_date"] += 1
                if any(k != "page_ashby_last_published" for *_, k in p.page_pub):
                    c["first_publish_date"] += 1
            if any(not op for _, op in p.page_obs):
                c["page_closed_obs"] += 1
            # closure the DB lacks: page closed obs and DB never saw it absent
            if any(not op for _, op in p.page_obs) and not any(not op for _, op in p.db_obs):
                c["closure_new_vs_db"] += 1
    out = []
    for (ats, comp), c in sorted(groups.items()):
        row = {"ats": ats, "company": comp, **c}
        n = c["postings"] or 1
        for k in (
            "any_capture",
            "usable_obs",
            "page_before_db_first_sighting",
            "page_before_own_first",
            "publish_date",
            "first_publish_date",
            "page_closed_obs",
            "closure_new_vs_db",
        ):
            row[f"{k}_pct"] = round(100 * c[k] / n, 1)
        out.append(row)
    return {"table": out}


def replay_gain(
    main: sqlite3.Connection, postings: dict[str, Posting], log: Callable[[str], None] = print
) -> dict:
    """Cases in the v4 datasets (target companies) that would gain a first-publish date at T."""
    from rli.models.evidence import EvidenceItem
    from rli.policy.action import decide
    from rli.policy.inputs import (
        CLAIM_FIRST_PUBLISHED,
        UNKNOWN,
        best_publish_claim,
        derive_policy_inputs,
        last_publish_or_refresh,
    )
    from rli.policy.quality import evidence_quality

    companies = {p.company_id for p in postings.values()}
    ph = ",".join("?" for _ in companies)
    res: dict = {}
    for ds in DATASETS:
        cases = main.execute(
            f"""SELECT c.posting_id, c.replay_at, c.company_id,
                   (SELECT r.final_decision FROM runs r WHERE r.posting_id = c.posting_id
                     AND r.replay_at = c.replay_at AND r.system = 'A' AND r.mode = 'replay'
                     AND r.status = 'completed' ORDER BY r.started_at DESC LIMIT 1) AS fd
            FROM replay_cases c WHERE c.dataset_id = ? AND c.company_id IN ({ph})""",
            (ds, *sorted(companies)),
        ).fetchall()
        c = Counter()
        flips = Counter()
        for row in cases:
            c["cases"] += 1
            p = postings.get(row["posting_id"])
            T = _dt(row["replay_at"])
            if not row["fd"]:
                c["no_A_run"] += 1
                continue
            fd = json.loads(row["fd"])
            ev = [EvidenceItem(**e) for e in fd.get("evidence", [])]
            quality0 = fd.get("evidence_quality")
            action0 = fd.get("recommended_action")
            c[f"quality_{quality0}"] += 1
            has_primary = best_publish_claim(ev) is not None
            c["has_primary_publish"] += has_primary
            if p is None:
                c["posting_not_loaded"] += 1
                continue
            cands = [(av, d, k) for av, d, k in p.page_pub if av <= T]
            if any(True for _ in cands):
                c["page_date_at_T_any"] += 1
            first_kinds = [x for x in cands if x[2] != "page_ashby_last_published"]
            if first_kinds:
                c["page_first_publish_at_T"] += 1
                if not has_primary:
                    c["gains_first_publish"] += 1
                    if quality0 == "weak":
                        c["weak_gains_first_publish"] += 1
            if not first_kinds or has_primary or quality0 != "weak":
                continue
            # Re-run the frozen policy with one extra claim (features unavailable:
            # repost_pattern / long_lived UNKNOWN). Count only cases whose
            # no-extra-claim recompute reproduces the recorded action.
            av, d, k = min(first_kinds, key=lambda x: (x[1], x[0]))
            if p.db_first_observed and d > p.db_first_observed + timedelta(days=1):
                c["guard_published_after_first_observed"] += 1
                continue
            sq = "ats_native" if k == "page_gh_first_published" else "page_structured"
            extra = EvidenceItem(
                id="eP",
                probe="pilot_archived_page",
                claim_type=CLAIM_FIRST_PUBLISHED,
                value=d.isoformat(),
                source_url="archive",
                source_quality=sq,
                source_event_at=d,
                available_at=av,
                fetched_at=av,
            )

            def run(evs: list) -> tuple[str, str]:
                inputs = derive_policy_inputs(evs, None, T)
                q = evidence_quality(evs, inputs)
                out = decide(
                    inputs, q, T, long_lived=UNKNOWN, last_refreshed_at=last_publish_or_refresh(evs)
                )
                return q, out.recommended_action

            try:
                q_base, a_base = run(ev)
                q_new, a_new = run([*ev, extra])
            except Exception as exc:  # noqa: BLE001 - pilot accounting
                c["recompute_error"] += 1
                log(f"recompute error {row['posting_id']}: {exc}")
                continue
            if a_base != action0 or q_base != quality0:
                c["recompute_not_reproduced"] += 1
                continue
            c["recompute_reproduced"] += 1
            flips[f"{quality0}->{q_new} {action0}->{a_new}"] += 1
        res[ds] = {"counts": dict(c), "policy_rerun": dict(flips)}
    return res


# ---------------------------------------------------------------------------
# Signal test
# ---------------------------------------------------------------------------


@dataclass
class View:
    """One posting's observations under a data variant ('db' or 'arch')."""

    obs: list[tuple[datetime, bool]]
    pubs: list[tuple[datetime, datetime, str]]


def _view(p: Posting, variant: str) -> View:
    if variant == "db":
        return View(p.db_obs, p.db_pub)
    return View(sorted(p.db_obs + p.page_obs), sorted(p.db_pub + p.page_pub))


def _first_sighting(v: View, c: datetime) -> datetime | None:
    for t, op in v.obs:
        if t > c:
            return None
        if op:
            return t
    return None


def _start(v: View, c: datetime) -> datetime | None:
    fs = _first_sighting(v, c)
    pubs = [d for av, d, _ in v.pubs if av <= c and d <= c]
    cands = [x for x in [fs, *pubs] if x is not None]
    return min(cands) if cands else None


def _known_open(v: View, c: datetime) -> bool:
    last = None
    for t, op in v.obs:
        if t > c:
            break
        last = (t, op)
    return bool(last and last[1] and (c - last[0]).days <= KNOWN_OPEN_LOOKBACK_DAYS)


def _label(v: View, c: datetime) -> int | None:
    end = c + timedelta(days=HORIZON_DAYS)
    closed_in = any((not op) and c < t <= end for t, op in v.obs)
    open_after = any(op and t >= end for t, op in v.obs)
    if closed_in and open_after:
        return None  # reappeared: ambiguous
    if closed_in:
        return 1
    if open_after:
        return 0
    return None


def _ttc(v: View, c: datetime) -> float | None:
    """Time-to-close (midpoint of the closure interval) for a closure observed <= c."""
    fs = None
    last_open = None
    for t, op in v.obs:
        if t > c:
            return None
        if op:
            fs = fs or t
            last_open = t
        elif fs is not None and last_open is not None:
            mid = last_open + (t - last_open) / 2
            return (mid - fs).total_seconds() / 86400
    return None


def _republished(p: Posting, v: View, c: datetime) -> bool:
    ash = sorted({d.date() for av, d, k in v.pubs if av <= c and "ashby" in k and "last" in k})
    if len(ash) >= 2:
        return True
    fs = _first_sighting(v, c)
    if fs and any(av <= c and "ashby" in k and d > fs + timedelta(days=2) for av, d, k in v.pubs):
        return True
    return False


FEATURES_FULL = [
    "log_age",
    "co_long_share",
    "co_long_share_missing",
    "co_ttc_log",
    "co_ttc_missing",
    "repost",
    "team_new_30_log",
    "team_missing",
]
FEATURES_AGE = ["log_age"]


def build_samples(postings: dict[str, Posting], variant: str) -> tuple[list[dict], dict]:
    by_company: dict[str, list[Posting]] = defaultdict(list)
    for p in postings.values():
        if variant == "db" and not p.in_db:
            continue
        by_company[p.company_id].append(p)
    views = {p.key: _view(p, variant) for ps in by_company.values() for p in ps}
    rows: list[dict] = []
    acct = Counter()
    for c in CUTS:
        for cid, ps in by_company.items():
            # company habit at c
            eligible = 0
            long_n = 0
            ttcs = []
            starts: dict[str, datetime] = {}
            for q in ps:
                v = views[q.key]
                fs = _first_sighting(v, c)
                st = _start(v, c)
                if st is not None:
                    starts[q.key] = st
                if fs is not None and fs <= c - timedelta(days=90):
                    eligible += 1
                    if any(op and fs + timedelta(days=90) <= t <= c for t, op in v.obs):
                        long_n += 1
                x = _ttc(v, c)
                if x is not None:
                    ttcs.append(x)
            long_share = long_n / eligible if eligible >= 5 else None
            med_ttc = statistics.median(ttcs) if len(ttcs) >= 5 else None
            team_starts: dict[str, list[datetime]] = defaultdict(list)
            for q in ps:
                if q.team and q.key in starts:
                    team_starts[q.team].append(starts[q.key])
            for p in ps:
                v = views[p.key]
                if not _known_open(v, c):
                    continue
                acct["known_open"] += 1
                lab = _label(v, c)
                if lab is None:
                    acct["label_unknown_dropped"] += 1
                    continue
                st = starts.get(p.key)
                if st is None:
                    continue
                age = max(0.0, (c - st).total_seconds() / 86400)
                repost = any(
                    (old := postings.get(o)) is not None
                    and old.db_first_seen_absent is not None
                    and old.db_first_seen_absent <= c
                    for o in p.repost_old
                ) or _republished(p, v, c)
                if p.team:
                    lo = c - timedelta(days=30)
                    n_new = sum(1 for s in team_starts[p.team] if lo < s <= c) - (
                        1 if lo < st <= c else 0
                    )
                    team_log, team_missing = math.log1p(max(0, n_new)), 0
                else:
                    team_log, team_missing = 0.0, 1
                rows.append(
                    {
                        "key": p.key,
                        "company": cid,
                        "ats": p.ats,
                        "cut": c.date().isoformat(),
                        "label": lab,
                        "age_days": age,
                        "log_age": math.log1p(age),
                        "has_pub": int(any(av <= c for av, _, _ in v.pubs)),
                        "co_long_share": long_share if long_share is not None else np.nan,
                        "co_long_share_missing": int(long_share is None),
                        "co_ttc_log": math.log1p(med_ttc) if med_ttc is not None else np.nan,
                        "co_ttc_missing": int(med_ttc is None),
                        "repost": int(repost),
                        "team_new_30_log": team_log,
                        "team_missing": team_missing,
                        "in_db": int(p.in_db),
                        "has_page_capture": int(p.page_captures > 0),
                    }
                )
                acct[f"label_{lab}"] += 1
    return rows, dict(acct)


def _fit_predict(X_tr, y_tr, X_te, kind: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "logit":
        model = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(max_iter=2000, C=1.0),
        )
    else:
        model = HistGradientBoostingClassifier(
            max_depth=3, max_iter=150, learning_rate=0.05, random_state=0
        )
    model.fit(X_tr, y_tr)
    return model.predict_proba(X_te)[:, 1], model


def _oof(
    rows: list[dict], feats: list[str], kind: str, n_splits: int = 5
) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold predictions with GroupKFold by company; returns (pred, fold id)."""
    from sklearn.model_selection import GroupKFold

    X = np.array([[r[f] for f in feats] for r in rows], dtype=float)
    y = np.array([r["label"] for r in rows])
    g = np.array([r["company"] for r in rows])
    pred = np.full(len(rows), np.nan)
    fold = np.zeros(len(rows), dtype=int)
    k = min(n_splits, len(set(g)))
    for i, (tr, te) in enumerate(GroupKFold(n_splits=k).split(X, y, g)):
        fold[te] = i
        if len(set(y[tr])) < 2:
            pred[te] = y[tr].mean()
            continue
        pred[te], _ = _fit_predict(X[tr], y[tr], X[te], kind)
    return pred, fold


def _auc(y, p) -> float | None:
    """Mann-Whitney AUC (ties count half); None when one class is missing."""
    from scipy.stats import rankdata

    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(p)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _group_auc(y: np.ndarray, p: np.ndarray, groups: np.ndarray) -> float | None:
    """Case-weighted mean of per-group AUCs (groups lacking both classes skipped)."""
    total, weight = 0.0, 0
    order = np.argsort(groups, kind="stable")
    gs = groups[order]
    bounds = np.flatnonzero(np.r_[True, gs[1:] != gs[:-1], True])
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        idx = order[a:b]
        val = _auc(y[idx], p[idx])
        if val is not None:
            total += val * len(idx)
            weight += len(idx)
    return total / weight if weight else None


METRICS = ("pooled", "within_fold", "within_company_cut")


def _metric(name: str, y, p, fold, cc) -> float | None:
    if name == "pooled":
        return _auc(y, p)
    if name == "within_fold":
        return _group_auc(y, p, fold)
    return _group_auc(y, p, cc)


def _cluster_boot(
    y, preds: dict[str, np.ndarray], fold, cc, groups, n: int = 500, seed: int = 7
) -> dict:
    """Company-cluster bootstrap CIs for every metric, per model and for full - age."""
    rng = np.random.default_rng(seed)
    comps = np.array(sorted(set(groups)))
    idx_by = {c: np.where(groups == c)[0] for c in comps}
    names = list(preds)
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    for _ in range(n):
        pick = rng.choice(comps, size=len(comps), replace=True)
        # Relabel duplicated companies so their (company, cut) groups stay separate.
        idx = np.concatenate([idx_by[c] for c in pick])
        dup = np.concatenate([np.full(len(idx_by[c]), j) for j, c in enumerate(pick)])
        cc_b = np.array([f"{a}#{b}" for a, b in zip(cc[idx], dup, strict=True)])
        yy = y[idx]
        if len(set(yy)) < 2:
            continue
        for m in METRICS:
            vals = {k: _metric(m, yy, preds[k][idx], fold[idx], cc_b) for k in names}
            for k in names:
                if vals[k] is not None:
                    samples[(m, k)].append(vals[k])
            if vals.get("age") is not None and vals.get("full") is not None:
                samples[(m, "delta")].append(vals["full"] - vals["age"])

    def ci(v: list[float]) -> list[float]:
        return [round(float(np.percentile(v, 2.5)), 3), round(float(np.percentile(v, 97.5)), 3)]

    out: dict = {}
    for (m, k), v in samples.items():
        out.setdefault(m, {})[k] = ci(v)
        if k == "delta":
            out[m]["delta_p_le_0"] = round(float(np.mean(np.array(v) <= 0)), 3)
    return out


def _calibration(y, p, bins: int = 5) -> list[dict]:
    order = np.argsort(p)
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk):
            out.append(
                {
                    "n": int(len(chunk)),
                    "mean_pred": round(float(p[chunk].mean()), 3),
                    "obs_rate": round(float(y[chunk].mean()), 3),
                }
            )
    return out


FEATURES_POSTING = ["log_age", "repost", "team_new_30_log", "team_missing"]
FEATURES_COMPANY = ["co_long_share", "co_long_share_missing", "co_ttc_log", "co_ttc_missing"]


def evaluate(rows: list[dict], feats_full: list[str] = FEATURES_FULL) -> dict:
    """Age-only vs full model, grouped CV by company; three AUC readings.

    * `pooled` — one AUC over all out-of-fold predictions. Biased DOWN for
      weak models under leave-companies-out CV (a held-out fold's base rate
      is anti-correlated with its training folds'), so not the headline.
    * `within_fold` — AUC inside each held-out fold, case-weighted mean
      (headline: same model scores every case it is compared with).
    * `within_company_cut` — AUC among postings of one company at one cut:
      can company-level features help at all? (they are constant there).
    """
    from sklearn.metrics import brier_score_loss

    if len(rows) < 50 or len({r["label"] for r in rows}) < 2:
        return {"n": len(rows), "note": "too few rows"}
    feats_full = list(feats_full)
    y = np.array([r["label"] for r in rows])
    g = np.array([r["company"] for r in rows])
    cc = np.array([f"{r['company']}|{r['cut']}" for r in rows])
    res: dict = {
        "n_cases": len(rows),
        "n_postings": len({r["key"] for r in rows}),
        "n_companies": len(set(g)),
        "positive_rate": round(float(y.mean()), 3),
        "by_cut": dict(Counter(r["cut"] for r in rows)),
        "by_company": dict(Counter(r["company"] for r in rows)),
    }
    age_feats = [f for f in feats_full if f.startswith("log_age")][:1]
    for kind in ("logit", "gbm"):
        p_age, fold = _oof(rows, age_feats, kind)
        p_full, _ = _oof(rows, feats_full, kind)
        out = {"ci": _cluster_boot(y, {"age": p_age, "full": p_full}, fold, cc, g)}
        for m in METRICS:
            a = _metric(m, y, p_age, fold, cc)
            f = _metric(m, y, p_full, fold, cc)
            out[m] = {"age": round(a, 3), "full": round(f, 3)}
        out["brier"] = {
            "age": round(float(brier_score_loss(y, p_age)), 4),
            "full": round(float(brier_score_loss(y, p_full)), 4),
            "base_rate": round(float(np.mean((y - y.mean()) ** 2)), 4),
        }
        out["calibration_full"] = _calibration(y, p_full)
        res[kind] = out
    # Raw (unfitted) single-feature AUCs: >0.5 = higher value -> more closure.
    raw = {}
    for f in ["age_days", *feats_full]:
        x = np.array([r[f] for r in rows], dtype=float)
        if np.isnan(x).all() or len(set(x[~np.isnan(x)])) < 2:
            continue
        x = np.where(np.isnan(x), np.nanmedian(x), x)
        raw[f] = {
            "pooled": round(_auc(y, x), 3),
            "within_company_cut": round(_group_auc(y, x, cc) or float("nan"), 3),
        }
    res["raw_feature_auc"] = raw
    # Feature effects: standardized logistic coefficients on all rows.
    X = np.array([[r[f] for f in feats_full] for r in rows], dtype=float)
    _, model = _fit_predict(X, y, X[:1], "logit")
    coefs = model[-1].coef_[0]
    res["logit_std_coefs"] = {f: round(float(c), 3) for f, c in zip(feats_full, coefs, strict=True)}
    return res


def repost_check(rows: list[dict]) -> dict:
    out = {}
    for flag in (0, 1):
        ys = [r["label"] for r in rows if r["repost"] == flag]
        n = len(ys)
        if n == 0:
            continue
        k = sum(ys)
        rate = k / n
        se = math.sqrt(rate * (1 - rate) / n) if n else 0
        out[f"repost={flag}"] = {
            "n": n,
            "closed_60d": k,
            "rate": round(rate, 3),
            "ci95": [round(max(0, rate - 1.96 * se), 3), round(min(1, rate + 1.96 * se), 3)],
        }
    # age-adjusted: logistic on log_age + repost
    if len({r["repost"] for r in rows}) == 2 and len(rows) > 50:
        X = np.array([[r["log_age"], r["repost"]] for r in rows], dtype=float)
        y = np.array([r["label"] for r in rows])
        _, model = _fit_predict(X, y, X[:1], "logit")
        out["logit_std_coef_repost_given_age"] = round(float(model[-1].coef_[0][1]), 3)
    return out


def signal(postings: dict[str, Posting], log: Callable[[str], None] = print) -> dict:
    out: dict = {}
    rows_db, acct_db = build_samples(postings, "db")
    rows_ar, acct_ar = build_samples(postings, "arch")
    log(f"samples db={len(rows_db)} arch={len(rows_ar)}")
    out["db"] = {"accounting": acct_db, **evaluate(rows_db)}
    out["arch"] = {"accounting": acct_ar, **evaluate(rows_ar)}
    # Posting-level signals only (no company habit): age vs age + repost + team.
    out["db_posting_only"] = evaluate(rows_db, FEATURES_POSTING)
    out["arch_posting_only"] = evaluate(rows_ar, FEATURES_POSTING)
    for ats in ("greenhouse", "ashby", "lever"):
        sub = [r for r in rows_ar if r["ats"] == ats]
        out[f"arch_{ats}"] = (
            evaluate(sub)
            if len({r["company"] for r in sub}) >= 3
            else {"n": len(sub), "note": "fewer than 3 companies"}
        )
    # Same (arch) sample, DB-only features: isolates what archived pages add as
    # features vs as labels/sample.
    db_feat = {(r["key"], r["cut"]): r for r in rows_db}
    paired = [dict(r) for r in rows_ar if (r["key"], r["cut"]) in db_feat]
    for r in paired:
        d = db_feat[(r["key"], r["cut"])]
        for f in FEATURES_FULL:
            r[f + "_db"] = d[f]
    if paired:
        same_label = [r for r in paired if db_feat[(r["key"], r["cut"])]["label"] == r["label"]]
        out["paired_db_vs_arch_features"] = {
            "n": len(paired),
            "label_agree": len(same_label),
            "arch_features": evaluate(paired),
            "db_features": evaluate(paired, [f + "_db" for f in FEATURES_FULL]),
        }
    out["repost_check_db"] = repost_check(rows_db)
    out["repost_check_arch"] = repost_check(rows_ar)
    out["by_ats_arch"] = {
        ats: {"n": len(rs), "pos": sum(r["label"] for r in rs)}
        for ats in ("greenhouse", "ashby", "lever")
        if (rs := [r for r in rows_ar if r["ats"] == ats])
    }
    out["has_page_capture_closure_rate_arch"] = {
        str(flag): {"n": len(rs), "rate": round(sum(r["label"] for r in rs) / len(rs), 3)}
        for flag in (0, 1)
        if (rs := [r for r in rows_ar if r["has_page_capture"] == flag])
    }
    return out


# ---------------------------------------------------------------------------
# Bias
# ---------------------------------------------------------------------------


def _group_stats(ps: list[Posting]) -> dict:
    durations, closed = [], 0
    for p in ps:
        fo = min(t for t, op in p.db_obs if op)
        lo = max(t for t, op in p.db_obs if op)
        durations.append((lo - fo).total_seconds() / 86400)
        closed += any(not op for t, op in p.db_obs if t > lo)
    return {
        "n": len(ps),
        "ats": dict(Counter(p.ats for p in ps)),
        "closed_share": round(closed / len(ps), 3),
        "median_observed_open_days": round(statistics.median(durations), 1),
        "share_team_engineering": round(
            sum(bool(p.team and "engineer" in p.team) for p in ps) / len(ps), 3
        ),
    }


def bias(postings: dict[str, Posting]) -> dict:
    """Postings with vs without any archived job-page capture.

    Stratified by observation regime, because the two groups differ in era:
    `own_era_new` = first seen more than 2 days after our own collector
    started on that company (new postings, observed twice daily, so closure
    share and duration compare like with like); `own_era_already_open` =
    first seen at the start of own collection; `pre_own` = first seen
    earlier (archived boards only before 2026-09-07). Within-company differences remove company mix.
    """
    strata: dict[str, dict[str, list[Posting]]] = defaultdict(lambda: defaultdict(list))
    own_start: dict[str, datetime] = {}
    for p in postings.values():
        if p.own_first is not None:
            cur = own_start.get(p.company_id)
            own_start[p.company_id] = p.own_first if cur is None else min(cur, p.own_first)
    for p in postings.values():
        if not p.in_db or not any(op for _, op in p.db_obs):
            continue
        last_open = max(t for t, op in p.db_obs if op)
        if last_open < WINDOW_START:
            continue
        first = min(t for t, op in p.db_obs if op)
        start = own_start.get(p.company_id)
        if start is not None and first > start + timedelta(days=2):
            stratum = "own_era_new"  # appeared while our own collector was watching
        elif first >= OWN_ERA_START:
            stratum = "own_era_already_open"
        else:
            stratum = "pre_own"
        grp = "with_capture" if p.page_captures else "without_capture"
        strata[stratum][grp].append(p)
        strata["all"][grp].append(p)
    out: dict = {}
    for stratum, groups in strata.items():
        out[stratum] = {grp: _group_stats(ps) for grp, ps in groups.items() if ps}
        diffs_closed, diffs_days = [], []
        by_co: dict[str, dict[str, list[Posting]]] = defaultdict(lambda: defaultdict(list))
        for grp, ps in groups.items():
            for p in ps:
                by_co[p.company_id][grp].append(p)
        for _co, g in by_co.items():
            if len(g["with_capture"]) >= 10 and len(g["without_capture"]) >= 10:
                w, wo = _group_stats(g["with_capture"]), _group_stats(g["without_capture"])
                diffs_closed.append(w["closed_share"] - wo["closed_share"])
                diffs_days.append(w["median_observed_open_days"] - wo["median_observed_open_days"])
        out[stratum]["within_company"] = {
            "companies": len(diffs_closed),
            "median_diff_closed_share": (
                round(statistics.median(diffs_closed), 3) if diffs_closed else None
            ),
            "median_diff_open_days": (
                round(statistics.median(diffs_days), 1) if diffs_days else None
            ),
        }
    # Timing: how soon after our first sighting does the first archived page appear?
    lags = []
    for p in strata["own_era_new"]["with_capture"]:
        first = min(t for t, op in p.db_obs if op)
        caps = [t for t, _ in p.page_obs] + [av for av, _, _ in p.page_pub]
        if caps:
            lags.append((min(caps) - first).total_seconds() / 86400)
    if lags:
        out["own_era_first_capture_lag_days"] = {
            "n": len(lags),
            "median": round(statistics.median(lags), 1),
            "share_before_first_sighting": round(sum(x < 0 for x in lags) / len(lags), 3),
        }
    per_company: dict[str, Counter] = {}
    for p in strata["all"]["with_capture"] + strata["all"]["without_capture"]:
        d = per_company.setdefault(p.company_id, Counter())
        d["n"] += 1
        d["with"] += p.page_captures > 0
    out["capture_share_by_company"] = {
        k: round(v["with"] / v["n"], 3) for k, v in sorted(per_company.items())
    }
    return out


def run_all(
    pilot: sqlite3.Connection, main: sqlite3.Connection, log: Callable[[str], None] = print
) -> dict:
    postings = load_postings(main, pilot, log=log)
    result = {"validation": validation(pilot, postings)}
    log("validation done")
    result["coverage"] = coverage(postings)
    result["replay_gain"] = replay_gain(main, postings, log=log)
    log("coverage done")
    result["signal"] = signal(postings, log=log)
    log("signal done")
    result["bias"] = bias(postings)
    fetch = pilot.execute(
        """SELECT COUNT(*) AS n, SUM(ok) AS ok, SUM(from_cache) AS cache,
        MIN(fetched_at) AS first, MAX(fetched_at) AS last FROM fetches"""
    ).fetchone()
    result["fetch_stats"] = dict(fetch)
    result["capture_stats"] = [
        dict(r)
        for r in pilot.execute(
            """SELECT ats, COUNT(*) AS captures, COUNT(DISTINCT company_id || job_id) AS jobs,
        SUM(posting_id IS NOT NULL) AS mapped_captures,
        COUNT(DISTINCT CASE WHEN posting_id IS NOT NULL THEN posting_id END) AS mapped_postings
        FROM captures GROUP BY ats"""
        )
    ]
    result["parsers"] = [
        dict(r)
        for r in pilot.execute(
            """SELECT ats, evidence, parser, state, COUNT(*) AS n FROM page_obs
        GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 5 DESC"""
        )
    ]
    return result
