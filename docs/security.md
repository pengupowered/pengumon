# 🔒 Security

The API and dashboard are guarded by a request middleware (the vendored `homeguard` package under `src/pengumon/_vendor/homeguard/`, with
this app's profile in `security.py`): the `Host` header must be an IP
address, `localhost`, a single-label name, a `.local/.lan/.home.arpa/.internal` name or listed in
`PENGUMON_ALLOWED_HOSTS` (blocks DNS rebinding); state-changing requests must be same-origin; request bodies
are size limited; the websocket checks Host, Origin and the password. Responses carry a strict CSP (scripts from
`'self'` only, no inline scripts). `data/secrets.json` is written atomically with mode 0600. The traceroute and
credential endpoints are rate limited. No external program is started for probing: ping and traceroute are sent by the
app itself, to IPv4 or IPv6 addresses, after the host is validated.

## Who can open the dashboard

There are two ways, and you can switch between them at any time in **Settings > Sign-in**:

* **Anyone on my home network** (no password). Anyone who can reach the port sees your network's details and can change
  settings. Fine for most homes, and the default.
* **Only people with a password.** Every request except `/api/health`, `/login` and `/static/` needs it: a browser
  signs in once on the sign-in page (which calls it the "access token") and stays signed in; scripts send it as
  `Authorization: Bearer ...`. Optionally, **Others may look, but not change anything**
  ([View only](#view-only-without-the-password)).

**The first start asks.** When no password is set anywhere, the first person to open the dashboard is asked
**Who can open this dashboard?**, with "Anyone on my home network" chosen. It is asked once: whoever answers first
decides, and Settings > Sign-in changes it later. Choosing a password signs that browser in at once (nobody is locked
out by their own choice) and shows the password once, with Copy; Pengumon proposes one (16 letters and digits in four
groups, without look-alikes such as 0 and o), and you can type your own (at least 8 characters). An install whose password comes from `PENGUMON_ACCESS_TOKEN` is never asked.
An install upgraded from before 0.24 without a token is asked once, at its first start after the upgrade.

**Settings > Sign-in** shows which way is in force and offers **Set a password**, **Change** and **Remove**. These
apply at once, without Save. A new password is shown once, with Copy, and never again: lost, it is replaced (Change, in
any browser still signed in, or the steps under "Locked out?" below). A new password signs every other browser out (the
password is the key of the sign-in cookies); removing it opens the dashboard to everyone again. Every change also
closes the live connection of every open dashboard, which reconnects by itself under the new rule (a browser without the
new password lands on the sign-in page).

**Where the password lives:** in `secrets.json` in the data folder (`access_token`, mode 0600, next to the device
passwords), never in the database, the settings export or the log. `PENGUMON_ACCESS_TOKEN` wins over it (containers
and scripts): the page then says the password comes from the environment and cannot change it.

**Locked out?** Shell access to the computer Pengumon runs on is the proof that you own it:

1. Either remove the `"access_token": "..."` entry from `secrets.json` in the data folder (`pengumon paths` prints
   where it is; in a container it is `/app/data/secrets.json` in the data volume), or set `PENGUMON_ACCESS_TOKEN` to a
   password of your choice for the service or container.
2. Restart Pengumon (its service, e.g. `systemctl --user restart pengumon`, or `podman restart pengumon`). It reads
   the password at start only. Without a password the dashboard is open to your network again (set a new one in
   Settings > Sign-in); with the variable, sign in with what you set.

If `secrets.json` cannot be read (a damaged file) or is missing (a lost data volume, a different data folder) while a
password was set, Pengumon does not open the dashboard to everyone: nobody can sign in until the file is fixed or
restored, or `PENGUMON_ACCESS_TOKEN` is set, and the log says so at start. To start over without a password instead,
write `{}` into `secrets.json` there (a file without the password, as in step 1) and restart.

## Settings

The sign-in itself is on the Settings page (above). These are environment variables:

* `PENGUMON_ACCESS_TOKEN`: the password, set where Pengumon is started. It wins over the one set on the page.
* `PENGUMON_VIEW_WITHOUT_TOKEN`: `1` turns on "Others may look, but not change anything" (Settings > Sign-in), `0`
  turns it off; either wins over the page. Unset, the page decides (off by default). It does nothing without a
  password.
* `PENGUMON_TLS_CERT` and `PENGUMON_TLS_KEY`: HTTPS with your own certificate ([HTTPS](#https)).
* `PENGUMON_ALLOWED_HOSTS`: extra host names, comma separated (`.example.net` also matches subdomains).
* `PENGUMON_ALLOWED_ORIGINS`: extra origins allowed for state-changing requests and the websocket.
* `PENGUMON_MAX_BODY_BYTES`: the request body limit (default 262144).
* `PENGUMON_COOKIE_SECURE`: `1` when a reverse proxy serves the dashboard over HTTPS (marks the session cookie
  `Secure`; with `PENGUMON_TLS_CERT` it is marked anyway).
* `PENGUMON_SESSION_MAX_AGE_DAYS`: how long a sign-in lasts, enforced by the server. Default 30; at least 1 hour
  (0.0417), at most 400; fractions allowed.

## HTTPS

Plain HTTP is fine on a home network you trust: the dashboard is meant for your own devices, and the password is never
sent anywhere but to Pengumon. HTTPS is optional, and Pengumon never turns it on by itself:

* **Your own certificate.** Set `PENGUMON_TLS_CERT` to a PEM certificate (with its chain) and `PENGUMON_TLS_KEY` to its
  key (without a passphrase); both are read at start. Pengumon then speaks HTTPS only, on the same port, and the
  session cookie is marked `Secure`. Only one of them set, a missing file or a key that does not match stops the start
  with a message that says which. Good sources: a home certificate authority your devices already trust, the
  certificates Tailscale makes for your tailnet names, or one for a real domain. Renewing it means restarting Pengumon.
  `pengumon service status` still checks the plain-HTTP address, so it reports a service with HTTPS as not answering.
* **A reverse proxy** (Caddy, nginx, Traefik) in front, which holds the certificate and passes requests to Pengumon on
  `127.0.0.1:8000`. Add the name it serves under to `PENGUMON_ALLOWED_HOSTS` and its origin
  (`https://pengumon.example`) to `PENGUMON_ALLOWED_ORIGINS`, set `PENGUMON_COOKIE_SECURE=1`, and let the proxy pass
  websockets (`/ws`). Caddy, for example: `pengumon.example { reverse_proxy 127.0.0.1:8000 }`.

**Why Pengumon does not make a certificate of its own:** a certificate that Pengumon signed itself is trusted by no
browser, so every browser on every device would show a full-page security warning ("Your connection is not private")
and keep showing it. Teaching people to click through that warning does more harm than plain HTTP on a home network.

## Without a password

With no password, any device that can reach the port can change settings, including the probe path: it can remove hops
and delete a removed hop's history (from 0.19 on, also the hops that came from `config.yaml`, which the API could not
remove before). The default listens on this computer only, which is safe. If you open the dashboard to your network and
other people's devices share it (guests, children's computers, smart-home gadgets), set a password in Settings >
Sign-in ([Opening the dashboard from other devices](install.md#opening-the-dashboard-from-other-devices)).

## View only without the password

Some households want the dashboard on a shared screen, or on everyone's phone, without handing out the password that
also changes settings. With a password set and **Others may look, but not change anything** on (Settings > Sign-in, or
`PENGUMON_VIEW_WITHOUT_TOKEN=1`), a browser without the password sees the whole dashboard, live, but cannot change
anything:

* **What a viewer can do:** open the page, read every panel and dialog, and get the live updates. The header says
  **View only · Sign in to edit**, which opens the sign-in page.
* **What needs the password:** every change (any `POST`, `PUT`, `PATCH` or `DELETE`: editing the path, adding or removing
  a target, device setup, a traceroute, discovery, the server alert test, "Show" in Settings > Sign-in), and two reads
  that give away more than the dashboard shows: the path export (`/api/path/export`) and anything under a device's
  `credentials`. FastAPI's `/docs` are not shown to viewers either.
* **Clients:** under the default `privacy.client_details: token`, a viewer gets the masked client list (vendor and the
  last four MAC digits, no IP or MAC); signed in, the full one ([clients.md](clients.md)).
* **The page:** the edit controls (Edit on Path health, Target, Trace, the device Setup buttons, the server alert test)
  are shown dimmed with the tooltip "View only · Sign in to edit", and clicking one says so instead of opening its
  dialog. The page learns it from `/api/info` (`can_edit`), but the server enforces it whatever the page does.
* **The live connection** is receive-only for a viewer: the server drops anything a viewer's browser sends on it (the
  dashboard sends nothing in any case).

Signing in from the header gives the full dashboard; signing out (or a sign-in that ends) goes back to the sign-in
page, and the dashboard opens view only again from there. A wrong or expired cookie is treated as no password: view
only. With the setting off, nothing of this applies: the password protects every page and request. Turning it off
closes the viewers' live connections at once.

Viewers can see your network's addresses, hop names and history, the LAN address of this computer and the names of
your alert channels. Turn it on only where that is fine for everyone who can reach the port.

## Signing out

With a password set, a browser signed in (through `/login`, or by setting the password) shows **Sign out** in the header (in the "more" menu on a
phone). Its dialog has two choices:

* **Sign out**: ends the sign-in of this browser (`POST /logout`). The server refuses that cookie from then on,
  so a copy of it stops working too.
* **Sign out everywhere**: after a confirmation, ends the sign-in of every browser and device at once, this one
  included, without changing the password (`POST /logout?everywhere=1`). Use it when a device was lost or a sign-in
  was shared.

Both are kept in `data/sessions.json` (in the data volume, mode 0600; it holds times and hashes, no usable
cookie), so they survive a restart. Deleting the file undoes them. If the file cannot be read, every sign-in is
refused until it is fixed or deleted (the log says so); scripts that send `Authorization: Bearer` are not affected
by any of this.

A sign-in also ends on its own after `PENGUMON_SESSION_MAX_AGE_DAYS` (at least 1 hour), and every sign-in ends when
the password changes (except the browser that changed it on the Settings page, which gets a new sign-in). A dashboard left open in a browser whose sign-in ended goes to the sign-in page by itself (its live
connection is closed, or its next request refused). Sign-ins from before this version (the old cookie format) are
refused: sign in once more after the upgrade.

Rolling back to an image from before this change makes old copied cookies valid again: change the password after such
a rollback. Rolling back to a version before 0.24 drops a password set on the page (those versions read only
`PENGUMON_ACCESS_TOKEN`): the dashboard is then open until you set the variable.

`docker-compose.yml` passes `PENGUMON_ACCESS_TOKEN`, `PENGUMON_ALLOWED_HOSTS`, `PENGUMON_SESSION_MAX_AGE_DAYS`,
`PENGUMON_VIEW_WITHOUT_TOKEN`, `PENGUMON_NOTIFY_URL`,
`PENGUMON_NOTIFY_TOKEN` and the modem/router passwords on from the git-ignored `.env` (see `.env.example`).
