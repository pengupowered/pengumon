# Speed test

Pengumon can measure your download and upload speed on a schedule, and the delay your line adds while it is full
(bufferbloat), and keep the results next to the latency record. It answers "is my line as fast as I pay for?" with
numbers over weeks instead of one test on a good afternoon. It is **off by default**: turn it on in Settings > Speed test.
It is meant to be light: every test has hard limits on the data it moves, and a monthly limit is one setting away.

Where the results show up: the speed card on the dashboard, the [ISP dossier](#in-the-reports) (a "Speed tests"
section), the [AI pack](ai-pack.md), the [MCP tool](mcp.md) `speed_tests`, the [metrics](telemetry.md) and the
[API](api.md).

## What a test measures, and how

A test is three steps against one provider: idle latency, download, upload. Its answer to "is my line as fast as I pay
for?" is the **line's** rate, read from your router while the test fills the line, whenever a router with WAN counters
is set up; the test's own rate otherwise. Both are kept.

- **Idle latency.** The median latency of your public probe targets (the "Internet" hops of the path) over the 30
  seconds before the test starts. Pengumon already pings them every second; nothing extra is sent.
- **Download, then upload**, one direction at a time. Each runs until data has flowed for **about 8 seconds**, or until
  its data cap (`download_mb`, `upload_mb`) is used, whichever comes first; 15 seconds is the hard limit. The HTTP
  providers (Cloudflare, LibreSpeed) run 8 parallel streams on kept connections, each asking for one request after
  another (the cap spread over the streams, each request under 10 MB; an upload's in four rounds, so each is answered
  within a second or two), and stop reading the moment the direction is done. M-Lab's ndt7 is one stream by design.
- **The line's rate, from your router.** While each direction runs, Pengumon reads the router's WAN byte counters about
  once a second, through the same device (driver and session) its regular polls use, never during one of them. The
  best rate over 3 seconds of those readings is what the line carried while the test filled it: the test's traffic and
  everything else in the house (`wan_down_mbps`, `wan_up_mbps`). [Why this is the honest answer](#why-the-routers-view).
- **The test's own rate** (`down_mbps`, `up_mbps`), what reached this computer from the server. A download's is the
  bytes between the first fifth (TCP's slow start) and the last tenth (or the last request's worth, when one request
  per stream used the whole cap: one that lags), over the time data flowed: the connection setup and the waits for
  answers to start are left out. An upload's is the rate the sockets took the bytes at between the first fifth of its
  time and the end of its 8 seconds, when the socket buffers are full and take a byte only once the server has
  acknowledged one (a short upload, under 2 s: the average up to the server's answers). [How it is
  scored](#how-the-tests-own-rate-is-scored).
- **The headline** is the router's rate when it gave one at least as high as the test's own (both are lower bounds of
  what the line can carry; one under the test's own was not the router's full view), else the test's own. The card, the
  reports, the AI pack, the MCP tool, the metrics and the slow-line alert all use it and say which it is
  (`measured_by`: `router` or `test`). When the test itself got under 70 % of what the line carried, the rest was other
  traffic in the house, not a slow line, and the result says so.
- **Latency under load.** While each direction runs, the median latency of the same public targets is taken again. The
  **added latency** is the larger loaded median minus the idle one, and the **bufferbloat grade** follows from it: A+
  under 5 ms, A under 30, B under 60, C under 200, D under 400, F beyond. The grade is computed when read, not stored.

A test is `ok` (both directions worked), `partial` (one did), `failed` (neither: a refusal, a timeout, a DNS error) or
`skipped` (not run: the monthly data limit, or a busy line). A provider that refuses (HTTP 429 or 403, usually a rate
limit) makes a failed test whose reason says so; try another provider or a longer interval. A stream that drops out
(Cloudflare now and then stalls a request, or resets one) costs nothing once the others have kept data flowing for the
whole 8 seconds; earlier, it fails the direction.

### Why the router's view

A speed test measures what reached one computer from one server. That is not the line: the line also carries the
household's traffic during the test, and the server may not fill it. Measured on a line of about 1 Gbps down / 40 up
(2026-11, four rounds over an hour): Cloudflare's free endpoint served 670 to 820 Mbps to Pengumon, 156 to 541 Mbps to
curl with eight streams on kept connections (single requests ranged from 12 to 902 Mbps), and refused requests of 10 MB
or more for over an hour after a few dozen; two streams from a Hetzner server read 650 to 760 Mbps over the whole
transfer (their own speeds added up to 760 to 920), and it refused a third and fourth stream (HTTP 429). The router counts every byte that crosses
the WAN port, whoever sent it, so while the test keeps the line full, its counters show what the line delivers. Its
10-second polls are too coarse for that (an 894 Mbps transfer of 9 s read as 558 averaged over 10 s), so during a test
the counters are read once a second.

How fresh each router's counters are, as far as the drivers can tell (not checked on every router model):

| Driver | The light read during a test | How fresh |
| --- | --- | --- |
| `asuswrt` | one `appGet.cgi` request with the `netdev()` hook only, on the polls' session (the router allows one admin session) | read from the interface statistics when asked, as far as the driver can tell; not established on hardware-accelerated firmware |
| `openwrt` | one ubus batch: the interface dump and the device statistics | the kernel's counters when asked; hardware flow offloading (some MediaTek routers) can leave forwarded traffic out of them |
| `snmp` | one GET of `sysUpTime` and the WAN interface's octet counters | many agents refresh every few seconds (net-snmp caches its interface table): handled, see below |
| `upnp_igd` | the two counter actions (`GetTotalBytesReceived`, `GetTotalBytesSent`) | usually read when asked (miniupnpd does); 32-bit counters wrap every 34 s at 1 Gbit/s, which a read a second follows |

Counters that change only every few reads are measured between the readings where they changed, over a window of about
2.5 of their steps (at least 3 s), so their timing errs little. A counter that goes back is a wrap when the driver knows
its width (32-bit UPnP and SNMP), else a router restart, and is never read as traffic; a read that fails is a gap the
next one covers. A device that must not be read often (the cable modem) is never read this way.

### How the test's own rate is scored

Compared on real runs (2026-11, a 1 Gbps line, Cloudflare): the aggregate over the steady part of a direction read
670 to 820 Mbps; Cloudflare's own method, the 90th percentile of the speed of each request's body, reads one stream's
speed (270 to 310 Mbps), and times eight streams it reads 2,200 to 2,500 Mbps, more than the line. With parallel streams
only the aggregate means anything, so Pengumon takes it, over the time data flowed; a request that stalls is absorbed by
the other streams and cut off when the 8 seconds are up.

### How fast the client is

The HTTP providers read through their own small HTTP/1.1 transport: it waits a few milliseconds between reads of a
response and then takes everything the socket holds, decrypting it in OpenSSL, instead of handling every few kilobytes
that arrive on their own. CPU per MB downloaded (user and system, this machine, an i9-10900X): through httpx's own
transport 13.1 ms on loopback TLS with data arriving 16 KiB at a time and 9.7 to 10.3 ms from Cloudflare (one core is
spent at about 800 Mbps); through the new one 5.6 ms on the same loopback test and 5.7 to 5.8 ms from Cloudflare. No proxy is used for
the test, by any provider (the transport does not read proxy settings, and M-Lab's server lookup and WebSocket are told
not to): a test through a proxy would measure the proxy, not your line.
On a network that lets traffic out only through a proxy (`HTTPS_PROXY` set for Pengumon), the test therefore fails,
and its reason says so.

One test is one set of connections to one server, on a line that is also carrying your household's traffic. A result a
little under the plan speed is normal; **repeatedly** slow results are the finding.

## What is sent where (privacy)

Each test moves bytes in both directions to **one** provider, chosen in Settings. Nothing else leaves your network. Every
request says `User-Agent: Pengumon/<version>`; redirects are not followed.

| Provider | What it sees | What it publishes |
| --- | --- | --- |
| **Cloudflare** (the default, `speed.cloudflare.com`) | Your public IP address and the transfer (`GET /__down`, `POST /__up`), as for any visit to its site, under Cloudflare's own privacy policy. The result names the Cloudflare location that answered (for example `AMS`). | Nothing about you from Pengumon's side. |
| **M-Lab NDT7** (opt-in) | A locate request first (`locate.measurementlab.net`) finds the nearest M-Lab server; the server then sees your public IP address and the transfer. | **Every result is published, with your public IP address, as open data** by Measurement Lab: [privacy policy](https://www.measurementlab.net/privacy/), [acceptable use policy](https://www.measurementlab.net/aup/). Choose it only if you are fine with that. |
| **Your LibreSpeed server** (`librespeed_url`) | Only your own server sees anything. | Nothing. |

Pengumon's reports and the AI pack name a LibreSpeed server as "your LibreSpeed server", never by its address; a Cloudflare
location or an M-Lab machine name may appear, since they say nothing about you.

### Your own LibreSpeed server

The base URL is the folder that holds `garbage.php` and `empty.php`. For the official Docker image
(`ghcr.io/librespeed/speedtest`) in standalone mode that is **`http://<host>/backend`, not `http://<host>`**: the bare
host answers 404. The Go backend accepts the same paths. A URL with credentials, or without `http://` or `https://`,
is refused.

The requests say `User-Agent: Pengumon/<version>`. No redirects are followed. A refusal (an HTTP 429 or 403 from the
provider, usually a rate limit) is a failed test whose reason says so; try another provider or a longer interval.
Cloudflare rate-limits large requests: after a few dozen requests of 10 MB or more within minutes from one address it
refused every such request (403, then 429) for more than 45 minutes, while smaller ones were still served (seen
2026-11). Pengumon asks for less than 10 MB per request, so a test stays clear of that limit.

## Data use

A test moves at most `download_mb` down and `upload_mb` up (decimal megabytes; a hard cap, enforced as the data flows,
not only requested), and stops a direction once data has flowed for about 8 seconds (15 at most), so a line slower than
the cap's pace moves less: 8 seconds of a 300 Mbps line is about 300 MB. The table is the most a month can use (30
days), by interval and size, as download + upload per test:

| Run every | 10 + 5 MB (15 MB) | 25 + 10 MB (35 MB, the default) | 100 + 25 MB (125 MB) | 250 + 50 MB (300 MB) | 1000 + 50 MB (1,050 MB) |
| --- | --- | --- | --- | --- | --- |
| 24 hours (30 tests) | 0.45 GB | **1.05 GB** | 3.8 GB | 9 GB | 31.5 GB |
| 12 hours (60) | 0.9 GB | 2.1 GB | 7.5 GB | 18 GB | 63 GB |
| 6 hours (120) | 1.8 GB | 4.2 GB | 15 GB | 36 GB | 126 GB |
| 3 hours (240) | 3.6 GB | 8.4 GB | 30 GB | 72 GB | 252 GB |
| 1 hour (720) | 10.8 GB | 25 GB | 90 GB | 216 GB | 756 GB |
| 1 week (4.3) | 64 MB | 150 MB | 540 MB | 1.3 GB | 4.5 GB |
| 30 days (1) | 15 MB | 35 MB | 125 MB | 300 MB | 1.05 GB |

Small caps measure a slow line well and a fast one poorly: 15 MB moves in a blink on a gigabit line, so the ramp-up
dominates it. To measure a line for the full 8 seconds, the download cap needs about its speed in Mbps (1,000 MB for
a gigabit line, 500 for 500 Mbps); the upload choices (up to 50 MB) cover 8 seconds up to 50 Mbps. Pick the cap for the
line, then the interval for the data you can spare. With the plan speed set (Your line), the estimate in Settings counts
what 9 seconds of it move when that is less than the cap.

**Monthly limit** (`monthly_cap_mb`, 0: none). Once this calendar month's tests (in the time zone under Display) have
moved that much, scheduled tests are skipped (recorded as `skipped`, once per slot), and **Run now** is refused. The count
is the bytes tests actually moved, read from the record.

## The schedule

- Tests run at slots every `interval_hours`: any whole number of hours from 1 to 8,760 (365 days). Settings shows it as
  a number and a unit (hours, days or weeks): a week for weekly, 30 days for about monthly. The slots are counted from
  the epoch and shifted by an offset chosen at random once per install (kept in the database), so installs do not all
  test on the hour; for an interval of two days or more the offset also picks the install's day within it, so weekly
  installs do not all test on the same weekday. The time of day stays the same (in UTC: a daylight-saving change moves
  it by an hour on the local clock).
- The next scheduled test is the first slot after the last recorded test (a manual test counts; a test skipped for the data
  cap counts). **A restart or an update never causes an extra test.** After the feature has been off, or the app down,
  for more than an interval, only the current slot is due: missed ones are not caught up.
- With a window (`window_start` to `window_end`, in the time zone under Display; it may cross midnight), scheduled tests
  run only inside it. Without one, any time.
- **A busy line waits.** Before a scheduled test, if the line's load is at or above `busy_load_pct` (default 20 %) the test
  waits 10 minutes and looks again, up to 3 times, then the slot is recorded as skipped ("line busy"). An unknown load (no
  router with WAN counters) does not block a test.
- Only one test runs at a time. **Run now** (the card, or `POST /api/speedtests/run`) ignores the window and a busy line,
  not the monthly limit, and is refused while a test runs.

## Tests are not outages

A speed test fills the line on purpose, so its delay and loss must not open incidents. While a test runs, and for the
judged window after it (the classifier looks back over its last probes, so loaded ones are still in it), a delay or loss
fault is shown as **"Speed Test Running: Delay Expected"** (a busy verdict, `busy_speedtest`) instead of an incident. A
real outage (everything down) still counts: only the delay the test causes is excused. The outage-rules preview applies
the same mask to the stored tests. See [diagnosis.md](diagnosis.md#while-a-speed-test-runs).

## The slow-line alert

Off by default (`speedtest.slow_alert_pct`, 0). Set it to a share of your plan speed, say 50, and Pengumon sends one
notification (ntfy and your webhooks, event `speed_slow`, [webhooks.md](webhooks.md#speed_slow)) when a test that worked
measures **under** that share in download or upload. It judges the headline: the line's rate your router measured when
it gave one (so other traffic in the house does not make a slow line), else the test's own; the message says which,
and when the test itself got far less than the line carried. It needs the plan speed (**Your line**, Settings:
`line.down_mbps`, `line.up_mbps`); without it there is nothing to compare with, and no alert.

- Once, not every time: the next slow test sends nothing until a test has been above the line, then the next slow one
  sends again. This is kept across restarts.
- Only tests with status `ok` are judged; partial, failed and skipped ones neither alert nor re-arm. A direction too
  short to measure the line (under a second) judges nothing either way.
- Quiet hours apply (it waits for the end of them, like a route change), the severity filter does not.
- It is only an alert: no incident is opened and nothing is marked in the history.

## In the reports

- **ISP dossier.** A "Speed tests" section when the window has tests: how many (ok, partial, failed, skipped), median and
  lowest download and upload against the plan ("median 212 of 300 Mbps (71 %)"), how they were measured (by the router,
  by the test alone, and how often the household's traffic took a large share), how many tests fell under 50 % of the
  plan, and the median latency added under load with its grade. The speeds are the headline's. One line goes into the
  support-ticket summary. Nothing appears when there are no tests.
- **AI pack.** A "Speed tests" section with the same summary and the newest 12 (30 in the detailed pack) tests that ran as
  a table, with a *By* column (router or test); see [ai-pack.md](ai-pack.md).
- **MCP.** The read-only tool `speed_tests` ([mcp.md](mcp.md)).

## What a test records

The test's own speeds (Mbps, measured at the application level over eight parallel streams, over the time data flowed;
see [above](#what-a-test-measures-and-how)), the line's peak rate the router measured during each direction
(`wan_down_mbps`, `wan_up_mbps`; none without a router that reports WAN counters, and none for a direction that
failed), how far they can be trusted (below), the server, the bytes moved, and **latency under load**: the median
latency of the public probe targets in the 30 seconds before the test (idle) and during the download and the upload.
The headline and `measured_by` are worked out when read, from these, like the bufferbloat grade (A+ under 5 ms added, A
under 30, B under 60, C under 200, D under 400, F beyond). Tests recorded by 0.25.0 kept, as their WAN rate, the peak of
the 10-second polls; where it is under the test's own rate (as it usually is), the test's own stays the headline.

A direction that lasts under a second is too short to measure a fast line: it is about as long as the server takes to
start some of its answers, and such tests scatter (on a line Cloudflare served at about 600 Mbps, 25 MB tests read 500
to 840 Mbps, 100 MB ones 575 to 665). Such a test has `confidence: low` and a note that says how much data per test
would measure that line: a second's worth (speed in Mbps / 8 MB, as the next choice up: 25 MB for 100 to 200 Mbps,
50 MB to 400 Mbps, 100 MB to 800 Mbps, 250 MB to 2,000 Mbps, then 500 and 1,000). Its headline is the test's own figure:
the router's window saw mostly the time around so short a test. A direction that ran its 8 seconds is long enough.

Turning the speed test off while one runs stops it within a second; it is recorded as failed, with the bytes it moved.

While a test runs, and for the judged window after it, the classifier does not call the test's own delay an incident:
[diagnosis.md](diagnosis.md#while-a-speed-test-runs).

The record keeps: start time, duration, trigger (scheduled or manual), provider and server label, status and reason,
download and upload Mbps, idle and loaded latency, bytes moved each way and the WAN peaks. Rows older than the rollup
retention (`database.rollup_retention_days`, but at least 32 days, so this month's data use stays whole) are deleted. The table is in
[storage.md](storage.md#the-speed-tests-schema-v17).

## On the dashboard

The **Speed** card ([dashboard.md](dashboard.md#panels)) shows the last result against your plan (the headline: under the
figures, "Line, measured by your router" with the test's own figure small under each, or "Test only"), the bufferbloat
grade, a 7 or 30 day history, the next test and this month's data use, and has **Run now**, which asks first and says how
much data the test will use at most. Settings > Speed test shows a live estimate under the data fields: "About 1.1 GB a month (daily, 25 MB
down + 10 MB up)." It is 30 days of tests at the interval, each at its largest sizes (a window or a busy line only make it
less); with the plan speed set, a cap larger than 9 seconds of it counts as those 9 seconds ("about 338 of the 1,000 MB
down ...: a test stops after about 8 s at your plan speed"). With a monthly limit set that the estimate exceeds it adds
when the limit stops the tests: "...; the 2,000 MB limit stops tests after about day 3." While M-Lab is the provider, a privacy line under the choice repeats that every result is
published with your public IP address.

`GET /api/speedtests` (state, plan, next run, data used and the recent tests, each with its bufferbloat grade, its
headline speeds and `measured_by`) and
`POST /api/speedtests/run` (start a test now): [api.md](api.md). Metrics (`pengumon_speedtest_*`) and the Grafana row:
[telemetry.md](telemetry.md).
