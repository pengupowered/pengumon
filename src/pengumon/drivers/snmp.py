"""Any SNMPv2c or SNMPv3 agent (most routers, switches and NAS boxes): WAN byte counters from IF-MIB, CPU and memory
from HOST-RESOURCES-MIB, and the model from sysDescr.

Read-only by protocol: only GET and GETBULK requests. SNMPv2c uses the community string the device's password holds.
SNMPv3 (options.version "3", snmp_v3.py) signs in as the device's username, with the password as the authentication
password and the device's privacy password (a secret of its own) for authPriv. The first poll finds the layout (which
interface is the WAN, which processors and memory rows there are) with a few table walks; every later poll is one GET
of the values. The layout is found again after the agent restarts (its sysUpTime goes back), when one of its rows
disappears, and every hour. No SNMPv1; no client list.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pengumon.capabilities import Capability, DriverError, PollResult, SystemReading, WanCounters
from pengumon.drivers.common import CPU_MIN_INTERVAL_SECONDS, clean_text
from pengumon.drivers.snmp_ber import COUNTER32, COUNTER64, INTEGER, TIMETICKS, Oid, SnmpClient, Value
from pengumon.drivers.snmp_v3 import MAX_CONTEXT_NAME, MAX_USER_NAME, MIN_PASSWORD, Usm
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials


def _oid(text: str) -> Oid:
    return tuple(int(part) for part in text.split("."))


SYS_DESCR = _oid("1.3.6.1.2.1.1.1.0")
SYS_UPTIME = _oid("1.3.6.1.2.1.1.3.0")
# sysUpTime is TimeTicks (hundredths of a second, 32 bits): it goes back to 0 every 2^32 ticks, about 497 days. The device
# events (devices/events.py) take a drop of that size as the wrap it is, not a restart.
UPTIME_WRAP_SECONDS = 2 ** 32 / 100
IF_DESCR = _oid("1.3.6.1.2.1.2.2.1.2")
IF_IN_OCTETS = _oid("1.3.6.1.2.1.2.2.1.10")
IF_OUT_OCTETS = _oid("1.3.6.1.2.1.2.2.1.16")
IF_NAME = _oid("1.3.6.1.2.1.31.1.1.1.1")
IF_HC_IN_OCTETS = _oid("1.3.6.1.2.1.31.1.1.1.6")
IF_HC_OUT_OCTETS = _oid("1.3.6.1.2.1.31.1.1.1.10")
# The interface of the default route: RFC 1213 ipRouteIfIndex.0.0.0.0, else the first IPv4 default route of
# inetCidrRouteIfIndex (type ipv4, 4 address bytes 0.0.0.0, prefix length 0) or of the older ipCidrRouteIfIndex.
IP_ROUTE_DEFAULT_IFINDEX = _oid("1.3.6.1.2.1.4.21.1.2.0.0.0.0")
INET_CIDR_ROUTE_DEFAULT_IFINDEX = _oid("1.3.6.1.2.1.4.24.7.1.7.1.4.0.0.0.0.0")
IP_CIDR_ROUTE_DEFAULT_IFINDEX = _oid("1.3.6.1.2.1.4.24.4.1.5.0.0.0.0.0.0.0.0")
HR_PROCESSOR_LOAD = _oid("1.3.6.1.2.1.25.3.3.1.2")
HR_STORAGE_TYPE = _oid("1.3.6.1.2.1.25.2.3.1.2")
HR_STORAGE_DESCR = _oid("1.3.6.1.2.1.25.2.3.1.3")
HR_STORAGE_UNITS = _oid("1.3.6.1.2.1.25.2.3.1.4")
HR_STORAGE_SIZE = _oid("1.3.6.1.2.1.25.2.3.1.5")
HR_STORAGE_USED = _oid("1.3.6.1.2.1.25.2.3.1.6")
HR_STORAGE_RAM = _oid("1.3.6.1.2.1.25.2.1.2")
# net-snmp on Linux counts buffers and page cache in the RAM row's "used". It also has a row whose size is what the
# kernel calls available (5.8 and later), which gives memory in use as `free` counts it; without that row, the
# buffers and cache rows are taken off.
AVAILABLE_MEMORY = "Available memory"
RECLAIMABLE_MEMORY = ("Memory buffers", "Cached memory")
# CPU without hrProcessorLoad (net-snmp in a container, some builds): UCD-SNMP-MIB's raw tick counters, as /proc/stat
# has them. Busy is everything but idle and iowait; ssCpuRawKernel is left out (on Linux it is 0 or part of system).
UCD_CPU_BUSY = tuple(_oid(f"1.3.6.1.4.1.2021.11.{n}.0") for n in (50, 51, 52, 56, 61, 64))  # user nice system irq softirq steal
UCD_CPU_IDLE = tuple(_oid(f"1.3.6.1.4.1.2021.11.{n}.0") for n in (53, 54))  # idle, iowait

DEFAULT_PORT = 161
REDISCOVER_SECONDS = 3600.0
MAX_TABLE_ROWS = 1024
MAX_PROCESSORS = 256
GET_CHUNK = 24  # variables per GET (agents with a small PDU size answer tooBig to large ones)
INTERFACE_NAMES_SHOWN = 12


V3_ONLY = {"version": "3"}  # the settings page shows an option with this `shown_when` only for SNMPv3


class SnmpOptions(BaseModel):
    """`options:` of an snmp device."""
    model_config = ConfigDict(extra="forbid")

    # The WAN (or uplink) interface by its ifName or ifDescr. Unset: the interface of the default route, when the
    # agent reports one.
    wan_interface: Optional[str] = Field(default=None, min_length=1, max_length=64)
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)  # per try
    retries: int = Field(default=1, ge=0, le=5)  # tries after the first, per request
    version: Literal["2c", "3"] = "2c"
    # SNMPv3 (the device's username is the user, its password the authentication password, its privacy password the
    # privacy one); refused with version 2c, so a v3 setting is never silently ignored.
    security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] = Field(
        default="authPriv", json_schema_extra={"shown_when": V3_ONLY})
    auth_protocol: Literal["SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"] = Field(
        default="SHA", json_schema_extra={"shown_when": V3_ONLY})
    priv_protocol: Literal["AES"] = Field(default="AES", json_schema_extra={"shown_when": V3_ONLY})
    context_name: Optional[str] = Field(default=None, max_length=MAX_CONTEXT_NAME,
                                        json_schema_extra={"shown_when": V3_ONLY})

    @model_validator(mode="after")
    def _v3_options_need_version_3(self):
        given = sorted(name for name in self.model_fields_set
                       if (type(self).model_fields[name].json_schema_extra or {}).get("shown_when") == V3_ONLY)
        if self.version != "3" and given:
            raise ValueError(f"{', '.join(given)} {'is' if len(given) == 1 else 'are'} for SNMPv3: set version to \"3\"")
        if self.context_name is not None and len(self.context_name.encode("utf-8")) > MAX_CONTEXT_NAME:
            raise ValueError(f"context_name is longer than {MAX_CONTEXT_NAME} bytes")
        return self


@dataclass(frozen=True)
class Layout:
    """What the discovery walks found. `wan_problem` says why there is no `if_index`."""

    model: Optional[str]
    if_index: Optional[int]
    if_label: Optional[str]
    wan_problem: Optional[str]
    processors: Tuple[int, ...]
    ucd_cpu: bool  # no processors: CPU from the UCD-SNMP-MIB tick counters
    ram: Optional[int]
    available: Optional[int]  # the "Available memory" row
    reclaimable: Tuple[int, ...]
    found_at: float


def _index(oid: Oid, prefix: Oid) -> Optional[int]:
    """The single sub-identifier after `prefix`, or None for a deeper or foreign OID."""
    return oid[-1] if len(oid) == len(prefix) + 1 and oid[:len(prefix)] == prefix else None


def model_of(descr: Optional[str]) -> Optional[str]:
    """A short model text from sysDescr: its first line, trimmed to 64 characters."""
    return clean_text(descr.splitlines()[0]) if descr and descr.strip() else None


def match_interface(wanted: str, names: Dict[int, str], descrs: Dict[int, str]) -> Optional[int]:
    """The ifIndex whose ifName, else ifDescr, is `wanted`; exact first, then ignoring case."""
    for table in (names, descrs):
        for index, text in sorted(table.items()):
            if text == wanted:
                return index
    lower = wanted.lower()
    for table in (names, descrs):
        for index, text in sorted(table.items()):
            if text.lower() == lower:
                return index
    return None


class Unconfigured:
    """The `security` of an SNMPv3 client whose configuration is incomplete: every request fails with
    DriverError("unconfigured") before anything is sent. An SNMPv3 device never falls back to SNMPv2c (which would send
    a community string in clear), whatever happens to its credentials while a poll is under way."""

    def __init__(self, why: str):
        self.why = why

    async def request(self, exchange, next_id, pdu, oids, a: int = 0, b: int = 0):
        raise DriverError("unconfigured", f"SNMPv3 is not configured: {self.why}")


def v3_problem(level: str, username: Optional[str], password: Optional[str],
               privacy_password: Optional[str]) -> Optional[str]:
    """What is wrong with these SNMPv3 credentials for `level`, or None. Never holds a password."""
    if not username or len(username.encode("utf-8")) > MAX_USER_NAME:
        return f"the SNMPv3 user name (the device's username) must have 1 to {MAX_USER_NAME} bytes"
    if level == "noAuthNoPriv":
        return None
    if not password:
        return "no SNMPv3 authentication password (the device's password)"
    if len(password.encode("utf-8")) < MIN_PASSWORD:
        return f"the SNMPv3 authentication password is shorter than {MIN_PASSWORD} characters (agents refuse those)"
    if level == "authPriv":
        if not privacy_password:
            return "no SNMPv3 privacy password (security level authPriv needs one)"
        if len(privacy_password.encode("utf-8")) < MIN_PASSWORD:
            return f"the SNMPv3 privacy password is shorter than {MIN_PASSWORD} characters (agents refuse those)"
    return None


def memory_pct(values: Dict[Oid, Value], ram: int, available: Optional[int],
               reclaimable: Sequence[int]) -> Optional[float]:
    """RAM in use, in percent, from the hrStorage rows (sizes in allocation units, which may differ per row): the RAM
    row's size less the available row's, else the RAM row's used less the buffers and cache rows'."""
    def amount(column: Oid, index: int) -> Optional[int]:
        units = values.get(HR_STORAGE_UNITS + (index,))
        value = values.get(column + (index,))
        u = units.number(INTEGER) if units else None
        v = value.number(INTEGER) if value else None
        return u * v if u and u > 0 and v is not None and v >= 0 else None

    size = amount(HR_STORAGE_SIZE, ram)
    free = amount(HR_STORAGE_SIZE, available) if available is not None else None
    if size and free is not None:
        return round(max(0.0, min(100.0, (size - free) / size * 100.0)), 1)
    used = amount(HR_STORAGE_USED, ram)
    if not size or used is None:
        return None
    used -= sum(amount(HR_STORAGE_USED, i) or 0 for i in reclaimable)
    return round(max(0.0, min(100.0, used / size * 100.0)), 1)


class SnmpDriver:
    name = "snmp"
    label = "SNMP device"  # what the device is, in the dashboard's words (devices.summary.device_label)
    offers = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM})
    options_model = SnmpOptions
    url_schemes = ("snmp",)
    default_poll_seconds = 10.0  # one small GET per poll; 32-bit counters wrap after 34 s at 1 Gbit/s
    # the settings page's hint under the user name (devices.username)
    username_hint = "SNMPv3: the user name (with security level noAuthNoPriv it is set only here). Not used by SNMPv2c."

    def __init__(self, url: str, community: Optional[str], *, wan_interface: Optional[str] = None,
                 timeout_seconds: float = 2.0, retries: int = 1, options: Optional[SnmpOptions] = None,
                 username: str = "", privacy_password: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        """`community` is the device's password: the community string (v2c) or the authentication password (v3).
        `options` gives the version and the SNMPv3 settings (its wan_interface, timeout_seconds and retries are not read:
        the keyword arguments give those)."""
        parts = urlsplit(url)
        self._host = parts.hostname or url
        self._port = parts.port or DEFAULT_PORT
        self._options = options or SnmpOptions()
        self._community = community
        self._username = username
        self._privacy_password = privacy_password
        self._wan_interface = wan_interface
        self._clock = clock
        self._wall_clock = wall_clock
        # SNMPv3 has no community: the client gets none, so nothing of the password can ever go out as one
        self.snmp = SnmpClient(self._host, self._port, (community or "") if self.version != "3" else "",
                               timeout=timeout_seconds, retries=retries)
        if self.version == "3":
            # noAuthNoPriv has no password to enter: the user name is the device's username (settings or environment)
            self.needs_sign_in = self._options.security_level != "noAuthNoPriv"
            self._secure()
        self._log = get_logger("pengumon.drivers.snmp")
        self._layout: Optional[Layout] = None
        self._uptime: Optional[int] = None
        self._ticks: Optional[Tuple[float, int, int]] = None  # (when, busy, total) of the UCD counters
        self._ucd_pct: Optional[float] = None
        self._logged_wan: Optional[str] = None

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *, client=None,
                    **clocks: Callable[[], float]) -> "SnmpDriver":
        """`client` (an HTTP client, for the HTTP drivers) is not used. `clocks` (clock, wall_clock) are for tests."""
        options = cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, credentials.password, wan_interface=options.wan_interface,
                   timeout_seconds=options.timeout_seconds, retries=options.retries, options=options,
                   username=credentials.username, privacy_password=credentials.privacy_password, **clocks)

    @property
    def host(self) -> str:
        return self._host

    @property
    def version(self) -> str:
        return self._options.version

    @property
    def takes_privacy_password(self) -> bool:
        """Whether this device uses a privacy password (SNMPv3 authPriv): the setup dialog asks for one then."""
        return self.version == "3" and self._options.security_level == "authPriv"

    @property
    def sign_in(self) -> Dict[str, Optional[str]]:
        """What the setup dialog asks for, by field: its label, or None when the device does not use it."""
        if self.version != "3":
            return {"username": None, "password": "Community string", "privacy_password": None}
        return {"username": "SNMPv3 user name", "password": "Authentication password",
                "privacy_password": "Privacy password" if self.takes_privacy_password else None}

    def missing_configuration(self) -> Optional[str]:
        if self.version != "3":
            return None if self._community else "no community string (the device's password)"
        return v3_problem(self._options.security_level, self._username, self._community, self._privacy_password)

    def credentials_problem(self, username: str, password: str, privacy_password: Optional[str] = None) -> Optional[str]:
        """Why the credentials API must refuse these (SNMPv3: a user name over 32 bytes, a password or privacy password
        under 8 characters), or None. A privacy password left out keeps the stored one and is not checked here."""
        if self.version != "3":
            return None
        level = self._options.security_level
        if level == "authPriv" and privacy_password is None:
            level = "authNoPriv"  # the user name and the authentication password only
        return v3_problem(level, username, password, privacy_password)

    def _secure(self) -> None:
        """The SNMPv3 security of the client (its keys from the passwords); while the configuration is incomplete, one
        that refuses every request (polls do not start before, but one may be under way when the credentials change)."""
        if self.version != "3":
            self.snmp.security = None
            return
        missing = self.missing_configuration()
        if missing:
            self.snmp.security = Unconfigured(missing)
            return
        o = self._options
        self.snmp.security = Usm(self._username, o.security_level, auth_protocol=o.auth_protocol,
                                 auth_password=self._community, priv_protocol=o.priv_protocol,
                                 priv_password=self._privacy_password, context_name=o.context_name or "",
                                 clock=self._clock)

    def set_credentials(self, username: str, password: str, privacy_password: Optional[str] = None) -> None:
        """v2c: the community string is the password; the username is not used. v3: the user, the authentication
        password and (when given; else the one it has is kept) the privacy password."""
        self._community = password
        if self.version != "3":
            self.snmp.community = (password or "").encode("utf-8")
        else:  # (the v3 password is the authentication password: it never goes into the community)
            self._username = username
            if privacy_password is not None:
                self._privacy_password = privacy_password
            self._secure()

    async def aclose(self) -> None:
        await self.snmp.aclose()

    # --- discovery ------------------------------------------------------------------------------------------------

    async def _table(self, column: Oid, limit: int = MAX_TABLE_ROWS) -> Dict[int, Value]:
        return {i: v for oid, v in await self.snmp.walk(column, limit) if (i := _index(oid, column)) is not None}

    async def _texts(self, column: Oid) -> Dict[int, str]:
        return {i: t for i, v in (await self._table(column)).items() if (t := clean_text(v.text())) is not None}

    async def _default_route_interface(self) -> Optional[int]:
        (_, value), = await self.snmp.get([IP_ROUTE_DEFAULT_IFINDEX])
        index = value.number(INTEGER)
        if index and index > 0:
            return index
        for column in (INET_CIDR_ROUTE_DEFAULT_IFINDEX, IP_CIDR_ROUTE_DEFAULT_IFINDEX):
            for _, value in await self.snmp.walk(column, 16):
                index = value.number(INTEGER)
                if index and index > 0:
                    return index
        return None

    async def _find_wan(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """(ifIndex, its name, None) of the WAN interface, or (None, None, why not)."""
        names, descrs = await self._texts(IF_NAME), await self._texts(IF_DESCR)
        if self._wan_interface:
            index = match_interface(self._wan_interface, names, descrs)
            if index is None:
                known = ", ".join(list(dict.fromkeys((names or descrs).values()))[:INTERFACE_NAMES_SHOWN]) or "none"
                return None, None, (f"the agent has no interface named {self._wan_interface!r} (ifName or ifDescr; "
                                    f"options.wan_interface); it has: {known}")
        else:
            index = await self._default_route_interface()
            if index is None:
                return None, None, ("the agent reports no default route, so the WAN interface is not known: set "
                                    "options.wan_interface to its ifName or ifDescr")
        return index, names.get(index) or descrs.get(index) or f"ifIndex {index}", None

    async def _discover(self) -> Layout:
        values = dict(await self.snmp.get([SYS_DESCR]))
        if_index, if_label, wan_problem = await self._find_wan()
        processors = tuple(sorted(i for i, v in (await self._table(HR_PROCESSOR_LOAD, MAX_PROCESSORS)).items()
                                  if v.number(INTEGER) is not None))
        types = await self._table(HR_STORAGE_TYPE)
        descrs = await self._texts(HR_STORAGE_DESCR)
        ram = next((i for i, v in sorted(types.items()) if v.value == HR_STORAGE_RAM), None)
        available = next((i for i, d in sorted(descrs.items()) if d == AVAILABLE_MEMORY and i != ram), None)
        reclaimable = tuple(i for i, d in sorted(descrs.items()) if d in RECLAIMABLE_MEMORY and i != ram)
        ucd_cpu = False
        if not processors:
            ticks = await self.snmp.get(list(UCD_CPU_BUSY[:1] + UCD_CPU_IDLE[:1]))
            ucd_cpu = all(v.number(COUNTER32) is not None for _, v in ticks)
        layout = Layout(model=model_of(values[SYS_DESCR].text()), if_index=if_index, if_label=if_label,
                        wan_problem=wan_problem, processors=processors, ucd_cpu=ucd_cpu, ram=ram,
                        available=available, reclaimable=reclaimable, found_at=self._clock())
        wan = f"{if_label} (ifIndex {if_index})" if if_index is not None else wan_problem
        if wan != self._logged_wan:
            self._logged_wan = wan
            cpu = f"{len(processors)} processors" if processors else ("CPU from UCD-SNMP-MIB" if ucd_cpu else "no CPU")
            self._log.info(f"SNMP agent: WAN interface {wan}; {cpu}, "
                           f"{'RAM row found' if ram is not None else 'no RAM row'}")
        return layout

    # --- poll -----------------------------------------------------------------------------------------------------

    def _oids(self, layout: Layout) -> List[Oid]:
        oids = [SYS_UPTIME]
        if layout.if_index is not None:
            oids += [column + (layout.if_index,) for column in
                     (IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS, IF_IN_OCTETS, IF_OUT_OCTETS)]
        oids += [HR_PROCESSOR_LOAD + (i,) for i in layout.processors]
        if layout.ucd_cpu:
            oids += [*UCD_CPU_BUSY, *UCD_CPU_IDLE]
        if layout.ram is not None:
            oids += [column + (layout.ram,) for column in (HR_STORAGE_UNITS, HR_STORAGE_SIZE, HR_STORAGE_USED)]
            if layout.available is not None:
                oids += [column + (layout.available,) for column in (HR_STORAGE_UNITS, HR_STORAGE_SIZE)]
            else:
                oids += [column + (i,) for i in layout.reclaimable for column in (HR_STORAGE_UNITS, HR_STORAGE_USED)]
        return oids

    async def _get(self, oids: List[Oid]) -> Dict[Oid, Value]:
        values: Dict[Oid, Value] = {}
        for start in range(0, len(oids), GET_CHUNK):
            values.update(await self.snmp.get(oids[start:start + GET_CHUNK]))
        return values

    def _stale(self, layout: Layout, values: Dict[Oid, Value]) -> bool:
        """Whether a row the layout names has gone (an agent restart can renumber interfaces and storage)."""
        rows = [IF_IN_OCTETS + (layout.if_index,)] if layout.if_index is not None else []
        rows += [HR_PROCESSOR_LOAD + (i,) for i in layout.processors]
        rows += [HR_STORAGE_SIZE + (layout.ram,)] if layout.ram is not None else []
        return any(values[oid].missing for oid in rows if oid in values)

    async def poll(self) -> PollResult:
        layout = self._layout
        if layout is None or self._clock() - layout.found_at >= REDISCOVER_SECONDS:
            layout = self._layout = await self._discover()
        values = await self._get(self._oids(layout))
        uptime = values[SYS_UPTIME].number(TIMETICKS)
        restarted = uptime is not None and self._uptime is not None and uptime < self._uptime
        if restarted or self._stale(layout, values):
            layout = self._layout = await self._discover()
            values = await self._get(self._oids(layout))
            uptime = values[SYS_UPTIME].number(TIMETICKS)
        self._uptime = uptime
        read_at = self._wall_clock()

        readings: dict = {}
        missing: dict = {}
        counters = self._counters(layout, values, restarted, read_at)
        if isinstance(counters, WanCounters):
            readings[Capability.WAN_COUNTERS] = counters
        else:
            missing[Capability.WAN_COUNTERS] = counters

        loads = [v for i in layout.processors
                 if (v := values[HR_PROCESSOR_LOAD + (i,)].number(INTEGER)) is not None and 0 <= v <= 100]
        cpu = round(sum(loads) / len(loads), 1) if loads else None
        if layout.ucd_cpu:
            cpu = self._ucd_cpu(values, restarted)
        mem = memory_pct(values, layout.ram, layout.available, layout.reclaimable) if layout.ram is not None else None
        if cpu is None and mem is None:
            missing[Capability.ROUTER_SYSTEM] = ("the agent reports no processor load or RAM (HOST-RESOURCES-MIB "
                                                 "hrProcessorLoad, hrStorage)")
        else:
            readings[Capability.ROUTER_SYSTEM] = SystemReading(cpu_pct=cpu, mem_pct=mem, model=layout.model,
                                                               uptime_s=uptime // 100 if uptime is not None else None,
                                                               uptime_wrap_s=UPTIME_WRAP_SECONDS)
        return PollResult(readings=readings, missing=missing)

    async def read_wan_counters(self) -> Optional[WanCounters]:
        """The WAN counters alone, for a running speed test (Device.read_wan_counters, about once a second): one GET of
        sysUpTime and the WAN interface's octet counters, with the layout the polls found (None before the first poll
        found one, or without a WAN interface). The uptime only tells a restart (the counters then start a new
        baseline, not a wrap); the polls' own restart check is left alone. Many agents refresh their interface
        counters every few seconds rather than when asked (net-snmp caches its interface table): the sampler sees the
        same value twice then, and measures over a longer window."""
        layout = self._layout
        if layout is None or layout.if_index is None:
            return None
        i = layout.if_index
        values = await self._get([SYS_UPTIME] + [column + (i,) for column in
                                                 (IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS, IF_IN_OCTETS, IF_OUT_OCTETS)])
        uptime = values[SYS_UPTIME].number(TIMETICKS)
        restarted = uptime is not None and self._uptime is not None and uptime < self._uptime
        counters = self._counters(layout, values, restarted, self._wall_clock())
        return counters if isinstance(counters, WanCounters) else None

    def _ucd_cpu(self, values: Dict[Oid, Value], restarted: bool) -> Optional[float]:
        """CPU % between this reading of the UCD tick counters and the last (32-bit: they wrap, after weeks on a busy
        many-core box, so the differences are taken modulo 2^32). Readings closer than CPU_MIN_INTERVAL_SECONDS
        repeat the last percentage and keep the baseline."""
        busy_ticks = [values[oid].number(COUNTER32) for oid in UCD_CPU_BUSY]
        idle_ticks = [values[oid].number(COUNTER32) for oid in UCD_CPU_IDLE]
        if busy_ticks[0] is None or idle_ticks[0] is None:
            return None
        busy = sum(t or 0 for t in busy_ticks) & 0xFFFFFFFF
        total = (busy + sum(t or 0 for t in idle_ticks)) & 0xFFFFFFFF
        now = self._clock()
        previous = None if restarted else self._ticks
        if previous is not None and now - previous[0] < CPU_MIN_INTERVAL_SECONDS:
            return self._ucd_pct
        if previous is not None and total == previous[2]:
            return self._ucd_pct  # net-snmp refreshes the counters every few seconds: no new reading yet
        self._ticks = (now, busy, total)
        self._ucd_pct = None
        if previous is not None:
            d_busy, d_total = (busy - previous[1]) & 0xFFFFFFFF, (total - previous[2]) & 0xFFFFFFFF
            if d_busy <= d_total:
                self._ucd_pct = round(d_busy / d_total * 100.0, 1)
        return self._ucd_pct

    def _counters(self, layout: Layout, values: Dict[Oid, Value], restarted: bool, read_at: float):
        """WanCounters, or why there are none. 64-bit counters when the agent has them, else the 32-bit ones (which
        wrap). Right after an agent restart the width is not given, so a counter that went back starts a new
        baseline instead of counting as a wrap."""
        if layout.if_index is None:
            return layout.wan_problem or "no WAN interface"
        i = layout.if_index
        hc_in = values[IF_HC_IN_OCTETS + (i,)].number(COUNTER64)
        hc_out = values[IF_HC_OUT_OCTETS + (i,)].number(COUNTER64)
        if hc_in is not None and hc_out is not None:
            return WanCounters(hc_in, hc_out, read_at=read_at)
        rx = values[IF_IN_OCTETS + (i,)].number(COUNTER32)
        tx = values[IF_OUT_OCTETS + (i,)].number(COUNTER32)
        if rx is not None and tx is not None:
            return WanCounters(rx, tx, counter_bits=None if restarted else 32, read_at=read_at)
        return f"the agent reports no octet counters for {layout.if_label} (IF-MIB ifHCInOctets / ifInOctets)"
