# Daily board-snapshot cron job

This moved to [`docs/deploy-snapshot.md`](deploy-snapshot.md).

Plain cron was replaced as the default deployment because a laptop isn't
always on, and cron has no built-in way to catch up a run it missed while
the machine was asleep or off. See `docs/deploy-snapshot.md` for the
systemd user timer that replaced it, the optional always-on cloud VM, and
the verification checklist.
