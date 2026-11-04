"""Concurrent network probers for ICMP ping and DNS resolution."""

import asyncio
import errno
import logging
import time
import socket
import sys
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple


try:
    import dns.asyncresolver
    import dns.resolver
    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False

from pengumon.models.probes import TargetConfig, ProbeSample, TargetStats
from pengumon.engine.prober_settings import DnsProbeConfig, HopStatusConfig
from pengumon.engine.icmp_base import IcmpBackend, IcmpUnavailable
from pengumon.engine.icmp_packet import ip_literal
from pengumon.engine.icmp_select import resolve_address, shared_backend
from pengumon.engine.netinfo import Ipv6Availability, is_no_ipv6_error, needs_route_check
from pengumon.engine.path_rules import ignoring_ping


logger = logging.getLogger("pengumon.prober")


def _send_fix() -> Optional[str]:
    """The step that lets this host send pings after a permission error (Linux: the sysctl line pengumon doctor prints)."""
    if not sys.platform.startswith("linux"):
        return None
    from pengumon.engine.icmp_linux import SYSCTL_FIX
    return SYSCTL_FIX


def _literal_version(address: str) -> Optional[int]:
    """4 or 6 for an IP literal, None for a host name."""
    try:
        return ip_literal(address)[1]
    except ValueError:
        return None


def _lookup(d: DnsProbeConfig) -> tuple:
    """What a DNS probe asks: a change to it makes its earlier statistics another lookup's."""
    return (d.domain, d.nameserver, d.record_type)


class NetworkProber:
    """Manages concurrent ICMP and DNS probing loops and rolling statistics."""

    def __init__(
        self,
        ping_timeout: float = 1.0,
        history_window: int = 60,
        stagger_seconds: float = 0.025,
        status_thresholds: Optional[HopStatusConfig] = None,
        backend: Optional[IcmpBackend] = None,
        ipv6: Optional[Ipv6Availability] = None,
        dns_timeout: Optional[float] = None,
    ):
        self.ping_timeout = ping_timeout
        self.dns_timeout = dns_timeout  # a lookup is lost after this; None: ping_timeout (prober.dns_timeout_seconds)
        self.status_thresholds = status_thresholds or HopStatusConfig()
        self.history_window = history_window
        self.stagger_seconds = stagger_seconds
        self.stats: Dict[str, TargetStats] = {}
        self.last_samples: Dict[str, ProbeSample] = {}
        self.last_round_ts: Optional[float] = None  # wall clock time the last probe round finished (health check)
        self._backend: Optional[IcmpBackend] = backend  # None: the app's shared one (icmp_select)
        self.ipv6 = ipv6 or Ipv6Availability()  # whether IPv6 targets can be probed from this host now
        self._families: Dict[str, int] = {}  # each ICMP target's family, to tell an edited hop from the same one
        self._warn_latency: Dict[str, float] = {}  # a hop's own "warn above" level (TargetConfig.warn_latency_ms)
        self._dns_lookups: Dict[str, tuple] = {}  # what each DNS probe's stats measure (domain, nameserver, type)
        # Lookups through the system resolver run in a thread that cannot be cancelled: one that outlives its timeout
        # keeps running (and holds one of the default executor's threads) until the resolver gives up.
        self._stuck_lock = threading.Lock()
        self.lookups_stuck = 0  # such lookups running now
        # Set when the system refused to send a ping (permission denied) and cleared by the next one that is sent: the
        # backend opened, yet no probe can go out, which is the same state as no backend (probing_problem).
        self._send_denied: Optional[IcmpUnavailable] = None

    def backend(self) -> IcmpBackend:
        """The ICMP backend probes go through; raises IcmpUnavailable when this host can't send ICMP."""
        return self._backend if self._backend is not None else shared_backend()

    def probing_problem(self) -> Optional[Tuple[str, Optional[str]]]:
        """(why, the one step that fixes it or None) while this host cannot send pings at all, else None: the ICMP
        backend can't be opened, or every send is refused with a permission error. Probes are then not sent, no loss is
        recorded and the classifier says so instead of reporting an outage (engine.loop)."""
        try:
            self.backend()
        except IcmpUnavailable as e:
            return e.reason, e.fix
        denied = self._send_denied
        return (denied.reason, denied.fix) if denied is not None else None

    def probe_method(self) -> Dict[str, Any]:
        """How probes are sent, for /api/info and pengumon doctor: the backend's name and RTT resolution, or why
        there is none (and the fix)."""
        try:
            backend = self.backend()
        except IcmpUnavailable as e:
            return {"backend": None, "resolution_ms": None, "error": e.reason, "fix": e.fix}
        return {"backend": backend.name, "resolution_ms": backend.resolution_ms, "error": None, "fix": None}

    def register_target(self, target: TargetConfig):
        """Initializes tracking stats for an ICMP target."""
        if target.id not in self.stats:
            self.stats[target.id] = TargetStats(
                target_id=target.id,
                name=target.name,
                category=target.category,
                address=target.address,
                enabled=target.enabled,
                color=target.color,
                group=target.group,
                recent_latencies=[],
            )
        self._families[target.id] = target.family
        if target.warn_latency_ms is not None:
            self._warn_latency[target.id] = target.warn_latency_ms
        else:
            self._warn_latency.pop(target.id, None)

    def set_targets(self, targets: List[TargetConfig], dns_ids: Iterable[str] = ()):
        """Makes `targets` (in path order) the ICMP targets, without a restart (an applied path edit): a new one
        starts with empty statistics, a removed one is forgotten, one whose address or family changed starts over, the others
        keep their statistics and take the new name, role, colour and group. The DNS probes (`dns_ids`) stay, after
        the ICMP targets. The probe loop sends to the new list from its next round."""
        old = self.stats
        keep_dns = {i: old[i] for i in dns_ids if i in old}
        families = self._families
        self._families = {}
        self._warn_latency = {}
        self.stats = {}
        for t in targets:
            st = old.get(t.id)
            same = st is not None and st.address == t.address and families.get(t.id, t.family) == t.family
            if same and t.id not in keep_dns:
                st.name, st.category, st.enabled, st.color, st.group = t.name, t.category, t.enabled, t.color, t.group
                self.stats[t.id] = st
            else:
                self.last_samples.pop(t.id, None)
            self.register_target(t)
        self.stats.update(keep_dns)
        self._dns_lookups = {i: v for i, v in self._dns_lookups.items() if i in keep_dns}
        live = set(self.stats)
        for tid in [k for k in self.last_samples if k not in live]:
            del self.last_samples[tid]
        self.update_path_flags()

    def set_dns_targets(self, dns_probes: List[DnsProbeConfig]) -> None:
        """Makes `dns_probes` the DNS probes, without a restart (a saved DNS lookups list): a new one starts with
        empty statistics, a removed one is forgotten, one whose domain, nameserver or record type changed starts
        over, the others keep their statistics and take the new name, enabled flag and colour. The ICMP targets keep
        theirs and stay first, the DNS probes follow in list order. The probe loop reads the list (config.dns_probes)
        each round, so it probes the new ones from its next round."""
        dns_ids = set(self._dns_lookups)
        old = {i: st for i, st in self.stats.items() if i in dns_ids}
        lookups = self._dns_lookups
        self._dns_lookups = {}
        self.stats = {i: st for i, st in self.stats.items() if i not in dns_ids}
        for d in dns_probes:
            st = old.get(d.id)
            if st is not None and lookups.get(d.id) == _lookup(d):
                st.name, st.enabled, st.color = d.name, d.enabled, d.color
                self.stats[d.id] = st
                self._dns_lookups[d.id] = lookups[d.id]
            else:
                self.last_samples.pop(d.id, None)
            self.register_dns_target(d)
        live = set(self.stats)
        for tid in [k for k in self.last_samples if k not in live]:
            del self.last_samples[tid]
        self.update_path_flags()

    def register_dns_target(self, dns_cfg: DnsProbeConfig):
        """Initializes tracking stats for a DNS probe, and records the lookup they measure (kept until the stats
        start over: a probe round that registers an edited lookup before set_dns_targets runs must not make the old
        statistics look like the new lookup's)."""
        if dns_cfg.id not in self.stats:
            self._dns_lookups[dns_cfg.id] = _lookup(dns_cfg)
            self.stats[dns_cfg.id] = TargetStats(
                target_id=dns_cfg.id,
                name=dns_cfg.name,
                category="dns",
                address=dns_cfg.domain + (f" @{dns_cfg.nameserver}" if dns_cfg.nameserver else " @system"),
                enabled=dns_cfg.enabled,
                color=dns_cfg.color,
                recent_latencies=[],
            )

    async def probe_icmp(self, target: TargetConfig) -> Optional[ProbeSample]:
        """One echo request to the target through the ICMP backend. None for a disabled target, and for an IPv6
        target while this host has no IPv6 (its stats say ipv6_unavailable: no loss is recorded, it is not an
        outage). Only an echo reply is a success; a Time Exceeded (a routing loop at TTL 64), an Unreachable or no
        answer is a loss whose `error` says which."""
        if not target.enabled:
            return None  # nothing is sent and nothing recorded: a fake success would pollute stats and history

        ts = time.time()

        def lost(error: str) -> ProbeSample:
            self._flag_ipv6(target, False)
            return ProbeSample(target_id=target.id, timestamp=ts, success=False, latency_ms=None,
                               packet_loss_pct=100.0, error=error)

        try:
            backend = self.backend()
        except IcmpUnavailable:
            return None  # not a loss and not an outage: probing_problem says why nothing is measured
        # IPv6 at all? Asked before a name is looked up: an AAAA record is no use without a route
        literal = _literal_version(target.address)
        if (literal or target.family) == 6 and needs_route_check(target.address) and not self.ipv6.available():
            return self._flag_ipv6(target, True)
        try:
            address, version = await resolve_address(target.address, self.ping_timeout, target.family)
        except ValueError as e:
            return lost(str(e))
        except OSError as e:
            return lost(f"Name lookup failed: {e}")
        if version == 6 and literal is None and needs_route_check(address) and not self.ipv6.available():
            return self._flag_ipv6(target, True)
        try:
            result = await backend.probe(address, timeout=self.ping_timeout)
        except OSError as e:
            if version == 6 and is_no_ipv6_error(e):
                self.ipv6.no_route(e)
                return self._flag_ipv6(target, True)
            if e.errno in (errno.EPERM, errno.EACCES):  # not allowed to send pings: nothing was measured
                self._send_denied = IcmpUnavailable(f"the system does not let Pengumon send pings ({e})", _send_fix())
                return None
            return lost(f"ICMP send failed: {e}")
        self._send_denied = None
        self._flag_ipv6(target, False)
        if result.kind == "reply" and result.rtt_ms is not None:
            return ProbeSample(target_id=target.id, timestamp=ts, success=True, latency_ms=round(result.rtt_ms, 2),
                               packet_loss_pct=0.0)
        if result.kind == "ttl_exceeded":
            return lost(f"TTL exceeded in transit at {result.responder or 'an unknown router'} (routing loop?)")
        if result.kind == "unreachable":
            return lost(f"Destination unreachable (reported by {result.responder or 'an unknown router'})")
        return lost("Request timed out")

    def _flag_ipv6(self, target: TargetConfig, unavailable: bool) -> None:
        """Marks the target's stats as an IPv6 target this host cannot probe now (or no longer). Returns None: no
        sample."""
        stat = self.stats.get(target.id)
        if stat is not None:
            stat.ipv6_unavailable = unavailable
        return None

    @property
    def lookup_timeout(self) -> float:
        """How long a DNS lookup may take before it counts as lost."""
        return self.dns_timeout if self.dns_timeout is not None else self.ping_timeout

    async def probe_dns(self, dns_cfg: DnsProbeConfig) -> Optional[ProbeSample]:
        """Performs an active DNS resolution and measures query latency. None for a disabled probe."""
        if not dns_cfg.enabled:
            return None

        ts = time.time()
        start_t = time.perf_counter()

        if HAS_DNSPYTHON and dns_cfg.nameserver:
            try:
                resolver = dns.asyncresolver.Resolver()
                resolver.lifetime = self.lookup_timeout
                resolver.nameservers = [dns_cfg.nameserver]
                answers = await resolver.resolve(dns_cfg.domain, dns_cfg.record_type)
                latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
                if answers:
                    return ProbeSample(
                        target_id=dns_cfg.id,
                        timestamp=ts,
                        success=True,
                        latency_ms=latency_ms,
                        packet_loss_pct=0.0
                    )
                else:
                    return ProbeSample(
                        target_id=dns_cfg.id,
                        timestamp=ts,
                        success=False,
                        latency_ms=None,
                        packet_loss_pct=100.0,
                        error="No DNS records returned"
                    )
            except Exception as e:
                return ProbeSample(
                    target_id=dns_cfg.id,
                    timestamp=ts,
                    success=False,
                    latency_ms=None,
                    packet_loss_pct=100.0,
                    error=f"DNS Error: {str(e)}"
                )
        else:
            # Standard library socket resolution in separate thread
            lookup = {"done": False, "stuck": False}

            def _resolve_sync():
                try:
                    return socket.getaddrinfo(dns_cfg.domain, 80, family=socket.AF_INET)
                finally:
                    with self._stuck_lock:
                        lookup["done"] = True
                        if lookup["stuck"]:
                            self.lookups_stuck -= 1

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_resolve_sync),
                    timeout=self.lookup_timeout
                )
                latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
                return ProbeSample(
                    target_id=dns_cfg.id,
                    timestamp=ts,
                    success=True,
                    latency_ms=latency_ms,
                    packet_loss_pct=0.0
                )
            except asyncio.TimeoutError:
                with self._stuck_lock:
                    if not lookup["done"]:  # the thread is still in the resolver
                        lookup["stuck"] = True
                        self.lookups_stuck += 1
                return ProbeSample(
                    target_id=dns_cfg.id,
                    timestamp=ts,
                    success=False,
                    latency_ms=None,
                    packet_loss_pct=100.0,
                    error="DNS Resolution Timeout"
                )
            except Exception as e:
                return ProbeSample(
                    target_id=dns_cfg.id,
                    timestamp=ts,
                    success=False,
                    latency_ms=None,
                    packet_loss_pct=100.0,
                    error=f"DNS Error: {str(e)}"
                )



    async def execute_probe_round(
        self,
        targets: List[TargetConfig],
        dns_probes: List[DnsProbeConfig]
    ) -> List[ProbeSample]:
        """Executes all ICMP and DNS probes concurrently, started a few ms apart.

        Firing every probe at once queues them behind each other in the container's network
        proxy (some VM-based runtimes), which inflated sub-millisecond LAN readings by ~0.2 ms.
        """
        async def after(delay: float, probe):
            if delay:
                await asyncio.sleep(delay)
            return await probe

        tasks = []
        for i, t in enumerate(targets):
            self.register_target(t)
            tasks.append(after(i * self.stagger_seconds, self.probe_icmp(t)))

        for j, d in enumerate(dns_probes, start=len(targets)):
            self.register_dns_target(d)
            tasks.append(after(j * self.stagger_seconds, self.probe_dns(d)))

        samples: List[ProbeSample] = [
            s for s in await asyncio.gather(*tasks, return_exceptions=False) if s is not None
        ]
        
        # Update rolling statistics for each target
        for sample in samples:
            self._update_target_stats(sample)
        self.update_path_flags()

        self.last_round_ts = time.time()
        return samples

    def update_path_flags(self):
        """Marks the targets that ignore ping (engine.path_rules), from every target's status after this round. It is
        observed only: no setting overrides it."""
        quiet = ignoring_ping(self.stats.values())
        blind = self.probing_problem() is not None
        for tid, st in self.stats.items():
            st.ignores_ping = tid in quiet
            st.probing_unavailable = blind

    def _update_target_stats(self, sample: ProbeSample):
        """Updates WinMTR-style rolling stats for a target."""
        stat = self.stats.get(sample.target_id)
        if not stat:
            return

        stat.sent += 1
        if sample.success and sample.latency_ms is not None:
            stat.received += 1
            stat.last_ms = sample.latency_ms
            
            # Min / Max / Avg calculation
            if stat.min_ms is None or sample.latency_ms < stat.min_ms:
                stat.min_ms = sample.latency_ms
            if stat.max_ms is None or sample.latency_ms > stat.max_ms:
                stat.max_ms = sample.latency_ms
            
            # Rolling window calculation
            stat.recent_latencies.append(sample.latency_ms)
            if len(stat.recent_latencies) > self.history_window:
                stat.recent_latencies.pop(0)

            valid_lats = [x for x in stat.recent_latencies if x is not None]
            if valid_lats:
                stat.avg_ms = round(sum(valid_lats) / len(valid_lats), 2)
                # Jitter = Mean absolute difference between consecutive samples
                if len(valid_lats) > 1:
                    diffs = [abs(valid_lats[i] - valid_lats[i-1]) for i in range(1, len(valid_lats))]
                    stat.jitter_ms = round(sum(diffs) / len(diffs), 2)
                    sample.jitter_ms = stat.jitter_ms
        else:
            stat.recent_latencies.append(None)
            if len(stat.recent_latencies) > self.history_window:
                stat.recent_latencies.pop(0)

        # Lifetime Loss % calculation based on sent/received
        if stat.sent > 0:
            stat.loss_pct = round(((stat.sent - stat.received) / stat.sent) * 100.0, 1)

        # Rolling Recent Loss % (over recent 60-sample / 1-minute window)
        if stat.recent_latencies:
            lost_recent_count = stat.recent_latencies.count(None)
            stat.loss_recent_pct = round((lost_recent_count / len(stat.recent_latencies)) * 100.0, 1)
        else:
            stat.loss_recent_pct = 0.0

        # Category-aware status evaluation (latency & jitter)

        recent_10 = stat.recent_latencies[-10:] if len(stat.recent_latencies) >= 10 else stat.recent_latencies
        if recent_10:
            lost_recent = recent_10.count(None)
            loss_rate_recent = lost_recent / len(recent_10)
            # Loss thresholds are fractions, compared strictly: as percentages 0.4 * 100 is 40.00000000000001.
            t = self.status_thresholds
            limits = t.latency_limits(stat.category)
            warn = self._warn_latency.get(sample.target_id)
            if warn is not None:  # the hop's own latency level; ISP and custom hops get a latency-only limit
                limits = (warn, limits[1] if limits else None)
            if loss_rate_recent > t.down_loss_fraction:
                stat.status = "down"
            elif loss_rate_recent > t.degraded_loss_fraction:
                stat.status = "degraded"
            elif limits and ((stat.avg_ms and stat.avg_ms > limits[0])
                             or (limits[1] is not None and stat.jitter_ms and stat.jitter_ms > limits[1])):
                stat.status = "degraded"
            else:
                stat.status = "up"
        
        self.last_samples[sample.target_id] = sample

