# Incremental statistics and CPU diagnostics

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

`v2ray-cpu-diagnose.service` is now a persistent Python observer with a 2% CPU and
32 MiB memory limit. Disable its old timer when upgrading. It samples process and
thread CPU from `/proc` deltas every second, divided by actual elapsed wall time.
PID/start-time changes reset sampling. Its heartbeat is saved every 30 seconds.
A sample >=80% triggers a report with up to 60 seconds of history and five further
samples, limited to one report per five minutes. Reports are root-readable only;
the dashboard publishes the heartbeat and conclusion summary, not sockets/logs.
The collector never restarts, signals, attaches to or reconfigures V2Ray.

The dashboard presents the current monitoring state, process CPU, alert threshold
and PID as separate cards, with the sample time in Beijing time. The Chinese
labels distinguish a fresh sample from a stale heartbeat. CPU 100% represents
approximately one logical core. The latest incident is collapsed by default and
explicitly labelled historical; expanding it shows the peak and translated
findings without treating possible causes as a diagnosis. A historical spike is
not evidence of a current fault. Original evidence for unrecognised finding types
remains available in the expanded details.

Reports include instantaneous thread CPU, descriptor trend, host network rate,
I/O wait/steal, bounded socket/process/journal output, and recent completed-domain,
service, outbound and error groups from the database. Findings distinguish observed
DNS failures, descriptor pressure, host contention, traffic correlation, and CPU
without large host traffic. Correlation does not establish the busy Go function:
encryption, GC and a busy loop need a separate CPU profile to distinguish. A leak
requires a persistent growth trend; current snapshots cannot prove one. Live TLS
domains and original V2Ray inbound user IDs are unavailable to this observer.
Statistics may lag during catch-up or a pressure pause; reports include the latest
completed-event timestamp. After each capture, archives are pruned to the last
20 reports and archives older than three days are removed. The latest report
remains available even if no further CPU incidents occur.

```sh
systemctl status v2ray-cpu-diagnose.service
cat /var/log/v2ray-cpu-diagnostics/status.json
cat /var/log/v2ray-cpu-diagnostics/latest.txt
# Full private evidence:
cat /var/log/v2ray-cpu-diagnostics/latest.json
```

For existing installations using `/var/lib/v2ray-rust-sidecar`, set
`SIDECAR_STATS_DATABASE=/var/lib/v2ray-rust-sidecar/stats.jsonl.sqlite3` in the
diagnostic service environment. The dashboard derives its DB path from `--stats`.
Install the new modules beside `sidecar_services.py`, replace only the dashboard
script and diagnostic service, disable the old diagnostic timer, then reload
systemd. Keep V2Ray and Rust sidecar running throughout. Back up scripts, unit
files and the static page before replacing them. To roll back, restore those
files and the old timers; archives can remain because the forwarding writer uses
the unchanged active pathname.

Validation: `python3 -m unittest discover -s tests`; Linux must also pass the
open-inode rotation/late-writer test. Run `tests/benchmark_observability.py` on a
helper machine to compare initial import against unchanged incremental refresh.
Use `v2ray_cpu_watch.py --once --out /tmp/isolated-diagnostic-test --database ...`
for a forced report without artificially loading the production V2Ray process.
