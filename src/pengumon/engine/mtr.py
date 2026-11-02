"""Hop-by-hop traceroute discovery engine (MTR style)."""

import asyncio
import logging
import platform
import re
from typing import List, Optional, Tuple
from pydantic import BaseModel

logger = logging.getLogger("pengumon.mtr")

TRACERT_WAIT_MS = 1000

_IP_RE = r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F]*:[0-9a-fA-F:]+)"
# One tracert probe column: "<1 ms", "12 ms" or "*"
_TRACERT_COL = r"(?:<?\d+\s*ms|\*)"
_TRACERT_LINE = re.compile(rf"^\s*(\d+)\s+({_TRACERT_COL})\s+({_TRACERT_COL})\s+({_TRACERT_COL})\s+(.*?)\s*$")
_TRACERT_IP = re.compile(rf"\[?{_IP_RE}\]?$")
_TRACEROUTE_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")

ParsedHop = Tuple[int, Optional[str], Optional[float]]


class TracerouteHop(BaseModel):
    hop_number: int
    ip: Optional[str] = None
    hostname: Optional[str] = None
    rtt_ms: Optional[float] = None
    label: Optional[str] = None


def parse_tracert_output(text: str) -> List[ParsedHop]:
    """Parses Windows `tracert -d` output into (hop, ip, average rtt ms) tuples.

    tracert only has 1 ms resolution; "<1 ms" is counted as 0.5 ms. Timed-out hops ("*  *  *")
    are kept with ip and rtt set to None.
    """
    hops: List[ParsedHop] = []
    for line in text.splitlines():
        m = _TRACERT_LINE.match(line)
        if not m:
            continue
        rtts = []
        for col in m.group(2, 3, 4):
            if col != "*":
                ms = float(re.match(r"<?(\d+)", col).group(1))
                rtts.append(0.5 if col.startswith("<") else ms)
        ip_m = _TRACERT_IP.search(m.group(5)) if rtts else None
        avg = round(sum(rtts) / len(rtts), 2) if rtts else None
        hops.append((int(m.group(1)), ip_m.group(1) if ip_m else None, avg))
    return hops


def parse_traceroute_output(text: str) -> List[ParsedHop]:
    """Parses Linux `traceroute -n` output into (hop, ip, average rtt ms) tuples."""
    hops: List[ParsedHop] = []
    for line in text.splitlines():
        m = _TRACEROUTE_LINE.match(line)
        if not m:
            continue
        rest = m.group(2)
        ip_m = re.search(_IP_RE, rest)
        rtts = [float(x) for x in re.findall(r"([\d.]+)\s*ms", rest)]
        avg = round(sum(rtts) / len(rtts), 2) if rtts else None
        hops.append((int(m.group(1)), ip_m.group(1) if ip_m else None, avg))
    return hops


class MtrEngine:
    """Discovers and updates hop-by-hop route path."""

    def __init__(
        self,
        target_host: str = "1.1.1.1",
        max_hops: int = 15,
        timeout: float = 1.5,
        known_targets: Optional[List] = None,
    ):
        if target_host.startswith("-"):
            # Never hand an option-like value to traceroute
            raise ValueError("invalid target host")
        self.target_host = target_host
        self.max_hops = max_hops
        self.timeout = timeout
        self.known_targets = known_targets or []
        self._is_windows = platform.system().lower() == "windows"

    async def run_trace(self) -> List[TracerouteHop]:
        """Runs a traceroute to discover all hops to the destination."""
        if self._is_windows:
            return await self._trace_windows()
        else:
            return await self._trace_linux()

    async def _trace_windows(self) -> List[TracerouteHop]:
        """Executes tracert on Windows and parses hops."""
        try:
            # tracert -d -h 15 -w 1000 1.1.1.1 (-d prevents slow reverse DNS on every hop)
            cmd = ["tracert", "-d", "-h", str(self.max_hops), "-w", str(TRACERT_WAIT_MS), self.target_host]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25.0)
            return self._build_hops(parse_tracert_output(stdout.decode("utf-8", errors="ignore")))
        except Exception as ex:
            logger.warning(f"Windows tracert failed: {ex}")
        return []

    async def _trace_linux(self) -> List[TracerouteHop]:
        """Executes traceroute on Linux."""
        try:
            # "--" ends option parsing, so the target is always read as a host
            cmd = ["traceroute", "-n", "-m", str(self.max_hops), "-w", "1", "--", self.target_host]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            return self._build_hops(parse_traceroute_output(stdout.decode("utf-8", errors="ignore")))
        except Exception as ex:
            logger.warning(f"Linux traceroute failed: {ex}")
        return []

    def _build_hops(self, parsed: List[ParsedHop]) -> List[TracerouteHop]:
        return [
            TracerouteHop(hop_number=hop_no, ip=ip, rtt_ms=rtt, label=self._label_hop(hop_no, ip))
            for hop_no, ip, rtt in parsed
        ]

    def _label_hop(self, hop_no: int, ip: Optional[str]) -> str:
        """Assigns semantic labels to discovered hops."""
        if not ip:
            return "Request Timed Out"

        # Check against known configured targets first
        for t in self.known_targets:
            t_addr = getattr(t, "address", None)
            t_name = getattr(t, "name", None)
            if t_addr and t_name and t_addr == ip:
                return t_name

        if hop_no == 1:
            return f"Hop 1: Local Router ({ip})"
        if ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")):
            return f"Hop {hop_no}: Private Gateway ({ip})"
        if hop_no in (2, 3):
            return f"Hop {hop_no}: ISP Node / CMTS ({ip})"
        if ip in ("1.1.1.1", "1.0.0.1"):
            return "Cloudflare Anycast (Target)"
        if ip in ("8.8.8.8", "8.8.4.4"):
            return "Google Anycast (Target)"
        return f"Transit Router ({ip})"
