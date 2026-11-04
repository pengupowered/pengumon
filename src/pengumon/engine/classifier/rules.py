"""The diagnosis rules as pure functions of the round metrics (no clock, no state, no I/O).

Every level comes from the ClassifierConfig (the Settings page's outage rules), except the named internal constants
below; tests/test_classifier_literals.py fails if a level is written here as a number again."""

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.classifier.metrics import RoundMetrics
from pengumon.engine.classifier.verdict import NOMINAL, Verdict
from pengumon.engine.load import LoadReading
from pengumon.lines import GENERIC, LineWording

# Internal (not settings): DNS is judged only while every lower layer loses under this (H4) ...
DNS_GUARD_LOSS_PCT = 10.0
# ... and an ISP outage is described as the ISP hop timing out from this ISP-hop loss, else as peering (H6).
ISP_HOP_WORDING_LOSS_PCT = 30.0


def _lan_answers(m: RoundMetrics) -> str:
    """What in the home network was seen answering, for "... while X answer(s) normally"."""
    if m.local.measured and m.gateway.measured:
        return "the local router and the gateway answer"
    if m.gateway.measured:
        return "the gateway answers"
    return "the local router answers"


def _past_gateway(m: RoundMetrics, line: LineWording, causes: str) -> str:
    """The possible causes of loss from the provider's first hop on. Without a gateway target nothing shows the
    gateway answering, so it stays a possible cause."""
    return causes if m.gateway.measured else f"{line.gateway}, not monitored here; {causes}"


def diagnose(m: RoundMetrics, config: ClassifierConfig, ts: float, last_upstream_issue_ts: float,
             line: LineWording = GENERIC) -> Verdict:
    """Determines the specific root-cause code and explanation. `line` phrases the gateway and the possible causes of
    a fault on the access line for its technology (pengumon.lines)."""
    local_loss, local_avg_lat, local_jitter = m.local.loss_pct, m.local.avg_latency_ms, m.local.jitter_ms
    gateway_loss, gateway_avg_lat, gateway_jitter = m.gateway.loss_pct, m.gateway.avg_latency_ms, m.gateway.jitter_ms
    isp_loss, isp_avg_lat, isp_jitter = m.isp.loss_pct, m.isp.avg_latency_ms, m.isp.jitter_ms
    public_loss, public_avg_lat, public_jitter = m.public.loss_pct, m.public.avg_latency_ms, m.public.jitter_ms
    dns_loss, dns_avg_lat = m.dns.loss_pct, m.dns.avg_latency_ms
    degraded, down = config.degraded_loss_pct, config.down_loss_pct
    isp_latency_level = config.isp_latency_spike_threshold_ms or config.public_latency_spike_threshold_ms

    # Friendly target names and addresses for the descriptions
    local_name, local_addr = m.local.name("Local Router"), m.local.address("LAN")
    gw_name, gw_addr = m.gateway.name("Modem / Gateway"), m.gateway.address("Gateway")
    isp_name, isp_addr = m.isp.name("ISP Regional Hub"), m.isp.address("ISP Network")

    # 1. Local Wi-Fi / Mesh Router Issue
    if local_loss >= down:
        return Verdict(
            kind="local_down",
            status="down",
            cause="local_wifi_mesh_drop",
            title=f"{local_name} Disconnect",
            description=f"Sustained packet loss ({local_loss}%) reaching {local_name} ({local_addr}). Possible causes: Ethernet backhaul cabling or the local Wi-Fi interface." + (
                "" if m.gateway.measured else
                " No separate gateway is monitored, so this router may also be the internet gateway: the router "
                "itself is a possible cause."),
            severity="critical",
            affected=m.failed_targets,
        )
    if local_loss >= degraded:
        return Verdict(
            kind="local_loss",
            status="degraded",
            cause="local_wifi_mesh_drop",
            title=f"{local_name} Packet Loss",
            description=f"{local_name} ({local_addr}) is dropping {local_loss}% of packets. Possible causes: Wi-Fi congestion or mesh node backhaul stutter.",
            severity="warning",
            affected=m.failed_targets,
        )
    if (local_avg_lat and local_avg_lat > config.local_latency_spike_threshold_ms) or (local_jitter and local_jitter > config.local_jitter_spike_threshold_ms):
        return Verdict(
            kind="local_latency",
            status="degraded",
            cause="local_wifi_mesh_drop",
            title=f"{local_name} Latency Spike",
            description=f"Latency to {local_name} ({local_addr}) is {local_avg_lat}ms (jitter {local_jitter}ms; a wired LAN is typically under 2ms). Possible causes: local Wi-Fi or network interference.",
            severity="warning",
            affected=m.failed_targets,
        )

    # 2. The gateway (modem), probed on its home-side address. Those probes never use the provider's line, so loss
    #    here is the gateway itself or the link from the router to it, not the line.
    gw_side = f"{gw_name} ({gw_addr}), {line.gateway} on its home-side address,"
    if gateway_loss >= down and local_loss < degraded:
        return Verdict(
            kind="gateway_down",
            status="down",
            cause="gateway_unreachable",
            title=f"{gw_name} Not Answering",
            description=f"{gw_side} is not answering ({gateway_loss}% of probes lost) while {local_name} answers normally. These probes do not use the provider's line. Possible causes: the gateway itself (restarting, overloaded, its firmware or power) or the link from the router to it.",
            severity="critical",
            affected=m.failed_targets,
        )
    if gateway_loss >= degraded:
        return Verdict(
            kind="gateway_loss",
            status="degraded",
            cause="gateway_unreachable",
            title=f"{gw_name} Packet Loss",
            description=f"{gw_side} is dropping {gateway_loss}% of probes while {local_name} answers normally. These probes do not use the provider's line. Possible causes: the gateway itself (overloaded, its firmware or power) or the link from the router to it.",
            severity="warning",
            affected=m.failed_targets,
        )
    if (gateway_avg_lat and gateway_avg_lat > config.gateway_latency_spike_threshold_ms) or (gateway_jitter and gateway_jitter > config.gateway_jitter_spike_threshold_ms):
        return Verdict(
            kind="gateway_latency",
            status="degraded",
            cause="double_nat_congestion",
            title=f"{gw_name} Latency Delay",
            description=f"Latency to {gw_side} is {gateway_avg_lat}ms (jitter {gateway_jitter}ms). Possible causes: a busy gateway or a busy link from the router to it.",
            severity="warning",
            affected=m.failed_targets,
        )

    # 3a. No LAN target at all: loss towards the internet cannot be placed on the line, since nothing shows the
    #     local network working.
    if not m.local.measured and not m.gateway.measured and (isp_loss >= degraded or public_loss >= degraded):
        worst = max(isp_loss, public_loss)
        is_down = worst >= down
        return Verdict(
            kind="internet_outage" if is_down else "internet_loss",
            status="down" if is_down else "degraded",
            cause="unknown_outage",
            title="Internet Unreachable" if is_down else "Internet Packet Loss",
            description=f"{worst}% of probes to internet targets were lost. No local router or gateway target is "
                        "configured, so a fault in the local network cannot be told apart from one at the ISP.",
            severity="critical" if is_down else "warning",
            affected=m.failed_targets,
        )

    # 3. Loss from the provider's first hop on while the gateway answers: the line (from the gateway to that hop) or
    #    the provider's first router. Loss only past it (the ISP hop answers, the internet does not): the provider's
    #    network beyond, not the line.
    if (isp_loss >= down or public_loss >= down) and local_loss < degraded:
        if isp_loss >= ISP_HOP_WORDING_LOSS_PCT:
            cause, title = "access_line_drop", f"Line or {isp_name} Outage"
            detail = f"Packets to {isp_name} ({isp_addr}) are timing out ({isp_loss}% loss) while {_lan_answers(m)} normally. Possible causes: {_past_gateway(m, line, line.outage)}."
        elif not m.isp.measured:
            # no provider hop to tell the line from what lies beyond it: the line stays first among the causes
            cause, title = "access_line_drop", "Line or Provider Outage"
            detail = f"Traffic to the public internet is timing out ({public_loss}% loss) while {_lan_answers(m)} normally. No provider hop is monitored, so the line cannot be told apart from the provider's network. Possible causes: {_past_gateway(m, line, line.outage)}, or a failure further into the provider's network."
        else:
            cause, title = "internet_backbone_transit_loss", "ISP Backbone Outage"
            subject = "The gateway" if m.gateway.measured else "The local router"
            detail = f"{subject} and {isp_name} answer, but public internet traffic is timing out ({public_loss}% loss). Possible causes: an ISP peering or backbone link failure."
        return Verdict(
            kind="isp_outage",
            status="down",
            cause=cause,
            title=title,
            description=detail,
            severity="critical",
            affected=m.failed_targets,
        )
    if isp_loss >= degraded or public_loss >= degraded:
        if isp_loss >= degraded:
            kind, cause = "isp_loss", "access_line_drop"
            title = f"Line or {isp_name} Packet Loss"
            detail = f"{isp_name} ({isp_addr}) lost {isp_loss}% of recent probes while {_lan_answers(m)} normally. Possible causes: {_past_gateway(m, line, line.loss)}, or the hop deprioritising ping replies."
        else:
            kind, cause = "backbone_loss", "internet_backbone_transit_loss"
            title = "ISP Upstream Backbone Packet Loss"
            detail = f"{public_loss}% of probes to public internet targets were lost while the local network and {isp_name} answer normally. Possible causes: ISP upstream peering or backbone congestion."
            if not m.isp.measured:
                detail = f"{public_loss}% of probes to public internet targets were lost while the local network answers normally. No provider hop is monitored, so this may also be the line. Possible causes: ISP upstream peering or backbone congestion, or {_past_gateway(m, line, line.loss)}."
        return Verdict(
            kind=kind,
            status="degraded",
            cause=cause,
            title=title,
            description=detail,
            severity="warning",
            affected=m.failed_targets,
        )
    if (isp_avg_lat and isp_avg_lat > isp_latency_level) or (public_avg_lat and public_avg_lat > config.public_latency_spike_threshold_ms) or (public_jitter and public_jitter > config.public_jitter_spike_threshold_ms):
        lat = max(isp_avg_lat or 0, public_avg_lat or 0)
        jit = max(isp_jitter or 0, public_jitter or 0)
        return Verdict(
            kind="upstream_latency",
            status="degraded",
            cause="access_line_drop",
            title="ISP Latency Spikes / Jitter",
            description=f"Latency across upstream routes is {lat}ms (jitter {jit}ms), above the configured spike thresholds. Possible causes: upstream congestion or a busy line.",
            severity="warning",
            affected=m.failed_targets,
        )

    # 4. DNS Resolver Outage (Only evaluated if physical layers: Local, Gateway, and Public IP routing are all healthy!)
    guard = DNS_GUARD_LOSS_PCT
    if local_loss < guard and gateway_loss < guard and isp_loss < guard and public_loss < guard:

        # Check cooldown from recent upstream ISP / gateway drops
        if ts - last_upstream_issue_ts > config.dns_cooldown_seconds:
            if dns_loss >= down:
                return Verdict(
                    kind="dns_outage",
                    status="down",
                    cause="dns_resolver_failure",
                    title="DNS Resolution Outage",
                    description=f"Ping to internet IPs works, but {dns_loss}% of domain name lookups fail. Possible causes: the DNS resolver or its upstream servers.",
                    severity="critical",
                    affected=m.failed_targets,
                )
            if dns_loss >= config.dns_degraded_loss_pct:
                return Verdict(
                    kind="dns_loss",
                    status="degraded",
                    cause="dns_resolver_failure",
                    title="DNS Query Packet Drops",
                    description=f"{dns_loss}% of DNS queries are lost while ping to internet IPs works. Possible causes: the DNS resolver or its upstream servers.",
                    severity="warning",
                    affected=m.failed_targets,
                )
            if dns_avg_lat and dns_avg_lat > config.dns_timeout_threshold_ms:
                return Verdict(
                    kind="dns_slow",
                    status="degraded",
                    cause="dns_resolver_failure",
                    title="Slow DNS Queries",
                    description=f"DNS queries take {dns_avg_lat}ms on average. Possible causes: slow upstream resolvers or cold-cache authoritative lookups.",
                    severity="warning",
                    affected=m.failed_targets,
                )

    # 5. All Systems Nominal
    return NOMINAL


def apply_load(
    verdict: Verdict,
    m: RoundMetrics,
    reading: LoadReading,
    config: ClassifierConfig,
    is_loss_or_outage: bool,
) -> Verdict:
    """Re-reads a degraded/down diagnosis in the light of the current WAN load.

    At or above config.busy_load_pct, latency/jitter alone, or loss seen only at the ISP hops while the LAN,
    modem and public internet answer, is what a full line does (queues fill; intermediate ISP routers
    drop ping replies first), so it becomes "busy" instead of a fault. Real loss stays a fault and
    says that it happened under load.
    """
    if reading.pct < config.busy_load_pct:
        return verdict
    load_text = reading.describe()
    if not is_loss_or_outage:
        return Verdict(
            kind="busy_latency",
            status="busy",
            cause="load_induced_latency",
            title="Line Busy: Delay From Heavy Load",
            description=(
                f"{verdict.title} while the line is busy ({load_text}). Queues in the modem and router add delay "
                f"at this load (bufferbloat); expected, not counted as an incident."
            ),
            severity="info",
            affected=verdict.affected,
        )
    if m.isp_loss_only(config):
        return Verdict(
            kind="busy_isp_hops",
            status="busy",
            cause="load_induced_latency",
            title="Line Busy: ISP Hops Deprioritise Ping",
            description=(
                f"{verdict.title} while the line is busy ({load_text}). The LAN, gateway and public internet "
                f"answer normally; intermediate ISP routers drop ping replies first under load. Expected, "
                f"not counted as an incident."
            ),
            severity="info",
            affected=verdict.affected,
        )
    return verdict.with_(description=f"{verdict.description} During heavy load ({reading.pct:.0f}%): {load_text}.")


def apply_hop_deprioritisation(verdict: Verdict, m: RoundMetrics, config: ClassifierConfig) -> Verdict:
    """Loss at ISP hops that does not carry through to the end targets is not a fault (the classic MTR rule).

    Routers answer ping from a slow path and drop those replies first, load or not, while forwarding the
    traffic they carry normally. So when the public targets lose under the degraded level (15 % by default) and
    under half of what the ISP hops lose (local and gateway are already known healthy here, DNS is checked), the
    line is fine.
    """
    isp_loss, public_loss, dns_loss = m.isp.loss_pct, m.public.loss_pct, m.dns.loss_pct
    if public_loss >= config.degraded_loss_pct or public_loss >= isp_loss / 2 or dns_loss >= config.dns_degraded_loss_pct:
        return verdict
    worst = max(m.isp.targets, key=lambda st: st.recent_latencies[-10:].count(None), default=None)
    hop = worst.name if worst and worst.recent_latencies[-10:].count(None) else "An ISP hop"
    return Verdict(
        kind="hop_deprioritised",
        status="healthy",
        cause="nominal",
        title="ISP Hop Deprioritises Ping",
        description=(
            f"{isp_loss}% of recent probes to the ISP hops were lost, against {public_loss}% to the public targets. "
            f"{hop} drops some ping replies, but later hops and public targets are fine: routers often "
            f"deprioritise ping replies, so this is not packet loss."
        ),
        severity="info",
        affected=(),
    )
