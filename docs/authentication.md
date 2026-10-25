# Management Authentication

[English README](../README.md) | [中文 README](../README.zh-CN.md)

## Existing Installations

The management API no longer accepts `X-Sidecar-Admin` as authorization. All
management reads require a server-side session. Writes, including login and
logout, also require JSON, a session-bound `X-CSRF-Token`, and same-origin checks.
`GET /api/health` exposes only readiness and whether authentication is configured.

Existing Nginx Basic Auth can remain as an outer gate. Configure the API to use
the same local password file; it independently verifies credentials rather than
trusting a forwarded username header. The account and password do not change.
After the first successful verification, the API stores a salted
PBKDF2-HMAC-SHA256 verifier (600,000 iterations). APR1 migration requires OpenSSL;
bcrypt migration requires system `crypt` support. Nginx uses the same system
capability for Basic Auth: an unsupported `$2y$` verifier can produce HTTP 500
with `crypt_r() failed (22: Invalid argument)`. On such systems, back up the
password file and interactively regenerate only the affected account with
SHA-512 crypt, using `htpasswd -5 FILE USER` (without `-c` for an existing file).
The original plaintext password is required; an existing bcrypt hash cannot be
converted to another format. Preserve restrictive ownership and permissions,
and verify successful and rejected credentials before considering it fixed.
Changing or deleting a migrated account in the Basic Auth file invalidates its
sessions. Existing browser Basic Auth credentials establish the first session
automatically. After logout, explicit form login is required instead of silently
reusing cached Basic Auth.

For a new standard-path installation, add a systemd API drop-in with local paths:

```ini
[Service]
Environment=SIDECAR_HTPASSWD_FILE=/etc/nginx/.htpasswd-v2ray-egress-sidecar
Environment=SIDECAR_COOKIE_SECURE=1
Environment=SIDECAR_COOKIE_PATH=/sidecar-stats/
```

Use an interactive tool to create/update the password file, never a password in
shell arguments or a checked-in environment file:

```bash
sudo htpasswd -cB /etc/nginx/.htpasswd-v2ray-egress-sidecar sidecar
sudo chmod 640 /etc/nginx/.htpasswd-v2ray-egress-sidecar
```

Choose ownership so Nginx and the API can read it, but unrelated users cannot.
Use `-c` only when creating the file, not when adding accounts. Set
`SIDECAR_ADMIN_STATE_DIR` to a private persistent directory (0700); preserve it
across API upgrades. Install `deploy/sidecar_auth.py` alongside the existing API
helper modules. `deploy/install-ui.sh` installs the new assets and helper.

After adding the API drop-in, run `systemctl daemon-reload` and restart only
`v2ray-egress-sidecar-admin-api.service` (or the existing management service name).
Check `/api/health` before reloading Nginx. Do not restart the forwarding services.

For a deliberately standalone deployment without Basic Auth, create a native API
account with the interactive CLI:

```bash
sudo python3 /usr/local/lib/v2ray-egress-sidecar/sidecar_auth.py sidecar
```

Pass `--directory` before the username for a nonstandard state directory. This
CLI changes the API account only, not the outer Nginx Basic Auth account.
It revokes that account's sessions. Never expose an unconfigured API publicly.

## Protect Pages And Statistics

Use HTTPS and Nginx's `http_auth_request_module`. The public static location must
use `auth_request /_sidecar_session_check`, not just a JavaScript redirect; this
also protects `stats.json`. Login HTML and `auth.js` are exempt from the session
gate, but retain the existing outer Basic Auth gate. API requests go directly to
the session-aware backend; do not replace backend checks with trusted headers.

The UI omits the account/logout toolbar. Session verification and CSRF checks
remain active; automation can still call the authenticated logout endpoint.

Include `/etc/nginx/mime.types` in the relevant `http` or server block so static
assets have their correct content types. In particular, `auth.js` must not be
served as `text/plain` together with `X-Content-Type-Options: nosniff`, because
browsers will block it and no statistics will load. The migration helper adds
an explicit JavaScript default type, including when upgrading an existing v1
session gate. Run it again, test Nginx configuration, and gracefully reload.

The example in `deploy/nginx-sidecar-ui.conf.example` explains how to augment an
existing Basic-protected `/sidecar-stats/` location safely:

```bash
sudo env PYTHONPATH=/usr/local/lib/v2ray-egress-sidecar \
  python3 /usr/local/lib/v2ray-egress-sidecar/sidecar_nginx_auth.py /etc/nginx/nginx.conf
sudo nginx -t
sudo systemctl reload nginx
```

Back up the configuration first. The helper refuses ambiguous/conflicting
locations and does not modify other paths, certificates or proxy tunnels. Restore
the backup if `nginx -t` fails. A reload is graceful; no V2Ray/sidecar restart is
needed. The local-only HTTP example is for SSH tunnels/testing; browser testing
over HTTP requires `SIDECAR_COOKIE_SECURE=0` and `SIDECAR_COOKIE_PATH=/` in an
isolated API instance, never in the public HTTPS deployment.

`deploy/deploy-auth-existing.sh` is a **legacy-path** management-only upgrade,
not a generic installer. It backs up the old management code/assets/Nginx config,
preserves the password file and forwarding configuration, checks readiness and
unchanged forwarding PIDs, and rolls management changes back on failure. It
restarts only the management API and gracefully reloads Nginx. Review its paths
before using it on another installation. Existing sessions persist across API
restarts because both the CSRF key and session database are retained.

## Sessions And Automation

Cookies are host-only, HttpOnly, Secure by default and SameSite=Strict. Idle expiry
is 30 minutes, absolute expiry 12 hours; pre-login sessions expire after 5 minutes.
Login rotates both session and CSRF tokens; logout revokes the session immediately.
Only SHA-256 digests of random session IDs are stored in SQLite. The browser keeps
the CSRF token only in memory, not localStorage. State-changing requests are never
automatically replayed following an authentication or network error.

Login verification is limited to 30 attempts globally, 10 per IP, and 10 per
account per minute, persisted across restarts. Session capacity is capped at 2,000.
When proxying, overwrite `X-Real-IP` with the actual client address and keep the
API bound to loopback. An untrusted client must not be able to reach that port.

Automations must obtain a pre-login session and CSRF token from `/api/session`,
POST JSON credentials to `/api/login` with its cookie and `X-CSRF-Token`, then use
the rotated cookie/token for mutations. Existing Basic Auth clients may bootstrap
via `/api/session` with `X-Sidecar-Session: 1`. Do not put credentials/tokens in
command history, logs, URLs, repository fixtures or issue reports.

Private state includes `auth-users.json`, `auth-csrf.key`, `auth-sessions.sqlite3`
and its journal files. Keep these and Basic Auth files out of Git and web roots.
Backups also contain private configuration and must remain root-only. Clearing
UI routing rules never clears authentication state or manually managed routes.

Design references: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
[CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).
