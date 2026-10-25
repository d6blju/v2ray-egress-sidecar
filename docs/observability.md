# Incremental statistics and resource controls

The dashboard stores normalized completed-session records and JSONL byte cursors in
SQLite (`stats.jsonl.sqlite3`, outside the web root). Each run reads at most 4 MiB
of new data. Events and cursors commit together; partial lines wait for the next
run, oversized records are discarded with a counter, and malformed JSON cannot
stop collection. A rename or copytruncate is detected. Retained events cover the
last 24 hours; dashboard time windows retain millisecond timestamps. Traffic from
still-active sessions is not included until the sidecar writes their completion.

The first run backfills the existing file over multiple batches. `backlog_bytes`
and `collection_status=catching up` make incomplete initial history explicit.
Subsequent runs do not reread old JSON. A fully consumed active file over 64 MiB
is renamed, never copytruncated. The Rust writer already opens the pathname for
each append, so no signal, restart or Rust binary upgrade is needed. Late writes
to a renamed inode are ingested on the next run. Consumed archives older than
three days are removed. Unconsumed archives are retained, including under resource
pressure; this deliberately favors data integrity over a hard disk cap. Monitor
disk capacity. SQLite reuses freed event pages instead of growing with total
historical traffic; no periodic VACUUM is run on the forwarding host.

Rendering pauses when CPU sampling reaches 80%, available memory drops below 20%,
or established host TCP sockets reach 2048. The last good page remains available
and shows the pause reason. Collection resumes at the next timer run. Defaults:
5% of one CPU, 256 MiB memory, low scheduling priority, one render per minute.
`--max-bytes`, `--rotate-bytes`, `--retention-days` and `--database` are configurable.
`--ignore-pressure` is only for isolated tests, never normal production rendering.

## CPU observer disabled by default

Continuous V2Ray CPU monitoring is disabled by default. The dashboard does not
read or publish diagnostic heartbeats, current process CPU, incident history or
findings. The continuous observer is not needed for traffic statistics, route
management or the renderer's resource-pressure guard. That guard performs its
bounded resource checks when the renderer runs, rather than sampling V2Ray
processes and threads every second.

Routine UI installation and upgrades stop and disable both the legacy
`v2ray-cpu-diagnose.service` and `v2ray-cpu-diagnose.timer`. Existing diagnostic
scripts, units and private reports may remain for compatibility or manual
investigation, but are not started by the normal installer. Reports left on disk
are historical data and are not refreshed after the observer is stopped. Keep
them private and manage their retention separately; do not publish them to Git.

## Install or upgrade

```sh
sudo bash deploy/install-ui.sh
```

Keep V2Ray and the Rust sidecar running throughout. The UI installer updates the
statistics modules and dashboard without restarting either forwarding process.
For existing installations using `/var/lib/v2ray-rust-sidecar`, keep the renderer's
`--stats` argument pointed at that installation's active JSONL file. The renderer
derives its default SQLite path from `--stats`; no diagnostic-service environment
is needed.

Back up scripts, unit files and the static page before a manual upgrade. To roll
back a statistics change, restore the renderer and statistics modules and retain
the current log pathname and SQLite database. Re-enabling the legacy CPU observer
is not required to restore traffic statistics.

## Validation

Run `python3 -m unittest discover -s tests`; Linux must also pass the open-inode
rotation/late-writer test. Run `tests/benchmark_observability.py` on a helper
machine to compare initial import against unchanged incremental refresh. Do not
artificially load the production V2Ray process to test statistics rendering.
