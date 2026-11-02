#!/usr/bin/env bash
# Traceroute for real on Linux: builds a small routed network out of network namespaces, then runs the ICMP
# backend's TTL probes and MtrEngine.run_trace from the client, expecting r1, r2 and the target, over IPv4 and IPv6
# (hop limit, ICMPv6 Time Exceeded) through the app's dual-stack backend.
#
#   client 192.0.2.2 --- 192.0.2.1 r1 198.51.100.1 --- 198.51.100.2 r2 203.0.113.1 --- 203.0.113.2 target
#          2001:db8:a::2   2001:db8:a::1  2001:db8:b::1   2001:db8:b::2  2001:db8:c::1   2001:db8:c::2
#
# Needs root (ip netns, veth, forwarding): `sudo scripts/netns-trace.sh` in CI (.github/workflows/ci.yml), or as
# root in a throwaway container with CAP_SYS_ADMIN and CAP_NET_ADMIN. Both socket kinds are checked: the raw socket
# as root, the ping socket (error queue) as uid 65534 with no capabilities. PYTHON names an interpreter that has the
# app's dependencies (default python3), readable by uid 65534; the code is a copy of src/ of this tree. Everything is
# removed at the end.
# Addresses are RFC 5737 and RFC 3849 documentation ranges, used only inside the namespaces.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
P="pgtr$$"  # namespace and link name prefix
NS=("$P-client" "$P-r1" "$P-r2" "$P-target")

cleanup() { for ns in "${NS[@]}"; do ip netns del "$ns" 2>/dev/null || true; done; }
trap cleanup EXIT
# nsenter, not `ip netns exec`: that remounts /sys, which a container (the local way to run this) may not do
in_ns() { local ns="$1"; shift; nsenter --net="/run/netns/$ns" "$@"; }

for ns in "${NS[@]}"; do ip netns add "$ns"; in_ns "$ns" ip link set lo up; done
link() {  # <ns a> <addr a> <addr6 a> <ns b> <addr b> <addr6 b>: a veth pair between two namespaces, /30 and /64
    local a="$1" aa="$2" a6="$3" b="$4" ba="$5" b6="$6" ia="$P$RANDOM" ib="$P$RANDOM"
    ip link add "$ia" type veth peer name "$ib"
    ip link set "$ia" netns "$a"; ip link set "$ib" netns "$b"
    in_ns "$a" ip addr add "$aa/30" dev "$ia"; in_ns "$a" ip -6 addr add "$a6/64" dev "$ia" nodad; in_ns "$a" ip link set "$ia" up
    in_ns "$b" ip addr add "$ba/30" dev "$ib"; in_ns "$b" ip -6 addr add "$b6/64" dev "$ib" nodad; in_ns "$b" ip link set "$ib" up
}
# net.* sysctls are per network namespace; written through /proc (no procps needed)
sysw() { in_ns "$1" sh -c "echo '$3' > /proc/sys/$(echo "$2" | tr . /)"; }
for ns in "${NS[@]}"; do sysw "$ns" net.ipv6.conf.all.disable_ipv6 0; sysw "$ns" net.ipv6.conf.default.disable_ipv6 0; done
link "$P-client" 192.0.2.2 2001:db8:a::2 "$P-r1" 192.0.2.1 2001:db8:a::1
link "$P-r1" 198.51.100.1 2001:db8:b::1 "$P-r2" 198.51.100.2 2001:db8:b::2
link "$P-r2" 203.0.113.1 2001:db8:c::1 "$P-target" 203.0.113.2 2001:db8:c::2
in_ns "$P-client" ip route add default via 192.0.2.1
in_ns "$P-r1" ip route add default via 198.51.100.2
in_ns "$P-r2" ip route add 192.0.2.0/30 via 198.51.100.1
in_ns "$P-target" ip route add default via 203.0.113.1
in_ns "$P-client" ip -6 route add default via 2001:db8:a::1
in_ns "$P-r1" ip -6 route add default via 2001:db8:b::2
in_ns "$P-r2" ip -6 route add 2001:db8:a::/64 via 2001:db8:b::1
in_ns "$P-target" ip -6 route add default via 2001:db8:c::1
for r in r1 r2; do
    sysw "$P-$r" net.ipv4.ip_forward 1
    sysw "$P-$r" net.ipv6.conf.all.forwarding 1
    sysw "$P-$r" net.ipv6.icmp.ratelimit 0  # the trace asks each router three times at once, then again per kind
done
# the client's ping sockets: any group (in a user namespace only mapped groups can be named, hence the fallback)
sysw "$P-client" net.ipv4.ping_group_range "0 2147483647" 2>/dev/null || sysw "$P-client" net.ipv4.ping_group_range "0 65534"

# The unprivileged run reads the code from a world-readable copy (a CI runner's home is not readable by others)
SRC="$(mktemp -d)"; cp -r "$ROOT/src/pengumon" "$SRC/"; chmod -R a+rX "$SRC"
cat > "/tmp/$P.py" <<'PY'
import asyncio, sys
from pengumon.engine.icmp_dual import DualStackBackend
from pengumon.engine.icmp_linux import LinuxIcmpBackend
from pengumon.engine.mtr import MtrEngine

kind = sys.argv[1]
paths = {
    "203.0.113.2": [("ttl_exceeded", "192.0.2.1"), ("ttl_exceeded", "198.51.100.2"), ("reply", "203.0.113.2")],
    "2001:db8:c::2": [("ttl_exceeded", "2001:db8:a::1"), ("ttl_exceeded", "2001:db8:b::2"), ("reply", "2001:db8:c::2")],
}

async def main():
    # the app's pairing (icmp_select.open_backend): one socket per family, IPv6 opened by its first probe
    backend = DualStackBackend(LinuxIcmpBackend(kind), lambda: LinuxIcmpBackend(kind, family=6))
    failures = 0
    try:
        for target, want in paths.items():
            label = f"{backend.name} {'IPv6' if ':' in target else 'IPv4'}"
            # the routers' neighbour caches start empty: an IPv6 router resolving its next hop can hold the first
            # packet about a second (seen with the raw socket), so one echo first
            await backend.probe(target, timeout=3.0)
            for ttl, (kind_want, responder_want) in enumerate(want, start=1):
                r = await backend.probe(target, timeout=2.0, ttl=ttl)
                ok = (r.kind, r.responder) == (kind_want, responder_want) and r.rtt_ms is not None and r.rtt_ms >= 0
                failures += not ok
                print(f"  {'ok  ' if ok else 'FAIL'}  {label} ttl={ttl}: {r}")
            hops = await MtrEngine(target, max_hops=8, timeout=2.0, backend=backend).run_trace()
            got = [h.ip for h in hops]
            ok = got == [w[1] for w in want]
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {label} trace: {[(h.hop_number, h.ip, h.rtt_ms) for h in hops]}")
        for b in (backend.v4, backend.v6):
            ok = b is not None and b.kernel_timestamps > 0 and b.fallback_timestamps == 0
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {b and b.name} IPv{b and b.family}: {b and b.kernel_timestamps} kernel timestamps, "
                  f"{b and b.fallback_timestamps} from Python")
    finally:
        backend.close()
    return failures

sys.exit(1 if asyncio.run(main()) else 0)
PY
chmod 644 "/tmp/$P.py"
trap 'cleanup; rm -rf "/tmp/$P.py" "$SRC"' EXIT

failed=0
echo "raw socket, as root:"
in_ns "$P-client" "$PYTHON" -I -c "import sys; sys.path.insert(0, '$SRC'); exec(open('/tmp/$P.py').read())" raw || failed=1
echo "ping socket, as uid 65534 without capabilities:"
in_ns "$P-client" setpriv --reuid=65534 --regid=65534 --clear-groups --inh-caps=-all --bounding-set=-all \
    "$PYTHON" -I -c "import sys; sys.path.insert(0, '$SRC'); exec(open('/tmp/$P.py').read())" dgram || failed=1
if [ $failed = 0 ]; then echo "netns trace: all checks passed"; else echo "netns trace: FAILED" >&2; fi
exit $failed
