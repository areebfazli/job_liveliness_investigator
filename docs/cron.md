# Daily board-snapshot cron job

`rli snapshot` captures each verified target company's current open jobs
once per day and maintains posting lifecycle state (spec.md §5). Per
PLAN.md's M1 notes, this is the project's "long pole": start it running from
day 0 and keep it running continuously through later milestones, since
history-gated evidence (M2+) only exists for however long this job has
actually been running.

## Crontab entry

Run once per day at a fixed UTC time (06:00 UTC below — pick any time; what
matters is that it is fixed and does not drift with DST, hence UTC). `cd`
into the repo root first: `rli snapshot`'s `--targets`/`--db` defaults are
resolved module-relative to the repo (via `rli.snapshots.targets.
DEFAULT_TARGETS_PATH` / the `./data/rli.db` default), but a `cron`-invoked
shell has an unpredictable working directory, and a minimal `PATH` that may
not include `uv` at all — so use absolute paths throughout.

```cron
0 6 * * * cd /absolute/path/to/job_liveliness_investigator && /absolute/path/to/uv run rli snapshot >> data/logs/snapshot-$(date -u +\%Y\%m\%d).log 2>&1
```

Notes on that line:

- `%` must be escaped as `\%` inside a crontab (`date`'s format directives
  would otherwise be interpreted by cron as a newline).
- `>> ... 2>&1` appends both stdout and stderr to one dated log file per day
  under `data/logs/`, so a failed run's traceback is not lost and a rerun on
  the same day does not clobber the log.
- Find `uv`'s absolute path with `which uv` (typically
  `~/.local/bin/uv` or `~/.cargo/bin/uv`) — cron's `PATH` is usually just
  `/usr/bin:/bin`, so a bare `uv` will not resolve. Alternatively, set `PATH`
  explicitly at the top of the crontab.
- Create `data/logs/` ahead of time (`mkdir -p data/logs`) — cron does not
  create the redirect target's parent directory for you.

## Verifying the job actually ran

Three independent checks, roughly in order of how much they tell you:

1. **`rli snapshot-status --db data/rli.db`** — prints one line per target
   company with its most recent `board_snapshots.captured_at` and
   `coverage_status`, plus a final coverage-gap count (failed
   `capture_attempts` rows). Every target company should show today's date;
   any company still showing yesterday's (or `last_captured_at=None`) did
   not get captured today and is worth investigating.

2. **Tail today's log file**: `tail data/logs/snapshot-$(date -u +%Y%m%d).log`
   should show the `rli snapshot` summary line, e.g.:

   ```text
   companies: ok=76 failed=2 skipped=0 | postings: new=5 absent=3 reappeared=1
   ```

   `failed` here is expected to be occasionally nonzero (a transient 5xx/
   rate-limit is a coverage gap, not a bug — spec.md §4); `ok + failed +
   skipped` should equal the number of loaded targets.

3. **Row-count growth**: `board_snapshots` should gain roughly one row per
   target company per day (~78 as of this writing):

   ```sql
   SELECT date(captured_at), COUNT(*) FROM board_snapshots GROUP BY date(captured_at) ORDER BY 1;
   ```

   A day with a much lower count than the target list size means the cron
   invocation itself failed to run (check the log file and `crontab -l`)
   rather than that most companies individually failed.
