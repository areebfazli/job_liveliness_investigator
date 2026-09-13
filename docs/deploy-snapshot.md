# Deploying the daily board-snapshot job

`rli snapshot` captures each verified target company's current open jobs
once per day and maintains posting lifecycle state (spec.md §5). Per
PLAN.md's M1 notes, this is the project's "long pole": start it running from
day 0 and keep it running continuously through later milestones, since
history-gated evidence (M2+) only exists for however long this job has
actually been running.

## Laptop timer (default)

The default deployment is a systemd **user** timer, not cron. A laptop
isn't always on, and plain cron has no built-in way to "catch up" a missed
day — if the machine is asleep or off at the scheduled time, that day's run
just never happens. systemd timers do have that semantics
(`Persistent=true`: a missed run fires as soon as the machine is next
booted/logged in), which is why the daily job is installed as a timer
instead.

Install it with:

```sh
scripts/setup_laptop_timer.sh
```

This installs a `rli-daily.timer` + `rli-daily.service` user unit pair that
runs `scripts/daily.sh` once a day (`OnCalendar=daily`, randomized by up to
15 minutes so it doesn't fire at exactly the same instant every day), and
enables `loginctl enable-linger` so the timer still fires even when you are
logged out. Enabling linger needs your password once; the script warns and
continues if that fails, so a failed linger step doesn't fail the install.

Verify it's installed and firing:

```sh
systemctl --user list-timers
journalctl --user -u rli-daily
```

Uninstall with:

```sh
scripts/setup_laptop_timer.sh --uninstall
```

## Free cloud VM (optional)

For a truly always-on setup that doesn't depend on the laptop being open,
`scripts/daily.sh` can instead run on a free-tier cloud VM — Oracle Cloud
Always Free (ARM Ampere) or a GCP e2-micro both work. This is optional: the
laptop timer above is sufficient on its own, and the coverage-gap semantics
below mean a laptop that's sometimes closed just shows up as occasional
gaps rather than corrupting the data.

Worth knowing before provisioning one: `data/rli.db` is already ~1.8GB (as
of 2026-09-13) and grows every day. Daily runtime is budgeted at roughly 10
minutes total — dominated by the network-bound `rli snapshot` on most days
(one HTTP GET per company board across 78 companies), and occasionally the
~4.5-minute `rli history rebuild` that runs on Sundays. Pick a VM shape with
enough disk headroom for the database to keep growing.

Set up a fresh Ubuntu/Debian VM with:

```sh
scripts/setup_cloud_vm.sh <git-repo-url> [path-to-local-.env]
```

This installs `uv` and `git`, clones the repo, optionally copies in a local
`.env` (for any API keys), runs `rli init-db` and `rli load-targets`, and
installs a plain system cron entry for `scripts/daily.sh` — the VM is
always-on, so none of the systemd-user/linger dance from the laptop setup
is needed there.

To move the database between the laptop and the VM in either direction:

```sh
scripts/sync_db.sh pull <vm-ssh-host> [vm-repo-path]
scripts/sync_db.sh push <vm-ssh-host> [vm-repo-path]
```

Both directions run `sqlite3 ... VACUUM INTO` on the source side first, so
a live WAL-mode database is never rsynced mid-write.

## Coverage-gap semantics

A failed or skipped capture — a down laptop, a missed day, a transient
5xx — is recorded per-company as a `capture_attempts` coverage gap: a gap
in that day's `board_snapshots` row growth for that company. It is **never**
interpreted as "the postings on that board disappeared" or "closed" — a
coverage gap and an actual posting closure are tracked separately, and
nothing in this project treats the absence of a capture as evidence of
either.

Three independent checks, roughly in order of how much they tell you:

1. **`rli snapshot-status --db data/rli.db`** — prints one line per target
   company with its most recent `board_snapshots.captured_at` and
   `coverage_status`, plus a final coverage-gap count (failed
   `capture_attempts` rows). Every target company should show today's date;
   any company still showing yesterday's (or `last_captured_at=None`) did
   not get captured today and is worth investigating.

2. **Tail today's log file**: `tail data/logs/daily-$(date -u +%Y%m%d).log`
   should show the summary line, e.g.:

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

   A day with a much lower count than the target list size means the daily
   job itself failed to run (check the log file and `systemctl --user
   list-timers` / `journalctl --user -u rli-daily`) rather than that most
   companies individually failed.
