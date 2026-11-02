"""Concurrent network probers for ICMP ping and DNS resolution."""

import asyncio
import ipaddress
import logging
import platform
import time
import socket
from typing import List, Dict, Optional


try:
    import dns.asyncresolver
    import dns.resolver
    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False

from pengumon.models.probes import TargetConfig, ProbeSample, TargetStats
from pengumon.engine.prober_settings import DnsProbeConfig, HopStatusConfig
from pengumon.engine.icmp import KernelTimestampPinger
from pengumon.engine.path_rules import ignoring_ping


logger = logging.getLogger("pengumon.prober")


def _is_ipv4(address: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


class NetworkProber:
    """Manages concurrent ICMP and DNS probing loops and rolling statistics."""

    def __init__(
        self,
        ping_timeout: float = 1.0,
        history_window: int = 60,
        stagger_seconds: float = 0.025,
        status_thresholds: Optional[HopStatusConfig] = None,
    ):
        self.ping_timeout = ping_timeout
        self.status_thresholds = status_thresholds or HopStatusConfig()
        self.history_window = history_window
        self.stagger_seconds = stagger_seconds
        self.stats: Dict[str, TargetStats] = {}
        self.last_samples: Dict[str, ProbeSample] = {}
        self.last_round_ts: Optional[float] = None  # wall clock time the last probe round finished (health check)
        self._is_windows = platform.system().lower() == "windows"
        self._kernel_pinger: Optional[KernelTimestampPinger] = None
        self._kernel_pinger_failed = False

    def _get_kernel_pinger(self) -> Optional[KernelTimestampPinger]:
        """Kernel-timestamped ICMP (Linux, raw or ping socket); None means use icmplib instead."""
        if self._kernel_pinger is None and not self._kernel_pinger_failed:
            try:
                self._kernel_pinger = KernelTimestampPinger()
                logger.info(f"ICMP probes use kernel receive timestamps (SO_TIMESTAMPNS, {self._kernel_pinger.kind} socket).")
            except OSError as e:
                self._kernel_pinger_failed = True
                logger.info(f"Kernel-timestamped ICMP unavailable ({e}); using icmplib.")
        return self._kernel_pinger

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
                recent_latencies=[],
            )

    def register_dns_target(self, dns_cfg: DnsProbeConfig):
        """Initializes tracking stats for a DNS probe."""
        if dns_cfg.id not in self.stats:
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
        """Probes a host via ICMP echo, with fallbacks for socket permissions. None for a disabled target."""
        if not target.enabled:
            return None  # nothing is sent and nothing recorded: a fake success would pollute stats and history

        ts = time.time()
        pinger = self._get_kernel_pinger()
        if pinger and _is_ipv4(target.address):
            try:
                rtt = await pinger.ping(target.address, self.ping_timeout)
            except OSError as e:
                return ProbeSample(target_id=target.id, timestamp=ts, success=False, latency_ms=None,
                                   packet_loss_pct=100.0, error=f"ICMP send failed: {e}")
            if rtt is None:
                return ProbeSample(target_id=target.id, timestamp=ts, success=False, latency_ms=None,
                                   packet_loss_pct=100.0, error="Request timed out")
            return ProbeSample(target_id=target.id, timestamp=ts, success=True, latency_ms=round(rtt, 2),
                               packet_loss_pct=0.0)

        try:
            # Try true ICMP raw socket ping first (enabled by NET_RAW capability)
            from icmplib import async_ping
            try:
                res = await async_ping(
                    target.address,
                    count=1,
                    timeout=self.ping_timeout,
                    privileged=True
                )
            except PermissionError:
                # Fallback to unprivileged if permissions restricted
                res = await async_ping(
                    target.address,
                    count=1,
                    timeout=self.ping_timeout,
                    privileged=False
                )

            if res.is_alive and res.avg_rtt >= 0:
                latency = round(res.avg_rtt, 2)
                return ProbeSample(
                    target_id=target.id,
                    timestamp=ts,
                    success=True,
                    latency_ms=latency,
                    packet_loss_pct=0.0
                )
            elif res.packet_loss > 0 or not res.is_alive:
                return ProbeSample(
                    target_id=target.id,
                    timestamp=ts,
                    success=False,
                    latency_ms=None,
                    packet_loss_pct=100.0,
                    error="Request timed out"
                )
        except Exception:
            return await self._system_ping_fallback(target, ts)


    async def _system_ping_fallback(self, target: TargetConfig, ts: float) -> ProbeSample:
        """Invokes OS ping command as fallback."""
        if target.address.startswith("-"):
            # Never hand an option-like value to ping
            return ProbeSample(target_id=target.id, timestamp=ts, success=False, latency_ms=None,
                               packet_loss_pct=100.0, error="invalid address")
        try:
            if self._is_windows:
                # ping -n 1 -w 1000 <ip>
                timeout_ms = int(self.ping_timeout * 1000)
                cmd = ["ping", "-n", "1", "-w", str(timeout_ms), target.address]
            else:
                # ping -c 1 -W 1 -- <ip>  ("--" ends option parsing)
                timeout_sec = int(max(1, self.ping_timeout))
                cmd = ["ping", "-c", "1", "-W", str(timeout_sec), "--", target.address]

            start_t = time.perf_counter()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.ping_timeout + 0.5)
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)

            if proc.returncode == 0:
                # Parse stdout for exact latency if possible
                out_str = stdout.decode("utf-8", errors="ignore")
                parsed_ms = self._parse_ping_stdout(out_str) or elapsed_ms
                return ProbeSample(
                    target_id=target.id,
                    timestamp=ts,
                    success=True,
                    latency_ms=parsed_ms,
                    packet_loss_pct=0.0
                )
            else:
                return ProbeSample(
                    target_id=target.id,
                    timestamp=ts,
                    success=False,
                    latency_ms=None,
                    packet_loss_pct=100.0,
                    error="Host unreachable / timed out"
                )
        except Exception as e:
            return ProbeSample(
                target_id=target.id,
                timestamp=ts,
                success=False,
                latency_ms=None,
                packet_loss_pct=100.0,
                error=str(e)
            )

    def _parse_ping_stdout(self, text: str) -> Optional[float]:
        """Extracts latency ms from Windows or Linux ping output."""
        try:
            import re
            # Match time=1.46 ms or time<1ms or time=12ms or time=0.892 ms
            m = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", text, re.IGNORECASE)
            if m:
                return float(m.group(1))
            # Match Linux rtt min/avg/max/mdev = 1.462/2.798/6.123/1.927 ms
            m = re.search(r"rtt [^=]+=\s*[\d.]+/([\d.]+)/", text, re.IGNORECASE)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return None


    async def probe_dns(self, dns_cfg: DnsProbeConfig) -> Optional[ProbeSample]:
        """Performs an active DNS resolution and measures query latency. None for a disabled probe."""
        if not dns_cfg.enabled:
            return None

        ts = time.time()
        start_t = time.perf_counter()

        if HAS_DNSPYTHON and dns_cfg.nameserver:
            try:
                resolver = dns.asyncresolver.Resolver()
                resolver.lifetime = self.ping_timeout
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
            try:
                def _resolve_sync():
                    return socket.getaddrinfo(dns_cfg.domain, 80, family=socket.AF_INET)

                await asyncio.wait_for(
                    asyncio.to_thread(_resolve_sync),
                    timeout=self.ping_timeout
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
        """Marks the targets that ignore ping (engine.path_rules), from every target's status after this round."""
        quiet = ignoring_ping(self.stats.values())
        for tid, st in self.stats.items():
            st.ignores_ping = tid in quiet

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
            if loss_rate_recent > t.down_loss_fraction:
                stat.status = "down"
            elif loss_rate_recent > t.degraded_loss_fraction:
                stat.status = "degraded"
            elif limits and ((stat.avg_ms and stat.avg_ms > limits[0]) or (stat.jitter_ms and stat.jitter_ms > limits[1])):
                stat.status = "degraded"
            else:
                stat.status = "up"
        
        self.last_samples[sample.target_id] = sample

