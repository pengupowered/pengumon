# 🔒 Security

The API and dashboard are guarded by a request middleware (the vendored `homeguard` package under `src/pengumon/_vendor/homeguard/`, with
this app's profile in `security.py`): the `Host` header must be an IP
address, `localhost`, a single-label name, a `.local/.lan/.home.arpa/.internal` name or listed in
`PENGUMON_ALLOWED_HOSTS` (blocks DNS rebinding); state-changing requests must be same-origin; request bodies
are size limited; the websocket checks Host, Origin and the password. Responses carry a strict CSP (scripts from
`'self'` only, no inline scripts). `data/secrets.json` is written atomically with mode 0600. The traceroute, credential,
router-traffic, report, incident dossier (20 a minute, one built at a time), incident inspector (60 a minute) and backup
(2 in 10 minutes) endpoints are rate limited (a report answer already built this minute is free; at most two are
built at once), and a dashboard's request to resend the whole live state is served to that connection alone, at most
once every 2 s. No external program is started for probing: ping and traceroute are sent by the
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
apply at once, without Save. **Change** and **Remove** ask for the current password first, so a browser left signed in
(or a copied sign-in cookie) cannot take the dashboard over or open it to everyone; a wrong one counts as a failed
sign-in (below). So do making, replacing or removing the read token, and turning **Others may look** on (a box under
the switch asks for it, and Save sends it), also when the password comes from `PENGUMON_ACCESS_TOKEN` (type that one):
such a browser cannot leave access behind that outlives it either. After a new password, nothing made with an old
sign-in still works: every other browser is signed out and the read token is removed. A new password is shown once, with Copy, and never again: lost, it is replaced (the steps under "Locked
out?" below). A new password signs every other browser out (it is part of the key of the sign-in cookies); removing it
opens the dashboard to everyone again. Every change also closes the live connection of every open dashboard, which
reconnects by itself under the new rule (a browser without the new password lands on the sign-in page).

**The read token** (Settings > Sign-in > Read token, with a password set): a second, random token for an AI assistant
([MCP](mcp.md)) or a script. Sent as `Authorization: Bearer pgr_...`, it reads what a viewer may read (below), whether or
not viewing is on, and may also call the read-only MCP endpoint (`POST /mcp`); it can change nothing (403), sign nobody
out and is no password for the sign-in page. Client names and addresses are masked for it. It is shown once, with Copy;
**Replace** makes a new one (the old one stops working at once), **Remove** ends it; each asks for the current password.
A new password, and removing the password, remove the read token too (make a new one): someone who knew the old password
may have made it. **Sign out everywhere** leaves it alone: it is no sign-in, and a signed-in browser alone cannot make
one. Nor does a new `PENGUMON_ACCESS_TOKEN` (Pengumon cannot tell that the variable changed): when you change the
variable because someone may know the old one, replace or remove the read token too. Failed sign-ins (below) never
lock it out: it is random, so a correct one is always checked. Use it instead of the password wherever a tool keeps the
token in a file.

**Where the password lives:** in `secrets.json` in the data folder (mode 0600, next to the device passwords), as an
scrypt hash (`access_hash`), never as text, and never in the database, the settings export or the log. The file also
holds `session_secret` (a random key for the sign-in cookies, so a copied cookie cannot be used to guess the password
offline) and, when there is one, `read_token_hash` (the read token's SHA-256). A password stored as text by an earlier
version is replaced by its hash at the first start of this one (logged once). Next to the hash, `access_token` holds a
placeholder: an earlier version rolled back onto this file finds a password nobody knows, so the dashboard stays locked
instead of opening (follow "Locked out?" below, or restore `secrets.json` from the backup taken before the upgrade).
`PENGUMON_ACCESS_TOKEN` wins over all of it (containers and scripts): the page then says the password comes from the
environment and cannot change it; that password stays as text where you set it.

**Failed sign-ins** (a wrong token on the sign-in page, as `Authorization: Bearer`, or as the current password) are
counted per address, an IPv6 address by its /64: after 5 in a minute, that address's tokens are not even checked for a
minute (429). After 20 in a minute from all addresses together, addresses that have not signed in within a day are
refused the same way, so changing addresses does not multiply the guesses. A browser that is signed in is never refused,
nor is the read token, and an address that signed in recently is exempt from the 20, so someone guessing elsewhere cannot
lock you out; a device that never signed in has to wait while such guessing goes on. The sign-in page takes its own form
only: a page on another site that you happen to open cannot submit guesses from your browser (403, not counted), so it
cannot use the count to lock your devices out. If several devices reach Pengumon through one address (a NAT, or a
container setup that shows every client as the same address), they share the count.

**Locked out?** Shell access to the computer Pengumon runs on is the proof that you own it:

1. Either remove the `"access_hash": {...}` and `"access_token": "..."` entries from `secrets.json` in the data folder
   (`pengumon paths` prints where it is; in a container it is `/app/data/secrets.json` in the data volume; both: with
   only the hash gone it stays locked, and the log says to remove the other), or set
   `PENGUMON_ACCESS_TOKEN` to a password of your choice for the service or container. Remove `"read_token_hash"` as
   well if you are not sure who has the read token (with the variable it would keep working; a password set later on
   the page removes it anyway).
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
* `PENGUMON_TRUSTED_PROXIES`: the addresses of a reverse proxy (comma separated; networks such as `10.0.0.0/8` too)
  whose `X-Forwarded-For` (the client's address) and `X-Forwarded-Proto` Pengumon believes. Unset (the default),
  both headers are ignored, from every address: every program on this computer connects from `127.0.0.1`, and if
  that address were believed, any of them could choose the address its failed sign-ins and the rate limits are
  counted under (a new one for every guess, or yours, to lock you out). `*` is refused at start.

In the compose files only the variables listed under `environment:` reach the container (`docker-compose.yml` lists
`PENGUMON_ACCESS_TOKEN`, `PENGUMON_ALLOWED_HOSTS`, `PENGUMON_SESSION_MAX_AGE_DAYS`, `PENGUMON_VIEW_WITHOUT_TOKEN`,
`PENGUMON_TLS_CERT` and `PENGUMON_TLS_KEY` among them). For any other variable above, such as
`PENGUMON_TRUSTED_PROXIES`, `PENGUMON_ALLOWED_ORIGINS` or `PENGUMON_COOKIE_SECURE`, add a line to the `environment:` list of an
override file (`docker-compose.override.yml` next to `docker-compose.yml`, `compose.override.yaml` next to `compose.yaml`);
compose merges the lists.

## Downloading a backup

`GET /api/backup` (Settings > About this install) sends a copy of the database file: the whole history and the
settings, no password, token or webhook address (those are in `secrets.json`, which it never includes). Only someone who
may change settings can download it: the password (cookie or Bearer), or everyone when no password is set. A viewer
gets 401 and the read token 403; the path is in the viewer policy's deny list and the handler checks again itself. At
most 2 in 10 minutes, one at a time ([storage.md](storage.md#backups)).

## What Pengumon reads, and how much

Everything that answers Pengumon is untrusted, a device on your network included, so each answer has a limit:

* **Size and time.** Every HTTP answer from a device (modem, router), a discovery target, a service check, ntfy, a
  webhook or a speed test server is read through a reader with a size limit (for example 16 KiB for an ntfy or webhook
  answer, which is not used; the router's limits are in [devices.md](devices.md)) and a total time limit for the whole
  exchange (the connect timeout plus three read timeouts: a server that sends a byte just inside each timeout cannot hold
  a poll for ever). No compression is asked for, and an answer is never decoded past its limit. The parsers of these
  texts (the Xfinity status page, the page titles discovery reads, the redaction rules) take time in proportion to the
  text, not its square.
* **Redirects.** A webhook follows at most 3, within its own host, and never from `https` to plain `http`; the
  Xfinity driver follows only on the configured host and never from `https` to `http`; the path's HTTP probes and the
  speed test follow none ([webhooks.md](webhooks.md), [devices.md](devices.md)).
* **The live connection.** At most 64 dashboards at once (one more is closed with code 1013), and a frame a page sends
  can be at most 4096 bytes.
* **The container.** The compose files drop every capability but `NET_RAW`, limit memory to 512 MB and processes to 256,
  and run with a read-only root file system. The image carries the raw-socket capability but not the tool that sets it
  (`libcap2-bin` is installed and removed again in the build).
* **Logs and traces.** The records and spans sent over OpenTelemetry name a device by its id and driver, never by its
  address or the login name Pengumon uses; the page and the API keep the full error text, so you still see which address
  failed ([telemetry.md](telemetry.md)).

## HTTPS

Plain HTTP is fine on a home network you trust: the dashboard is meant for your own devices, and the password is never
sent anywhere but to Pengumon. HTTPS is optional, and Pengumon never turns it on by itself:

* **Your own certificate.** Set `PENGUMON_TLS_CERT` to a PEM certificate (with its chain) and `PENGUMON_TLS_KEY` to its
  key (without a passphrase); both are read at start. Pengumon then speaks HTTPS only, on the same port, the
  session cookie is marked `Secure`, and every answer carries `Strict-Transport-Security: max-age=31536000` (browsers
  then use HTTPS for this address for a year; no subdomains, no preload list). Only one of them set, a missing file or a key that does not match stops the start
  with a message that says which. Good sources: a home certificate authority your devices already trust, the
  certificates Tailscale makes for your tailnet names, or one for a real domain. Renewing it means restarting Pengumon.
  `pengumon service status` still checks the plain-HTTP address, so it reports a service with HTTPS as not answering.
* **A reverse proxy** (Caddy, nginx, Traefik) in front, which holds the certificate and passes requests to Pengumon on
  `127.0.0.1:8000`. Add the name it serves under to `PENGUMON_ALLOWED_HOSTS` and its origin
  (`https://pengumon.example`) to `PENGUMON_ALLOWED_ORIGINS`, set `PENGUMON_COOKIE_SECURE=1` (which also sends
  `Strict-Transport-Security`), and let the proxy pass websockets (`/ws`). Caddy, for example: `pengumon.example { reverse_proxy 127.0.0.1:8000 }`.
  Every request then comes from the proxy's address, so failed sign-ins are counted for all its clients together
  (one guesser throttles them all; signed-in browsers and the read token keep working). To count them per client,
  set `PENGUMON_TRUSTED_PROXIES` to the proxy's address (`127.0.0.1` when it runs on this computer), and only where
  no other program on that computer is untrusted: each of them could then claim any address.

**Why Pengumon does not make a certificate of its own:** a certificate that Pengumon signed itself is trusted by no
browser, so every browser on every device would show a full-page security warning ("Your connection is not private")
and keep showing it. Teaching people to click through that warning does more harm than plain HTTP on a home network.

## Without a password

With no password, any device that can reach the port can change settings, including the probe path: it can remove hops
and delete a removed hop's history (from 0.19 on, also the hops that came from `config.yaml`, which the API could not
remove before). The default listens on this computer only, which is safe. If you open the dashboard to your network and
other people's devices share it (guests, children's computers, smart-home gadgets), set a password in Settings >
Sign-in ([Opening the dashboard from other devices](install.md#opening-the-dashboard-from-other-devices)).

## What the path's TCP and HTTP probes can reach

A hop probed by TCP or HTTP ([probing.md](probing.md#tcp-and-http-probes)) makes this computer open a connection to an
address a person typed in the path editor, so whoever can edit the path (by default anyone who reaches the dashboard)
can make it connect to any public, private-network or carrier-grade address. For hosts on your network that adds
nothing: that person can connect to them directly. What it would add is this computer's own view: its loopback
(unauthenticated admin interfaces, Pengumon itself) and link-local addresses (a cloud host's metadata service at
169.254.169.254). Those are refused, as a literal when the path is edited and again on the address a name resolves to at
every probe, which also defeats a name that is made to point at 127.0.0.1. This computer's own LAN address is a private
address like any other and is not refused; Settings, Service checks watch this computer's own apps.

The load on other hosts is bounded: at most 8 hops use TCP or HTTP, one connection each per second; a TCP probe sends no
data; an HTTP probe sends one fixed `HEAD` or `GET` to a fixed path with an honest `User-Agent: Pengumon/<version> (path
probe)` (no contact details); ports on the Fetch standard's bad-port list (mail, login, file sharing, IRC) are refused,
so Pengumon cannot be pointed at those services. Nothing is stored or sent that could hold a secret: a hop has no user
name, password or query string field, no cookies are sent, a redirect is not followed, a proxy variable of the
environment is ignored, and an error text only ever holds Pengumon's own fixed words with the host and port. Only the
status line (at most 1024 bytes) of an answer is read, and then the connection is dropped; Test returns the status
code and the timings, never headers or a body, so the test route cannot be used to read a page. TLS is always
verified; there is no switch to turn that off. The test route can tell a closed port from a silent one, but only for
addresses the person could reach anyway; it is limited to 30 a minute and refuses loopback.

## View only without the password

Some households want the dashboard on a shared screen, or on everyone's phone, without handing out the password that
also changes settings. With a password set and **Others may look, but not change anything** on (Settings > Sign-in, or
`PENGUMON_VIEW_WITHOUT_TOKEN=1`), a browser without the password sees the whole dashboard, live, but cannot change
anything:

* **What a viewer can do:** open the page, read every panel and dialog, and get the live updates. The header says
  **View only · Sign in to edit**, which opens the sign-in page.
* **What needs the password:** every change (any `POST`, `PUT`, `PATCH` or `DELETE`: editing the path, adding or removing
  a target, device setup, a traceroute, discovery, the server alert test, "Show" in Settings > Sign-in), and two reads
  that give away more than the dashboard shows: the path export (`/api/path/export`), the database backup
  (`/api/backup`: the whole history; [storage.md](storage.md#backups)) and anything under a device's `credentials`. The Settings page a viewer opens shows the lists without a device's user name and options (mesh
  labels), a check's path and query, a webhook's headers and the host of a secret address; a kept version of the
  settings (`/api/settings/versions/{n}`) or of the path (`/api/path/versions/{n}`) needs the password. The same holds
  elsewhere: the alert channels come without their hosts (`/api/notifications`), the version list of the settings
  without the names of header secrets, a device's address without its path and a failed device poll in fixed words
  ("The device refused Pengumon's sign-in"), not the device's own message, which can name the login it tried.
  FastAPI's `/docs` are not shown to viewers either. The read token gets the same.
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

## The MCP endpoint

`POST /mcp` ([mcp.md](mcp.md)) lets an AI assistant read the network's status, incidents, reports, device summaries, host app checks,
speed tests and the AI pack. It is off by default (`mcp.enabled`, Settings > Sign-in) and read-only. It sits behind the same guards as the
API: the `Host` allow-list, the `Origin` check (a foreign one is `403`; non-browser clients send none), the body limit,
the read token (or the password) as `Authorization: Bearer ...` (open while no password is set), and a rate limit of 120 a minute (the `ai_pack` tool: 6 builds a minute, as for the API). Viewers (see above) cannot use
it without the read token. No secret, client name or client address is in any answer, and no device address or login: a failed device poll is
told in fixed words, not the driver's message. While it is off, `/mcp` is a plain `404` to every method. Over plain HTTP the bearer token
crosses the LAN in clear: use [HTTPS](#https) if that matters.

## Signing out

With a password set, a browser signed in (through `/login`, or by setting the password) shows **Sign out** in the header (in the "more" menu on a
phone). Its dialog has two choices:

* **Sign out**: ends the sign-in of this browser (`POST /logout`). The server refuses that cookie from then on,
  so a copy of it stops working too.
* **Sign out everywhere**: after a confirmation, ends the sign-in of every browser and device at once, this one
  included, without changing the password (`POST /logout?everywhere=1`). Use it when a device was lost or a sign-in
  was shared.

Neither touches the read token, which is no sign-in (Settings > Sign-in removes or replaces it; a new password removes
it). Both are kept in `data/sessions.json` (in the data volume, mode 0600; it holds times and hashes, no usable
cookie), so they survive a restart. Deleting the file undoes them. If the file cannot be read, every sign-in is
refused until it is fixed or deleted (the log says so); scripts that send `Authorization: Bearer` are not affected
by any of this.

A sign-in also ends on its own after `PENGUMON_SESSION_MAX_AGE_DAYS` (at least 1 hour), and every sign-in ends when
the password changes (except the browser that changed it on the Settings page, which gets a new sign-in). A dashboard left open in a browser whose sign-in ended goes to the sign-in page by itself (its live
connection is closed, or its next request refused). Sign-ins from before this version (the old cookie format, keyed
with the password itself) are refused: sign in once more after the upgrade.

**Rolling back** to a version from before the password hash: restore `secrets.json` from the backup taken before the
upgrade (`pengumon backup`, or the folder the install script or `scripts/deploy.sh` made) together with the database.
The upgrade replaced the stored password by a hash, which an older version cannot read; left alone, it finds a password
nobody knows and the dashboard stays locked (see "Locked out?" above). With the old file back, sign-ins made before the
upgrade, a copied cookie included, work again: change the password after such a rollback. Rolling back to a version before 0.24 drops a password set on the page (those versions read only
`PENGUMON_ACCESS_TOKEN`): the dashboard is then open until you set the variable.

`docker-compose.yml` passes `PENGUMON_ACCESS_TOKEN`, `PENGUMON_ALLOWED_HOSTS`, `PENGUMON_SESSION_MAX_AGE_DAYS`,
`PENGUMON_VIEW_WITHOUT_TOKEN`, `PENGUMON_NOTIFY_URL`,
`PENGUMON_NOTIFY_TOKEN` and the modem/router passwords on from the git-ignored `.env` (see `.env.example`).
