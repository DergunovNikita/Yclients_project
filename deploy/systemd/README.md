# Systemd Units

Three sync schedules, from cheapest to most expensive:

- `yclients-sync-incremental@` — every few hours; re-reads only `SYNC_LOOKBACK_DAYS`
  around the last checkpoint.
- `yclients-sync-refresh@` — nightly; drops and reloads the trailing
  `SYNC_REFRESH_DAYS` window, so payments registered after the incremental lookback
  closed still arrive, and rows YClients has deleted stop lingering.
- `yclients-sync-full@` — weekly; the same purge-and-reload over the entire history.

Only one sync runs at a time: the pipeline takes a non-blocking advisory lock and exits
with status 75 when another run holds it. The units treat that exit as success, but the
skipped run is not retried, and it is not logged as anything but a clean unit.

That is harmless when the wider job wins — a `full` that overruns into an incremental
slot covers the same ground anyway. It is not harmless in reverse: give `refresh` and
`full` enough distance that the nightly window never swallows the weekly pass, and
remember a branch missing its coverage marker escalates `refresh` to a full history run,
which for a branch open since 2018 takes hours rather than minutes.

## Which scheduler is live

Two mechanisms exist, and the repository shows only one of them running:

- **Worker auto-enqueue** (`sync_worker.py`, `SYNC_AUTO_ENQUEUE_ENABLED`, default on; every
  `SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES`, default 240) is started by `docker compose up -d api worker`,
  which the auto-deploy script runs. It enqueues only `incremental` jobs, one per active tenant
  credential, and skips a tenant that has a recent job.
- **The timers in this directory** are the only thing in the repository that schedules `refresh` and
  `full`. Nothing in the deploy script or workflows installs them (no `systemctl enable` anywhere), so
  they exist on a host only if someone installed them by hand. Without them nothing re-reads the
  trailing window or the full history: deleted and late-registered payments are never reconciled.

The overlap on `incremental` is harmless (one advisory lock, a loser exits 75). The API is not a
systemd service: it runs in Docker (`docker compose`), and the old host-venv unit was removed.

These files are generic templates and the cadences in them are defaults to adjust per
environment. Production installation steps, service users, the schedules actually in
force, host paths and troubleshooting commands are intentionally kept out of this
public repository.

Store environment-specific runbooks in private operational documentation.
