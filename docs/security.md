# 🔒 Security

The API and dashboard are guarded by a request middleware (the vendored `homeguard` package under `src/pengumon/_vendor/homeguard/`, with
this app's profile in `security.py`): the `Host` header must be an IP
address, `localhost`, a single-label name, a `.local/.lan/.home.arpa/.internal` name or listed in
`PENGUMON_ALLOWED_HOSTS` (blocks DNS rebinding); state-changing requests must be same-origin; request bodies
are size limited; the websocket checks Host, Origin and the token. Responses carry a strict CSP (scripts from
`'self'` only, no inline scripts). `data/secrets.json` is written atomically with mode 0600. The traceroute and
credential endpoints are rate limited. No external program is started for probing: ping and traceroute are sent by the
app itself, to IPv4 or IPv6 addresses, after the host is validated.

## Settings

All of them are environment variables:

* `PENGUMON_ACCESS_TOKEN`: an optional shared token. When set, every request except `/api/health`, `/login` and
  `/static/` needs it, as `Authorization: Bearer ...` or as the HttpOnly session cookie that the `/login` page sets
  (but see [View only](#view-only-without-the-token)). Unset, the dashboard is open to everyone who can reach the
  port.
* `PENGUMON_VIEW_WITHOUT_TOKEN`: `1` lets people without the token see the dashboard but change nothing (next
  sections). Off by default, and it does nothing without a token.
* `PENGUMON_ALLOWED_HOSTS`: extra host names, comma separated (`.example.net` also matches subdomains).
* `PENGUMON_ALLOWED_ORIGINS`: extra origins allowed for state-changing requests and the websocket.
* `PENGUMON_MAX_BODY_BYTES`: the request body limit (default 262144).
* `PENGUMON_COOKIE_SECURE`: `1` when served over HTTPS (marks the session cookie `Secure`).
* `PENGUMON_SESSION_MAX_AGE_DAYS`: how long a sign-in lasts, enforced by the server. Default 30; at least 1 hour
  (0.0417), at most 400; fractions allowed.

## Without an access token

With no token, any device that can reach the port can change settings, including the probe path: it can remove hops and
delete a removed hop's history (from 0.19 on, also the hops that came from `config.yaml`, which the API could not remove
before). The default listens on this computer only, which is safe. If you open the dashboard to your network and other
people's devices share it (guests, children's computers, smart-home gadgets), set `PENGUMON_ACCESS_TOKEN`
([Opening the dashboard from other devices](install.md#opening-the-dashboard-from-other-devices)).

## View only without the token

Some households want the dashboard on a shared screen, or on everyone's phone, without handing out the token that also
changes settings. With a token set and `PENGUMON_VIEW_WITHOUT_TOKEN=1`, a browser without the token sees the whole
dashboard, live, but cannot change anything:

* **What a viewer can do:** open the page, read every panel and dialog, and get the live updates. The header says
  **View only · Sign in to edit**, which opens the sign-in page.
* **What needs the token:** every change (any `POST`, `PUT`, `PATCH` or `DELETE`: editing the path, adding or removing
  a target, device setup, a traceroute, discovery, the server alert test), and two reads that give away more than the
  dashboard shows: the path export (`/api/path/export`) and anything under a device's `credentials`. FastAPI's
  `/docs` are not shown to viewers either.
* **Clients:** under the default `privacy.client_details: token`, a viewer gets the masked client list (vendor and the
  last four MAC digits, no IP or MAC); signed in, the full one ([clients.md](clients.md)).
* **The page:** the edit controls (Edit on Path health, Target, Trace, the device Setup buttons, the server alert test)
  are shown dimmed with the tooltip "View only · Sign in to edit", and clicking one says so instead of opening its
  dialog. The page learns it from `/api/info` (`can_edit`), but the server enforces it whatever the page does.
* **The live connection** is receive-only for a viewer: the server drops anything a viewer's browser sends on it (the
  dashboard sends nothing in any case).

Signing in from the header gives the full dashboard; signing out (or a sign-in that ends) goes back to the sign-in
page, and the dashboard opens view only again from there. A wrong or expired cookie is treated as no token: view
only. With the setting off, nothing of this applies: the token protects every page and request.

Viewers can see your network's addresses, hop names and history, the LAN address of this computer and the names of
your alert channels. Turn it on only where that is fine for everyone who can reach the port.

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

`docker-compose.yml` passes `PENGUMON_ACCESS_TOKEN`, `PENGUMON_ALLOWED_HOSTS`, `PENGUMON_SESSION_MAX_AGE_DAYS`,
`PENGUMON_VIEW_WITHOUT_TOKEN`, `PENGUMON_NOTIFY_URL`,
`PENGUMON_NOTIFY_TOKEN` and the modem/router passwords on from the git-ignored `.env` (see `.env.example`).
