# Route transactions and recovery

Saving, deleting one saved service and clearing all saved services use the same
persistent transaction. Manual rules and unrelated outbounds remain outside the
UI-managed block. An apply performs:

1. Validate the existing configuration with the Rust parser before creating or
   reusing an exit. Reject damaged UI block markers and invalid state JSON.
2. Reuse the country's existing container where possible. Probe HTTPS through its
   localhost HTTP CONNECT proxy and measure IP/country using Cloudflare trace.
   A country mismatch is rejected when an expected country code is available.
3. Build the candidate off-path and run the standalone `--check` validator again.
4. Save a private original-config backup and fsync the transaction journal.
5. Atomically replace the configuration. Wait for a successful reload message
   from the same running sidecar PID after the saved journal cursor, while checking
   that the file has not changed. Then recheck the exit and test a sidecar HTTPS
   connection. Only after these pass is the UI state committed.

The checker uses the same configuration parser as the sidecar. `--check` exits
before binding a listener or creating the statistics directory. Existing servers
can install a newly built checker without replacing their running forwarding
binary. No V2Ray or sidecar restart is required for a route transaction.

Before publishing, validation/probe failures leave the old configuration and
state untouched. After publication, reload rejection, timeout, connectivity or
state-write failures restore the previous files and verify the rollback reload.
On management API startup, an unfinished transaction is rolled back; a committed
transaction with unfinished bookkeeping is completed idempotently. If an external
configuration/state edit is detected, preserve it, retain the journal and block
new mutations. An unconfirmed rollback also blocks new mutations. Do not manually
delete the journal until files and the running sidecar have been reconciled.

Mutations use both a thread lock and a POSIX file lock. Optimistic content hashes
detect external edits, but external tools should also respect `route.lock` to
avoid racing an atomic replacement. Pending transactions protect both old and new
exit ports from container cleanup. A failed new exit can remain cached for reuse;
it does not replace a working country's container. There is no implicit direct
fallback for traffic assigned to an unavailable VPN.

The page shows all saved services and their exits, configuration/state agreement,
pending recovery and the last transaction. A previous failure also remains in
the private state directory after a later success. Selected-exit
validation is separate from the `ip.sb` check: `ip.sb` can intentionally have a
different manual route. A successful anonymous HTTPS/country probe does not prove
that a streaming or financial service accepts that VPN IP.

Files under the configured admin state directory (mode 0700):

- `route-transaction.json`: private old configuration and state for recovery.
- `route-last-result.json`: latest transaction outcome.
- `route-last-failure.json`: persistent previous failure.
- `route.lock`: cooperative process lock.

Transaction files/backups are private (0600). Configuration mode and ownership
are preserved on replacement. Backups are not automatically pruned by this change;
retain those needed for recovery and manage their disk usage separately. These
files, logs, databases and credentials must never be published to Git.

## Saved service routes

Each profile ID identifies one saved service route. A legacy active single-route
state is migrated on the next save without dropping its domains or exit. Saving
another profile adds a route; saving an existing profile replaces only that
profile. `Custom` is one such profile, so repeated custom saves update the same
slot.

The control page lists every saved service with Edit and Delete buttons. Edit
backfills the saved country, protocol and domains and locks the profile selector
until save or cancel. A supplied domain list is authoritative: domains removed
from the form are not restored from the preset. A request that omits `domains`
uses the profile's defaults. Empty domain lists are rejected unless the separate
`ip.sb` option supplies domains. Exact domains owned by another saved profile,
and overlapping suffixes on different exits, are rejected without modifying that
other profile.

Services using the same country and VPN protocol can reuse one container and
proxy. The candidate configuration emits one outbound per distinct saved proxy,
with service domain rules referring to it. More specific suffixes are ordered
before broader suffixes. All referenced ports remain protected from container
cleanup, including the ports needed for transaction rollback.

`POST /api/routes/delete` takes a saved `profile` and deletes only that entry.
It requires the same authenticated session and CSRF token as Apply. Missing
profiles return 404; external configuration edits block deletion. Removing the
last service leaves no UI-managed rules. Delete does not stop a shared exit or
remove manual rules. Clear removes every UI-managed service route. Both use
validation, atomic publication, hot-reload acknowledgement and rollback rather
than editing only the state file.

## Install or upgrade

```bash
cargo build --release
sudo bash deploy/install-ui.sh
sudo systemctl restart v2ray-egress-sidecar-admin-api.service
```

`SIDECAR_CHECK_SOURCE` selects a checker binary built for the target machine.
`SIDECAR_CHECK_BIN` selects the installed checker path. `SIDECAR_SERVICE` selects
the forwarding service whose journal/PID is monitored; the default is
`v2ray-egress-sidecar.service`. For legacy installations the provided
`systemd/v2ray-route-transaction-legacy.conf` sets the older forwarding unit name.
Set `SIDECAR_CONFIG` and `SIDECAR_ADMIN_STATE_DIR` to existing paths as needed.
Journal access is required. If the checker, exit probe or reload acknowledgement
is unavailable, mutation fails closed while existing forwarding keeps running.

Recovery depends on a valid old configuration. Changes requiring a listener or
process restart are outside this API; it only edits the UI routing block. Already
open connections keep their selected exit across apply/rollback. New connections
can briefly use the candidate between its reload and a failed postcheck; a
rollback cannot undo requests already sent during that interval.

## Tests

```bash
cargo test --offline
cargo build --release --offline
SIDECAR_TEST_CHECK_BIN="$PWD/target/release/v2ray-egress-sidecar" python3 -m unittest discover -s tests
```

Tests cover damaged configuration, preflight failures, reload rejection, postcheck
failure, state-write errors, process interruption/recovery, commit bookkeeping,
external-edit conflicts and Clear rollback. The real-binary integration test uses
two isolated localhost echo proxies to verify that new connections change exits
and established connections survive both application and rollback.
