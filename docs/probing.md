# Probing: ping and traceroute on each OS

Pengumon sends its own ICMP echo requests; it never runs `ping`, `traceroute` or `tracert`, so it does not depend on
what the OS ships or on the language of its output. One backend per OS implements the same interface
(`src/pengumon/engine/icmp_base.py`): an echo request to an IPv4 or IPv6 address with a given TTL (the hop limit, for
IPv6), answered by an echo reply from the target, a Time Exceeded from a router on the way, a Destination Unreachable,
or nothing within the timeout. None of them needs administrator rights.

- Linux
  - Backend: `linux-raw` or `linux-dgram`
  - How: A raw ICMP socket when the process has `CAP_NET_RAW` (the container image grants it to the interpreter), else an unprivileged ICMP datagram socket ("ping socket"). One of each family: `AF_INET6`/`IPPROTO_ICMPV6` for IPv6, of the same kind.
  - RTT resolution: about 1 µs: the kernel stamps each answer (`SO_TIMESTAMPNS`), so event-loop delays don't count
- macOS
  - Backend: `macos-dgram`
  - How: An ICMP datagram socket, which macOS allows for any user; for IPv6 an ICMPv6 one (what `ping6` opens when not root).
  - RTT resolution: about 1 µs (`SO_TIMESTAMP`)
- Windows
  - Backend: `windows-icmpapi`
  - How: `IcmpSendEcho2` from `iphlpapi.dll` (what `ping.exe` uses), and `Icmp6SendEcho2` for IPv6, in a small thread pool.
  - RTT resolution: 1 ms: Windows reports whole milliseconds; the dashboard's latency panel says "1 ms resolution"

`engine/icmp_select.py` picks the backend from `sys.platform`; the app opens one at startup and shares it between the
prober, traceroute and the incident path trace. The log line `ICMP probes use the linux-dgram backend (1 µs
resolution)` says which one is in use, and `/api/info` reports it as `probe: {backend, resolution_ms, error, fix}`.

## IPv4 and IPv6

An IPv4 address is probed over ICMPv4, an IPv6 address over ICMPv6 (echo request 128, reply 129; Time Exceeded 3,
Destination Unreachable 1). A link-local IPv6 address carries its interface (`fe80::1%eth0`): the zone becomes the
scope id of the send, and a link-local router that answers comes back with it. A host name is resolved before each probe
to its first address of the target's `family` (`4` by default, `6` for IPv6; see
[configuration.md](configuration.md)). On Linux and macOS the IPv6 socket is opened by the first IPv6 probe, so a host
with IPv4 targets only never opens one.

Packet Too Big (ICMPv6 type 2) counts as unreachable: a probe is 104 bytes and every IPv6 link carries 1280, so a router
that says it cannot pass one is broken for this target.

**IPv6 unavailable on this host.** An IPv6 target needs a route. Before the first IPv6 probe, and every five minutes
after, Pengumon asks the operating system for a route to a public IPv6 address with a UDP `connect()`, which sends
nothing. Without one (and whenever sending fails with "network unreachable", "cannot assign requested address" or "address
family not supported", or their Windows equivalents) IPv6 targets are not probed: they record no samples, no loss and
no incident, and the dashboard shows them as **IPv6 unavailable on this host**, neutral, the way it shows a hop that ignores
ping. `::1` and link-local targets skip the route check; only a failed send marks them. Once IPv6 works, an IPv6
target that stops answering is an ordinary loss or outage. The live `targets` topic and `GET /api/targets` carry the
state as `ipv6_unavailable`.

A container on rootless Podman's default network (pasta) may not pass ICMPv6 echo even when the host has IPv6 (seen on
a Linux host with Podman 4.9: TCP over IPv6 worked, ICMPv6 echo did not). There the route check passes and IPv6 targets
time out; run the container with `--network host` (as the compose files do) to probe IPv6.

## Linux: the one setup step on Debian, Ubuntu, Mint and Raspberry Pi OS

The ping socket is open to the groups in `net.ipv4.ping_group_range`. The kernel default allows nobody. Fedora and
Arch ship systemd's `50-default.conf`, which allows every group; Debian, Ubuntu, Linux Mint and Raspberry Pi OS do not.
There, once, as root:

```bash
echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/60-unprivileged-ping.conf && sudo sysctl --system
```

The container needs the same on the host when it runs under rootless Podman. A service with `CAP_NET_RAW` (the image,
or a systemd unit with `AmbientCapabilities=CAP_NET_RAW`) uses the raw socket and needs neither.

## `pengumon doctor`

Checks that this host can probe, and says how to fix it when it can't:

```
ICMP backend    linux-dgram (1 µs resolution)
ping 127.0.0.1  echo reply from 127.0.0.1 in 0.041 ms
TTL=1 probe     time exceeded from 192.168.1.1 in 0.512 ms
gateway         192.168.1.1 (from /proc/net/route)
IPv6            available (a route to 2606:4700:4700::1111)
IPv6 backend    linux-dgram
ping ::1        echo reply from ::1 in 0.022 ms
IPv6 router     fe80::1%eth0
config file     /home/me/.local/state/pengumon/config.yaml  (missing: defaults are used)
data directory  /home/me/.local/state/pengumon
listen address  127.0.0.1:8000
probing works
```

Without permission it prints the reason and a `fix` line with the sysctl command above. It exits 0 when an echo to
127.0.0.1 is answered and 1 otherwise. The TTL=1 line is informational: some networks, and many cloud CI runners, drop
TTL-limited probes or the Time Exceeded answers, so a timeout there does not fail the check. So are the IPv6 lines:
whether the host has a route to the IPv6 internet, the IPv6 socket, an echo to `::1`, and the IPv6 default router
(Linux, from `/proc/net/ipv6_route`; a link-local router with its interface). A host without IPv6 still passes.

## Traceroute and the incident path trace

`MtrEngine.run_trace` (the dashboard's **Trace** dialog, `POST /api/mtr/trace`, and the path trace taken when an
incident starts) sends three echo requests for each TTL (hop limit, over IPv6) from 1 to 15, all at once a few milliseconds apart, so a trace
takes about one timeout (1.5 s) however long the path is. Each hop shows the router that answered most often and its
average round trip. The trace ends at the hop where the target answers, or at an Unreachable. When neither comes back,
it shows the hops up to the last router that answered, then one timed-out hop.

An IPv6 address is traced over ICMPv6; a host name over its IPv4 address, or its IPv6 one when the dialog's "A host
name over" says IPv6 (`"family": 6` in the request).

How the answers are read:
- Linux, ping socket: Time Exceeded and Unreachable arrive on the socket's error queue (`IP_RECVERR`,
  `recvmsg(MSG_ERRQUEUE)`), with the router's address in `SO_EE_OFFENDER` and the quoted request's id and sequence as
  data ([ip(7)](https://man7.org/linux/man-pages/man7/ip.7.html)). IPv6: `IPV6_RECVERR` at `SOL_IPV6`, origin
  `SO_EE_ORIGIN_ICMP6`, the offender a `sockaddr_in6` with its scope id
  ([ipv6(7)](https://man7.org/linux/man-pages/man7/ipv6.7.html)).
- Linux raw socket and macOS: Time Exceeded arrives as an ordinary ICMP packet that quotes the original IP header and
  the first 8 bytes of the request (RFC 792), where its id and sequence are. IPv6 sockets never deliver the IP header;
  the error quotes the request's 40-byte IPv6 header and its echo header (RFC 4443). A raw ICMPv6 socket lets only
  replies and errors through (`ICMP6_FILTER`); on macOS the ICMPv6 datagram socket receives every ICMPv6 message
  (XNU `icmp6_rip6_input`), like the IPv4 one.
- Windows: `IcmpSendEcho2` with `IP_OPTION_INFORMATION.Ttl`; the reply's status is `IP_TTL_EXPIRED_TRANSIT` and its
  address is the router. IPv6: `Icmp6SendEcho2`, the reply an `ICMPV6_ECHO_REPLY` (a packed `IPV6_ADDRESS_EX`, then
  Status and RoundTripTime); `IP_HOP_LIMIT_EXCEEDED` (the same 11013) or `IP_TIME_EXCEEDED`.

Every answer is matched to its request by echo id, sequence and destination (compared as addresses, so
`2001:DB8::1` is `2001:db8::1`), so other programs' pings on the same host are ignored.

## TCP and HTTP probes

A hop that does not answer ping (a server or a router that drops ICMP) can be probed another way: set its **Probe by**
in the path editor to **TCP connect** or **HTTP(S) request**. Ping stays the default and nothing changes for a hop
that does not ask for it.

- **TCP connect** opens a connection to the hop's port and closes it at once, with no data sent (a FIN, not a reset: a
  reset every second looks like a port scan to a firewall). The reading is the time of the handshake, one round trip
  like a ping, so a TCP hop can have any role and is judged exactly like a ping hop. Only a completed handshake counts:
  a refused port is a lost probe (it comes as easily from a firewall in front of the host as from the host).
- **HTTP(S) request** sends one `HEAD` or `GET` to a fixed path and reads only the status line (at most 1024 bytes),
  then drops the connection: no headers, no body. The request is exactly `Host`, `User-Agent: Pengumon/<version> (path
  probe)`, `Accept` and `Connection: close`: no cookies, no credentials, no query string, no proxy, no redirect followed
  (a 3xx is an answer), no connection reused, and TLS is always verified against the system certificate store (a
  self-signed device needs TCP or plain `http`). A status below 500 is an answer; 500 and above is a lost probe. The
  reading is the time to the server's answer, which includes the connection, TLS and the server's own work, so it is
  not a round trip: **an HTTP hop always has the role Other**, charted and never blamed for an outage.

How exact the reading is:

| Reading | Accuracy |
|---|---|
| TCP on Linux | The kernel's own handshake time (`TCP_INFO`, microseconds): unaffected by how late the program wakes up. Under rootless Podman (pasta) or slirp4netns the relay's time is included, as it is for ping there. |
| TCP on macOS and Windows | Timed by Pengumon: never below the true handshake, higher by however long the program was busy when the answer arrived (usually under 1 ms, tens of ms while a report or a TLS handshake runs). |
| HTTP, everywhere | Timed by Pengumon, from just before the connection starts to the status line. The name lookup is not included. |

The path health pages say the same under each hop (its "probe note"). An answer that arrives in time while the program
is blocked past the deadline is counted as lost; ping has the same limit.

Limits (docs/security.md has the reasons):

- At most 8 hops per path use TCP or HTTP: each opens a connection every second.
- Loopback, link-local and special-purpose addresses (`127.0.0.1`, `169.254.169.254`, `::ffff:127.0.0.1`, multicast,
  `localhost`) are refused when the path is edited and again on the address a name resolves to at every probe. This
  host's own apps are watched by Service checks (Settings), not by the path.
- Ports on the Fetch standard's list of bad ports (mail, login, file sharing, IRC; also 22 and 53) are refused. Probe a
  resolver on 443 or 853, not 53.
- A probe has the same deadline as a ping (`prober.timeout_seconds`, at most 1 s), name lookup included.

What a lost TCP or HTTP probe says (the text stored with the sample and shown by Test):

| Situation | Text |
|---|---|
| Closed port | `Connection refused on port N` (on Windows the system answers a closed port only after about two seconds, so with a probe timeout shorter than that it can read as "no answer in time" instead) |
| No SYN-ACK | `No answer in time (TCP handshake)` |
| TLS too slow | `No answer in time (TLS handshake)` |
| No status line | `No answer in time (waiting for the HTTP answer)` |
| Expired certificate | `TLS: certificate expired` |
| Certificate for another name | `TLS: certificate is for another name` |
| Any other certificate problem | `TLS: certificate not trusted` |
| Other TLS failure | `TLS handshake failed: <reason>` |
| Not HTTP | `Not an HTTP answer` (with `(no status line in the first 1024 bytes)` when it is too long) |
| Closed without answering | `The server closed the connection without answering` |
| 5xx | `HTTP 503 from the server` |
| Refused by the policy | `Not probed: <address> is a loopback address (TCP and HTTP probes never connect there)` |
| No route | `Host unreachable`, `Network unreachable` |

Without any ICMP hop on the path the dashboard does not say "pings are not allowed": TCP and HTTP hops are still
probed and judged. With a mix, and pings not allowed, the verdict stays "Not probing" (the ping hops have no evidence),
while the TCP and HTTP hops are still probed and recorded.

## The default gateway

When no probe path is stored yet, Pengumon probes the default gateway and two public resolvers. On Linux the gateway
comes from `/proc/net/route`. Where that file is missing (macOS, Windows), it is the router that answers a TTL=1 probe
to 1.1.1.1, if that address is private (RFC 1918). The probe gives up after about a second.

The IPv6 default router comes from `/proc/net/ipv6_route` on Linux (`netinfo.get_ipv6_default_router`: the `::/0`
route with the gateway flag and the lowest metric; a link-local router gets its interface, `fe80::1%eth0`). `pengumon
doctor` shows it; the default targets stay IPv4 for now.

## How it is tested

- Unit tests on Linux, without the real OS: each backend gets a fake socket or a fake DLL function, and the parsing is
  tested with byte-exact packets, ICMPv4 and ICMPv6 (`tests/test_icmp_linux.py`, `test_icmp_macos.py`,
  `test_icmp_windows.py`); the dual-stack pairing, the unavailable state and IPv6 traces in `tests/test_ipv6.py`.
- The same suite runs natively on Linux (x64 and arm64), macOS and Windows in CI (the `native` job), where tests marked
  for the OS probe 127.0.0.1 and `::1` and an unroutable documentation address of each family for real, and `pengumon doctor` runs from the wheel.
- `scripts/netns-trace.sh` builds a routed network out of Linux network namespaces (client, two routers, target), over
  IPv4 and IPv6 with forwarding on, and checks the TTL probes and a whole trace in both families on both socket kinds
  (real ICMPv6 Time Exceeded from the routers): the raw socket as root, the ping socket as an
  unprivileged user. It needs root (`sudo` in CI); locally it also runs as root inside a throwaway container, e.g.
  `podman run --rm --privileged -v "$PWD:/w:ro" <the scripts/test.sh image> sh -c 'apt-get update && apt-get install -y iproute2 && /w/scripts/netns-trace.sh'`.
- The container test (`scripts/test-container.sh`) checks that the image probes 127.0.0.1 on the raw socket, that
  `pengumon doctor` exits 0 inside it, and that no ping or traceroute program is installed.
