# 🔒 Security

The API and dashboard are guarded by a request middleware (the vendored `homeguard` package under `src/pengumon/_vendor/homeguard/`, with
this app's profile in `security.py`): the `Host` header must be an IP
address, `localhost`, a single-label name, a `.local/.lan/.home.arpa/.internal` name or listed in
`PENGUMON_ALLOWED_HOSTS` (blocks DNS rebinding); state-changing requests must be same-origin; request bodies
are size limited; the websocket checks Host, Origin and the token. Responses carry a strict CSP (scripts from
`'self'` only, no inline scripts). `data/secrets.json` is written atomically with mode 0600. The traceroute and
credential endpoints are rate limited.

| Variable | Purpose |
|---|---|
| `PENGUMON_ACCESS_TOKEN` | Optional shared token. When set, every request except `/api/health`, `/login` and `/static/` needs it, as `Authorization: Bearer ...` or as the HttpOnly session cookie that the `/login` page sets. Unset = open to everyone who can reach the port |
| `PENGUMON_ALLOWED_HOSTS` | Extra host names, comma separated (`.example.net` also matches subdomains) |
| `PENGUMON_ALLOWED_ORIGINS` | Extra origins allowed for state-changing requests and the websocket |
| `PENGUMON_MAX_BODY_BYTES` | Request body limit (default 262144) |
| `PENGUMON_COOKIE_SECURE` | `1` when served over HTTPS (marks the session cookie `Secure`) |
| `PENGUMON_SESSION_MAX_AGE_DAYS` | How long a sign-in lasts, enforced by the server (default 30; at least 1 hour (0.0417), at most 400; fractions allowed) |

## Signing out

With a token set, a browser signed in through `/login` shows **Sign out** in the header (in the "more" menu on a
phone). Its dialog has two choices:

* **Sign out**: ends the sign-in of this browser (`POST /logout`). The server refuses that cookie from then on,
  so a copy of it stops working too.
* **Sign out everywhere**: after a confirmation, ends the sign-in of every browser and device at once, this one
  included, without changing the token (`POST /logout?everywhere=1`). Use it when a device was lost or a sign-in
  was shared.

Both are kept in `data/sessions.json` (in the data volume, mode 0600; it holds times and hashes, no usable
cookie), so they survive a restart. Deleting the file undoes them. If the file cannot be read, every sign-in is
refused until it is fixed or deleted (the log says so); scripts that send `Authorization: Bearer` are not affected
by any of this.

A sign-in also ends on its own after `PENGUMON_SESSION_MAX_AGE_DAYS` (at least 1 hour), and every sign-in ends when
the token changes. A dashboard left open in a browser whose sign-in ended goes to the sign-in page by itself (its live
connection is closed, or its next request refused). Sign-ins from before this version (the old cookie format) are
refused: sign in once more after the upgrade.

Rolling back to an image from before this change makes old copied cookies valid again: change the token after such a
rollback.

`docker-compose.yml` passes `PENGUMON_ACCESS_TOKEN`, `PENGUMON_ALLOWED_HOSTS`, `PENGUMON_SESSION_MAX_AGE_DAYS`, `PENGUMON_NOTIFY_URL`,
`PENGUMON_NOTIFY_TOKEN` and the modem/router passwords on from the git-ignored `.env` (see `.env.example`).
