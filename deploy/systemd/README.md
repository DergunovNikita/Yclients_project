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

These files are generic templates and the cadences in them are defaults to adjust per
environment. Production installation steps, service users, the schedules actually in
force, host paths and troubleshooting commands are intentionally kept out of this
public repository.

Store environment-specific runbooks in private operational documentation.
