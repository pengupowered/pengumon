# Adding a device

This guide is for you if you have a modem, router or other device that Pengumon cannot read yet, and you want to teach it
to. You do not need the maintainer's hardware or any special tool: you need your own device, a text editor and, if you
like, an AI coding assistant. Everything below is checked against the code; the names are the ones in
`src/pengumon/`, so an assistant can follow them in the repository.

Pengumon adds a device with **a driver**: one Python module that talks to the device and hands back readings. Nothing
else in the code knows a vendor, an address or a device id, so a driver for a kind of device Pengumon already
understands (another router, another cable modem) needs no change anywhere else. The panels, the stored history, the
incident analysis, the Ask AI pack and the telemetry all follow from what the driver says it offers.

Contents: [before you start](#before-you-start), [how Pengumon talks to devices](#how-pengumon-talks-to-devices),
[choosing capabilities](#choosing-capabilities), [line technologies](#line-technologies), [writing the driver](#writing-the-driver),
[recording answers](#recording-answers-by-hand-and-redacting-them), [tests](#tests), [the Ask AI pack](#the-ask-ai-pack),
[what the panels show](#what-the-panels-show), [telemetry](#telemetry), [the pull request checklist](#checklist-for-the-pull-request).

## Before you start

- **Open an issue first**, with the "Support for a new device" form. Say what the device offers (a web page, an API,
  SNMP, UPnP) and whether you will write the driver. It saves you building something that exists, or that needs a
  different approach. Someone with the same device may already have notes.
- **Look for a standard first.** A device that answers SNMP, UPnP or OpenWrt's interface is already covered by a generic
  driver ([supported devices](devices.md#supported-devices)): you may only need to configure it.
- **Read-only, always.** A driver reads state. It must never change the device's settings, reboot it or log other
  users out. Call only requests that report state, and say so in the module's docstring (the OpenWrt driver keeps
  a list of the calls it may make, `READ_METHODS` in `drivers/openwrt.py`, and refuses any other).
- **Be gentle.** Home devices are often slow. A modem's web page can be upset by frequent reads (see
  `min_poll_seconds` below). Read only what you need, and as seldom as the readings change.
- **Keep your network private.** What you record from your device holds addresses, MAC addresses, serial numbers and
  sometimes passwords. Redact it before it goes into the repository, an issue or a chat with an AI tool
  ([below](#recording-answers-by-hand-and-redacting-them)).
- **Tests never touch a real device.** CI has no devices. Your driver is tested by replaying answers you recorded.

You work in a clone of the repository ([CONTRIBUTING.md](../CONTRIBUTING.md) has the setup, and how a pull request is
applied). The tests run in a container with `scripts/test.sh`.

## How Pengumon talks to devices

```
Settings > devices list ──> DeviceConfig ──> DRIVERS[driver] ──> driver.from_config(...) ──> Driver
                                                                                              │ poll()
   stored history, API, panels, incidents, pack  <── monitor (snapshot) <── Device (schedule) ┘ PollResult
```

1. **The configuration.** A device is an entry of the `devices` list on the Settings page ([devices.md](devices.md)).
   `DeviceConfig` (`devices/settings.py`) checks the entry when it is saved: the id, the `driver` name (it must be a key
   of `DRIVERS`), the URL (the schemes come from the driver), the `options` (checked by the driver's `options_model`),
   and that no password is in it. Passwords live in `data/secrets.json`, the environment or a `/run/secrets` file
   (`secrets.py`), never in the entry.
2. **The registry of drivers.** `DRIVERS` in `drivers/__init__.py` maps a driver's `name` to its class. It is a fixed
   list of drivers that ship with Pengumon; there is no plugin loading. Adding a driver is adding it there. The
   Settings page, the validation and the API all read this one list.
3. **The driver.** A class that follows the `Driver` protocol in `capabilities/__init__.py`. Its `from_config` builds it
   from the entry and the device's `Credentials`; its async `poll()` makes the requests and returns a `PollResult`:
   one *reading* per capability it offers, plus, for any capability it could not read this time, the reason in
   `missing`.
4. **The device runner.** `build_device` (`devices/registry.py`) wraps the driver in a `Device` (`devices/device.py`),
   which does everything a driver does not: it polls every `poll_interval_seconds` (the driver's
   `default_poll_seconds` when the entry sets none), one poll at a time, with a trace span and metrics for each poll,
   turns an exception into a `DriverError` kind (`classify`), keeps the log short when a device is down for hours, and
   stores the result. A driver does no scheduling, logging of failures, storage or retries of its own.
5. **The monitor.** A `Device` hands each result to a monitor chosen from the driver's capabilities
   (`monitor_class` in `devices/registry.py`): `LineMonitor` for `access_line`, `RouterMonitor` for `wan_counters` and
   `router_system` (with the client list and mesh, which it keeps for the pages). The monitor grades the readings,
   computes rates, keeps the latest snapshot and writes the history.
6. **The supervisor.** `DeviceSupervisor` (`devices/component.py`) owns one polling task per device id. When the
   devices are saved in Settings it applies the change live: an unchanged device keeps its connection, a changed one is
   rebuilt, a removed one is stopped, a new one starts. `validate_devices` (`devices/validation.py`) first does a *dry
   build* of every entry: it calls your `from_config` with no password and no network. So **`from_config` must not
   open a connection or read the device**; it may raise `ValueError` with a plain message for a bad option, which the
   Settings page shows.

### What a driver must have

The `Driver` protocol lists the required parts. The first block below is in the protocol; the second block is read by
other code without being in the protocol, so a driver that leaves them out can break the Settings page.

| Attribute or method | What it is |
|---|---|
| `name` | The driver id used in the `driver:` field and as the key in `DRIVERS`. By convention lower case, letters, digits and `_`. |
| `offers` | A `frozenset` of `Capability` values (next section). |
| `options_model` | A pydantic model of the entry's `options:`, with `extra="forbid"` so a misspelt option is refused. An empty model if there are none. |
| `default_poll_seconds` | How often to read when the entry sets no interval. |
| `from_config(cfg, credentials, *, client=None, **clocks)` | A classmethod that builds the driver. `client` is an httpx client for tests. |
| `host` | The device's host, for logs (never a password). |
| `missing_configuration()` | `None` when the driver can poll, otherwise what is missing in plain words ("no password"). |
| `set_credentials(username, password)` | Takes new credentials and forgets any session, so the next poll signs in again. |
| `poll()` | The async read, described below. |
| `aclose()` | Closes the HTTP client. |
| `label` | **Required in practice.** What the device is, in words a person uses ("ASUS router"). The Settings schema (`settings/schema.py`, `driver_spec`) reads it directly, and the dashboard shows it. |
| `url_schemes` | Optional; default `("http", "https")`. SNMP has `("snmp",)`. |
| `default_username` | Optional; used when the entry sets no `username` (OpenWrt has `root`; otherwise `admin`). |
| `needs_sign_in` | Optional; default `True`. A device read without a password (UPnP's `upnp_igd`) sets `False`: the Settings page and the device setup then ask for no user name or password, and a password posted to it is refused. |
| `min_poll_seconds` | Optional; the least time between two reads. A shorter interval is raised to it with a warning, and the runner enforces it for every caller, backs off after failures and stops after a refused login. Use it only for a device that is easily upset (the Xfinity gateway has 600 s). |
| `line_technology` | Required for a driver that offers `access_line` (`"docsis"`). |
| `pack_notes` | Optional text about the driver for the Ask AI pack ([below](#the-ask-ai-pack)). |

`from_config` takes `**clocks` because `build_device` passes the test clocks `clock` and `wall_clock` when a test gives
them (`devices/registry.py`). Accept both, or take what you need and drop the rest, as the example below does. (The
Xfinity driver takes none, which works because its tests never pass clocks.)

## Choosing capabilities

A capability is a kind of reading, independent of who made the device. Offer exactly the ones your device can report;
leave out the rest rather than inventing values. The enum is `Capability` in `capabilities/__init__.py`.

| Capability | The reading you return | What your device must give | What it brings |
|---|---|---|---|
| `wan_counters` | `WanCounters(rx_bytes, tx_bytes, counter_bits=None, read_at=None)` | Cumulative bytes received and sent on the WAN port since the device started | WAN rates, line load against the line's capacity, load-aware incident analysis, router restarts |
| `router_system` | `SystemReading(cpu_pct, mem_pct, client_count, model, uptime_s, cpu_temp_c, wan_link_mbps, uptime_wrap_s)` | Any of CPU %, memory %, the number of connected devices, the device's own model name, its uptime, CPU temperature and WAN link speed. A value you do not know is `None` | The router card (the uptime also gives the restart and WAN link events), warnings for high CPU or memory, the router module of the Ask AI pack |
| `client_inventory` | `ClientsReading(clients=(Client(...), ...))` | The list of connected devices: MAC, name, wired or Wi-Fi, band, signal, link rates | The Local network dialog, client counts per band, the Wi-Fi module of the pack |
| `mesh` | `MeshReading(nodes=(MeshNode(...), ...))` | The mesh nodes and how many clients each carries (usually with `client_inventory`) | Per-node counts, a node dropping to 0 clients, backhaul |
| `traffic_usage` | `TrafficReading(read_at, hours=(TrafficHour(...), ...), apps=(TrafficApps(...), ...))` | Bytes up and down per client and clock hour, and per app over 24-hour windows, as the device counted them (the `asuswrt` driver reads the router's Traffic Analyzer) | The Internet use card, the device traffic dialog, the traffic in the incident inspector, the MCP `traffic` tool |
| `access_line` | `LineReading(technology="docsis", channels=(DocsisChannel(...), ...))` | The physical line to the provider: per-channel power, SNR and codeword errors | The Line card and tab, the cable-line module of the pack, line-fault wording in incidents |

Rules that follow from the code:

- **`access_line` cannot be combined with `wan_counters` or `router_system` in one driver.** The monitors for them are
  different and no monitor keeps both (`monitor_class` in `devices/registry.py` refuses the combination and the device
  is left out). A gateway that is also a router is two `devices:` entries with two drivers, or you open an issue to
  discuss a combined monitor.
- A driver that offers none of `wan_counters`, `router_system` or `access_line` has no monitor and is refused.
  `client_inventory` and `mesh` ride along with `router_system` or `wan_counters`.
- A reading says only what was measured. A number the device did not report is `None`; a capability you could not read
  this time is left out of `readings` and explained in `missing[capability]` ("the router's answer had no WAN
  counters"). Never return `0` for "unknown": the charts and the load learning would take it as a quiet line.
- **32-bit counters.** Some devices (UPnP gateways) keep 32-bit byte counters that wrap. Give `counter_bits=32` and
  Pengumon (`devices/wan.py`) adds the wrap to the next rate. Leave it `None` for 64-bit counters. A counter that goes
  backwards without a width is taken as a restart of the device.
- `read_at` is the time the device's answer was produced, when the answer carries one; otherwise leave it `None`.
- Names, models and vendors a device reports can be anything: pass them through `clean_text` and MACs through
  `normal_mac` (`drivers/common.py`). `proc_stat_cpu` and `CpuMeter` turn a Linux `/proc/stat` into a CPU percentage.
- The features the dashboard switches on are in `FEATURES` in `capabilities/features.py`: for example `line_load` needs
  `wan_counters`, `clients` needs `client_inventory`.

## Line technologies

`access_line` readings are judged by line technology. Today only `"docsis"` (cable) exists: `LineReading.technology` is
`Literal["docsis"]`, `LineMonitor` answers any other value with an "unsupported" error, and the incident wording for
each technology is in `lines/__init__.py` (`WORDING`).

- **Another cable modem** (a different brand of DOCSIS modem) needs only a driver that returns `LineReading(technology="docsis", channels=...)`
  with `DocsisChannel` rows (`lines/docsis_model.py`) and sets `line_technology = "docsis"`. Everything else follows.
- **Fiber, DSL and other lines** are new technologies. That is real work: the reading model, a monitor, the judging rules,
  the wording, the panel and a contribution to the Ask AI pack (`TECHNOLOGY_CONTRIBUTIONS` in `reports/contributions.py`,
  [ai-pack.md](ai-pack.md#adding-a-module-developers)). Do not start with the code: open an issue with a sample of what
  your line reports and we will work out the shape together.

## Writing the driver

Put the module in `src/pengumon/drivers/`, add the class to `DRIVERS` (and the import) in `drivers/__init__.py`, and
nothing else in the code needs to change. This is a complete driver for a made-up router that serves its WAN counters as
JSON; a real one differs only in what it asks and how it reads the answer:

```python
"""Example router: WAN byte counters from a JSON status page."""

import time
from typing import TYPE_CHECKING, Callable, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from pengumon.capabilities import Capability, DriverError, PollResult, WanCounters
from pengumon.drivers.http import DeviceHttp, Verify, device_verify

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials


class ExampleRouterOptions(BaseModel):
    """`options:` of an example_router device (none yet)."""
    model_config = ConfigDict(extra="forbid")


class ExampleRouterDriver:
    name = "example_router"
    label = "Example router"  # what the device is, in the dashboard's words
    offers = frozenset({Capability.WAN_COUNTERS})
    options_model = ExampleRouterOptions
    default_poll_seconds = 10.0

    def __init__(self, url: str, username: str, password: Optional[str], *,
                 client: Optional[httpx.AsyncClient] = None, verify: Verify = True,
                 wall_clock: Callable[[], float] = time.time):
        self._username, self._password = username, password
        self._verify = verify
        self._wall_clock = wall_clock
        self.http = DeviceHttp(url, self._new_client, driver=self.name, client=client)

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None, **clocks: Callable[[], float]
                    ) -> "ExampleRouterDriver":
        cls.options_model.model_validate(cfg.options)
        clocks.pop("clock", None)  # this driver keeps no time of its own
        return cls(cfg.url, credentials.username, credentials.password, client=client,
                   verify=device_verify(cfg), **clocks)

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=6.0, verify=self._verify)

    @property
    def host(self) -> str:
        return self.http.host

    def missing_configuration(self) -> Optional[str]:
        return None if self._password else "no password"

    def set_credentials(self, username: str, password: str) -> None:
        self._username, self._password = username, password

    async def aclose(self) -> None:
        await self.http.aclose()

    async def poll(self) -> PollResult:
        response = await self.http.get("api/status", auth=(self._username, self._password or ""))
        if response.status_code in (401, 403):
            raise DriverError("auth", "the router refused the user name or password")
        if response.status_code != 200:
            raise DriverError("protocol", f"the status page answered HTTP {response.status_code}")
        try:
            wan = response.json()["wan"]
            counters = WanCounters(rx_bytes=int(wan["rx_bytes"]), tx_bytes=int(wan["tx_bytes"]),
                                   read_at=self._wall_clock())
        except (ValueError, KeyError, TypeError):
            raise DriverError("protocol", "the status page had no WAN byte counters") from None
        return PollResult(readings={Capability.WAN_COUNTERS: counters})
```

and in `drivers/__init__.py`:

```python
from pengumon.drivers.example_router import ExampleRouterDriver
DRIVERS = {driver.name: driver for driver in (AsusWrtDriver, ExampleRouterDriver, OpenWrtDriver, SnmpDriver, XfinityXbDriver)}
```

Points that matter when you adapt it:

- **HTTP goes through `DeviceHttp`** (`drivers/http.py`). It keeps one client and one cookie jar per device, makes a
  trace span for each request and, with `PENGUMON_LOG_LEVELS=pengumon.drivers=debug`, logs method, path, status, duration and
  size, never a query string or a body (a login carries credentials). Use `self.http.get`, `.post` or `.request`; for a
  session, the cookies stay in the client and `clear_session()` forgets them (call it from `set_credentials`). `client=`
  is how a test supplies a client with a mock transport. Every answer is read with a size limit (`limit=`, 1 MiB by
  default: a longer answer is a `protocol` error, and `.request_capped` reads only the start of one instead), counted
  after decoding; requests ask for no compression, and an answer compressed anyway is decoded only up to the limit.
- **TLS** comes from the entry's `tls:` settings. Pass `verify=device_verify(cfg)` to the client you build; do not
  read the `tls` settings yourself and never default to turning checking off. Set a timeout on the client (the drivers use 6 s
  and 12 s). Read only the configured address, and do not follow redirects unless your device needs it (httpx follows none by default; the Xfinity
  driver sets `follow_redirects=True`).
- **Errors.** Raise `DriverError(kind, message)` for what you can name: `auth` (login refused), `protocol` (an answer
  that is not what you expect), `unsupported`, `unconfigured`. Let the httpx exceptions (`ConnectError`, timeouts)
  through: the runner turns them into `unreachable` and `timeout` (`classify` in `devices/device.py`). The messages
  appear on the dashboard and in the logs, so write them for a person and never include the password, a cookie or the
  full URL. A session the device forgot (it restarted) should cost one new login in the same poll, not an error.
- **Credentials.** `credentials.username` and `.password` arrive in `from_config` (the password may be `None`; then
  `missing_configuration()` says so and no poll is made). A driver for a protocol that has no username ignores it, as
  SNMP does (its "password" is the community string).
- **Not HTTP?** The SNMP driver (`drivers/snmp.py`, with its own UDP client in `drivers/snmp_ber.py`) shows a driver
  that does not use `DeviceHttp`. The same rules apply.
- **Discovery (optional).** If the device's login page is recognizable, add a `Fingerprint` to `FINGERPRINTS` in
  `engine/discovery/devices.py`; discovery then suggests your driver for that hop. Skip it if you cannot do this
  without being sure.
- **Document it.** Add a section to [devices.md](devices.md) (what it reads, what it needs on the device, such as a
  read-only user, and what it cannot do) and a row to the supported-devices table there and in the README.

## Recording answers by hand, and redacting them

You write the driver from what your device actually says, so you need real answers: first to read, then as the test.
With your device on your own network, record the answers with the tool you already have:

- a web page or JSON endpoint: your browser's developer tools (Network tab, "Save as HAR" or "Copy response"), or
  `curl -i http://<device>/<path>` (add `-c cookies.txt -b cookies.txt` to keep a session);
- a login: record the request that signs in and the one that follows, to see which cookie or token carries the session;
- SNMP: `snmpwalk -v2c -c <community> <device> <oid>` for the tables the driver will read;
- UPnP: the description XML at the device's description URL and the SOAP answers of the calls you plan to make.

Record **one poll's worth** in each state you want to test: a good answer, a login refused, an answer with a field
missing, an answer from a device that has just restarted. Note the firmware version and the model.

### Redact before you keep it

A recording usually holds more than you see. Before it goes into a file in the repository, an issue, a pull request or
an AI tool's chat, replace:

| Find | Replace with |
|---|---|
| Your IPv4 addresses (the device, the gateway, DHCP clients, DNS servers) | `192.0.2.N` (or `198.51.100.N`, `192.168.88.N`), one made-up address for each real one, the same everywhere |
| IPv6 addresses and prefixes | `2001:db8::N` |
| MAC addresses (the device's own, its radios, every client, mesh nodes) | `02:00:00:00:00:NN`, one for each real one, the same everywhere |
| Serial numbers, device ids, UUIDs and anything that looks like one | `SERIAL-1` and so on |
| Your Wi-Fi names (SSIDs) | `ssid-1`, `ssid-2` |
| Host names of your devices and anything with your name or your family's in it | `host-1`, `laptop`, `printer` |
| Your provider's name, account or modem ids, and public IP addresses | `example-isp.net`, or leave out |
| Passwords, tokens, cookies, session ids, community strings and `Authorization` headers | **Delete them.** Do not replace with a value that looks real. |

Then **read the whole file once, line by line.** A script finds what it is told to find; it does not find a nickname in a
device description or a street name in a Wi-Fi name. Keep the same replacement for the same value, so a test can still
tell two devices apart. Keep the numbers (byte counts, signal levels, timings) as recorded unless one identifies you; the
tests need realistic values. Then `grep` the finished fixture for your real host names, your SSIDs and the first part of your
addresses.

A small helper that does the mechanical part (not a guarantee: it only knows IPv4, MAC addresses and the names you list):

```python
"""Replaces what identifies your network in a recorded answer with stable made-up values.

    python3 redact_capture.py answer.json "MySSID=ssid-1" "ABC123456=SERIAL-1" "my-laptop=host-1" > fixture.json
"""
import re
import sys

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
KEEP = {"0.0.0.0", "127.0.0.1", "255.255.255.0", "255.255.255.255"}


def redact(text: str, names: dict) -> str:
    for old, new in sorted(names.items(), key=lambda kv: -len(kv[0])):  # the longest first
        text = text.replace(old, new)
    seen_ip, seen_mac = {}, {}

    def ip(m):
        return m.group(0) if m.group(0) in KEEP else "192.0.2.%d" % seen_ip.setdefault(m.group(0), len(seen_ip) + 1)

    def mac(m):
        key = m.group(0).lower().replace("-", ":")
        return "02:00:00:00:00:%02x" % seen_mac.setdefault(key, len(seen_mac) + 1)

    return MAC.sub(mac, IPV4.sub(ip, text))


if __name__ == "__main__":
    path, pairs = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
    sys.stdout.write(redact(open(path, encoding="utf-8").read(), pairs))
```

It treats a four-number firmware version such as `1.2.3.4` as an address, and it does not know IPv6; check both by eye.
If you let an AI tool look at a recording, give it the **redacted** file, never the original.

## Tests

Pengumon's tests are lean: one test that fails without the behavior beats several that check the same thing. Read the
existing driver tests before writing yours; they are the best examples, and **never write fixtures, helper names or
signatures from memory.**

### The driver's own test

Put the redacted answers in the test file (as `tests/test_openwrt.py` does, with a small fake of the device that answers
by request) or, for large answers, in a file next to the tests. Replay them through `httpx.MockTransport`; no network is
used. `tests/device_helpers.py` has `Clock` (a clock a test moves by hand). Build the device the way the app does with
`build_device`, so the registry, the options and the credentials are tested too. This is the test of the example
driver above:

```python
import httpx
import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.secrets import CredentialSources, password_env_var

from device_helpers import Clock

STATUS = {"wan": {"rx_bytes": 100_000_000, "tx_bytes": 50_000_000}, "model": "Example X1"}


def build(handler, password="pw"):
    cfg = DeviceConfig(id="example", driver="example_router", url="http://192.0.2.1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    secrets = CredentialSources(env={password_env_var("example"): password}, secrets_dir=None, store={})
    return build_device(cfg, None, secrets, client=client, clock=Clock(), wall_clock=lambda: 1_700_000_000.0)


def status_page(request: httpx.Request) -> httpx.Response:
    if request.url.path != "/api/status":
        return httpx.Response(404)
    if request.headers.get("authorization") is None:
        return httpx.Response(401)
    return httpx.Response(200, json=STATUS)


async def test_one_poll_reads_the_wan_counters():
    result = await build(status_page).driver.poll()
    wan = result.readings[Capability.WAN_COUNTERS]
    assert (wan.rx_bytes, wan.tx_bytes, wan.read_at) == (100_000_000, 50_000_000, 1_700_000_000.0)
    assert result.missing == {}


@pytest.mark.parametrize("answer, kind", [
    (httpx.Response(401), "auth"),
    (httpx.Response(200, json={"wan": {}}), "protocol"),
    (httpx.Response(200, text="<html>not json</html>"), "protocol"),
])
async def test_a_bad_answer_is_a_driver_error_of_the_right_kind(answer, kind):
    with pytest.raises(DriverError) as caught:
        await build(lambda request: answer).driver.poll()
    assert caught.value.kind == kind


async def test_a_device_that_cannot_be_reached_becomes_an_error_snapshot():
    def down(request):
        raise httpx.ConnectError("refused")
    snapshot = await build(down).poll(force=True)  # the Device never raises for a device problem
    assert snapshot.status == "error" and snapshot.error_kind == "unreachable"
```

What a good driver test covers, taking `tests/test_openwrt.py` and `tests/test_snmp.py` as models:

- a good poll gives every capability the driver offers, with the right numbers;
- a refused login is an `auth` error, and new credentials (`set_credentials`) make the next poll sign in again;
- a session the device forgot costs one new login and the poll still succeeds;
- each reading that can be missing is reported in `missing` with a reason, and the others still come;
- a non-JSON or truncated answer is a `protocol` error, not an exception that escapes;
- a counter that wraps (`counter_bits`) or restarts, if the device has counters;
- the options are checked (an unknown option and a bad value are refused, as in `test_options_are_checked`), and
  the driver is in `DRIVERS` with the username and URL schemes you intended;
- nothing the driver sends changes the device (for HTTP APIs, assert the set of paths or methods requested).

For a security-relevant behavior (credentials never in a log line or an error message), check that the test fails
when you break the behavior: temporarily remove the protection and watch the test fail, then restore it.

### Tests that already cover every driver

Adding to `DRIVERS` puts your driver under these without further work; run them to see:

- `tests/test_device_config.py` and `tests/test_device_registry.py`: the entry is validated, the registry builds the
  device and picks the monitor for the capabilities, the features follow.
- `tests/test_settings_api.py` and `tests/test_api_devices.py`: the Settings schema lists the driver with its label,
  options, default username and schemes; the device API shows its summary.
- `tests/test_contributions.py`: the Ask AI contract tests (next section). They check every registered contribution and
  driver note: budgets, unique event codes, no names or addresses in the text, redaction coverage, documented
  instruments. **One of its tests names the drivers that have notes**
  (`test_driver_notes_state_the_configured_intervals_and_hide_the_vendor_when_anonymized` expects exactly the set it
  lists): if your driver adds `pack_notes`, add its name there and a check of its wording.
- `tests/test_telemetry_docs.py`: every metric is in [telemetry.md](telemetry.md). Only relevant if you add one.

### If you change the web page

A new capability, panel or dialog needs the browser tests as well: `scripts/test-ui.sh` and [testing.md](testing.md)
("the browser tests") explain the seed data (`tests/ui/serve.py` has one function of fake data per capability) and the
steps. A driver for an existing capability does not need this: the panels already show its readings.

### Run them

```bash
scripts/test.sh -p no:warnings tests/test_your_driver.py     # your tests
scripts/test.sh -p no:warnings                                # everything, a few minutes
```

## The Ask AI pack

Offering a capability is enough for its section of the pack ([ai-pack.md](ai-pack.md), "Device modules"). The capability
modules are listed in `CAPABILITY_CONTRIBUTIONS` in `devices/contrib.py` and activated from the configured devices by
`reports/contributions.py`: `wan_counters` brings the line-load module, `router_system` the router module,
`client_inventory` the Wi-Fi and mesh module, `access_line` with DOCSIS the cable-line module. Their events,
redaction (what is kept, replaced or dropped at each of the three levels) and gauges come with them.

What you may add as a driver author is `pack_notes`, a few sentences about the driver itself:

```python
from typing import ClassVar
from pengumon.capabilities.contrib import DriverNotes

    pack_notes: ClassVar[DriverNotes] = DriverNotes(
        full="Device notes: the router is an Example router. Its counters are read every {poll}.",
        anonymized="Device notes: the router reports WAN counters every {poll}.",
    )
```

- State **only what your own code establishes**: how often it reads, where the data comes from, a firmware behavior
  you work around. Not advice about the vendor and not claims you cannot point to a line for (the Xfinity and AsusWRT
  drivers list, in a comment above the notes, the line that backs each statement; do the same).
- Placeholders are filled from the device's configuration by `driver_note` (`reports/contributions.py`): `{poll}`,
  `{min_poll}`, `{poll_limit}`, and `{clientlist_interval}` if your options have `clientlist_interval_seconds`. A
  literal `{` or `}` in the text breaks the format, so write none.
- **The `anonymized` text names no vendor, model or provider.** Leave the words out, or give `None` for no note at that
  level. A new vendor name that could appear elsewhere in the pack should also be added to `VENDOR_WORDS` in
  `reports/redact.py`, which removes those words at the Anonymized level.
- Check it with the contract tests above, and read your pack at the three levels in the dashboard (Ask AI) with
  made-up data.

A new kind of *line* (not another modem) needs a technology contribution; that is described in
[ai-pack.md](ai-pack.md#adding-a-module-developers) ("Adding a module") and starts with an issue.

## What the panels show

Nothing to write for these: they appear when a device offers the capability and is configured (the Layout dialog lists a
panel that is not available with the reason). The list is [dashboard.md](dashboard.md); in short:

| Capability | Where it shows |
|---|---|
| `access_line` | The **Line** card and tab: channel levels and codeword errors, graded good, warning or critical against the usual DOCSIS ranges; the "line" wording in incident descriptions; the cable-line section of the pack |
| `wan_counters` | The **Router** card (download and upload rates) and the router history chart; the **line load**, as a share of the line's capacity (learned from the rates, or set in Settings), which lets incident analysis call a slow line "busy" instead of a fault |
| `router_system` | The **Router** card: CPU, memory, connected-client count, graded against the router levels in Settings |
| `client_inventory` | The **Local network** dialog (clients by band, signal, link rate, mesh node) and the client counts on the router card |
| `traffic_usage` | The **Internet use** card (which devices and apps used the internet, per hour), the traffic of the hours of an incident in the inspector and the MCP `traffic` tool |
| `mesh` | The mesh nodes on the router card and in the Local network dialog; a node the device reports offline turns the card to a warning |

`GET /api/devices` shows each device with its `label` and `offers`, and `GET /api/features` which features are available
and, if not, why ([api.md](api.md)). Open the dashboard with your driver and your own device and look at each card at
desktop and phone width, in the light and dark themes, before you open the pull request; the state of a device that is
off, has no password or answers wrongly is shown by the same card, so try those too.

## Telemetry

The runner records every device's polls for you: `pengumon_device_polls_total` and `pengumon_device_poll_duration_seconds`
(per device, with the outcome `ok`, `partial` or `error` and the error kind), `pengumon_device_status`, the failure
count, and a trace span for each poll with a child span for every HTTP request. Your driver gets all of these by
being a driver, and the module gauges (line levels, WAN rates, router CPU and memory, client and mesh counts) follow
from the capabilities. The full list, with what each means, is [telemetry.md](telemetry.md).

You add nothing, but keep the rules: a metric label is a device id from the config, a category or a small fixed set,
**never** an address, a name, a MAC or a credential; a driver that logs writes the host name Pengumon passes it
and no password, token or cookie. A new metric needs an entry in [telemetry.md](telemetry.md) (a test checks it).

## Checklist for the pull request

- [ ] An issue (the "Support for a new device" form) exists and the pull request refers to it.
- [ ] The driver is read-only, takes its credentials only from `Credentials`, and puts none in a log line, an error
      message or a metric label.
- [ ] `name`, `label`, `offers`, `options_model` (`extra="forbid"`), `default_poll_seconds`, and `min_poll_seconds`
      only if the device needs it; `from_config` opens no connection; the driver is in `DRIVERS`.
- [ ] Unknown values are `None` or listed in `missing`, never `0`; counters give `counter_bits` when they wrap.
- [ ] Tests replay **redacted** recorded answers through `httpx.MockTransport`; every value is made up (addresses from
      192.0.2.0/24, 198.51.100.0/24 or 192.168.88.0/24, MACs `02:00:00:...`, names such as "Example-Mesh"); no real address,
      MAC, serial, SSID, host name, password or cookie is in the diff. You have `grep`ped the diff for your own.
- [ ] The tests cover a good poll, a refused login, a missing field and an unreadable answer; each behavior has a test
      that fails without it.
- [ ] `scripts/test.sh` passes (and `scripts/test-ui.sh` if you touched `src/pengumon/web/` or `tests/ui/`).
- [ ] `pack_notes` (if any) state only what the code establishes; the anonymized text names no vendor; the contract
      tests in `tests/test_contributions.py` pass and list your driver.
- [ ] [devices.md](devices.md) has a section for the driver (what it reads, what it needs on the device, its limits) and
      a row in the supported-devices table, and the README's table has the row too.
- [ ] You tried it on your own device, and say in the pull request which model and firmware, and what you saw in the
      dashboard. Describe, in your own words, what you could not test.
- [ ] No new dependency (agree on one in the issue first), and no version or `CHANGELOG.md` change (the maintainers do
      that).
