#!/usr/bin/env bash
# The device lab: real device software in containers, read by this work tree's Pengumon through the drivers and the API
# the dashboard uses, for the drivers whose devices the maintainer does not own (docs/testing.md, "The device lab").
#   scripts/device-lab.sh up      start the lab devices (pulls and builds their images the first time)
#   scripts/device-lab.sh check   run Pengumon against them and check every device (tests/lab/check.py); exit 1 on a
#                                 difference. LAB_KEEP=1 leaves Pengumon running at http://127.0.0.1:18000 afterwards
#                                 (only lab devices: what a household with these devices sees), until `down`.
#   scripts/device-lab.sh down    remove the lab's containers, network and Pengumon
# The devices (all on the private lab network 10.89.77.0/24, published on 127.0.0.1 only):
#   OpenWrt 24.10.4 (the openwrt/rootfs image: procd, rpcd, uhttpd with ubus, LuCI), twice: "fresh" (root without a
#   password, as a new router) and "set up" (root with a password, and the read-only `pengumon` login of
#   docs/devices.md, its ACL taken from the docs themselves). Read as owrt_fresh, owrt_root, owrt_readonly and
#   owrt_wrong_password.
#   net-snmp (tests/lab/snmpd.Containerfile): SNMPv2c and SNMPv3 authPriv, read as snmp_v2c and snmp_v3.
#   The LAN's own router over UPnP (upnp_router), when one answers an SSDP search for an Internet Gateway Device:
#   read-only (byte counters and uptime). LAB_UPNP=0 leaves it out; LAB_UPNP_URL=http://host sets it.
# Lab-only passwords below: they guard nothing (the containers answer only on 127.0.0.1).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET=pengumon-lab
SUBNET=10.89.77.0/24
OPENWRT_IMAGE=docker.io/openwrt/rootfs:x86-64-24.10.4
SNMP_IMAGE=localhost/pengumon-lab-snmpd:1
APP=pengumon-lab-app
APP_PORT=18000
# name, address, published port, mode
OPENWRTS=("pengumon-lab-owrt-fresh 10.89.77.11 18101 fresh" "pengumon-lab-owrt 10.89.77.12 18102 set-up")
SNMP=(pengumon-lab-snmp 10.89.77.13 18161)

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }

# docs/devices.md's ACL for the read-only login, as the docs give it (so the lab checks the docs too)
docs_acl() {
    python3 - "$ROOT/docs/devices.md" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
part = text[text.index("**Set up a read-only user**"):]
print(re.search(r"```json\n(.*?)```", part, re.S).group(1), end="")
PY
}

up() {
    podman network exists "$NET" || podman network create --subnet "$SUBNET" "$NET" >/dev/null
    podman image exists "$OPENWRT_IMAGE" || { say "Pulling $OPENWRT_IMAGE"; podman pull -q "$OPENWRT_IMAGE" >/dev/null; }
    podman image exists "$SNMP_IMAGE" || { say "Building the SNMP agent"; podman build -q -t "$SNMP_IMAGE" \
        -f "$ROOT/tests/lab/snmpd.Containerfile" "$ROOT/tests/lab" >/dev/null; }
    local acl; acl="$(mktemp)"; docs_acl > "$acl"
    for spec in "${OPENWRTS[@]}"; do
        read -r name address port mode <<< "$spec"
        podman rm -f -t 1 "$name" >/dev/null 2>&1 || true
        say "OpenWrt ($mode) at 127.0.0.1:$port"
        # NET_ADMIN inside the container's own network namespace: netifd configures its WAN interface there
        podman run -d --name "$name" --network "$NET" --ip "$address" -p "127.0.0.1:$port:80" \
            --cap-add NET_ADMIN "$OPENWRT_IMAGE" /sbin/init >/dev/null
    done
    read -r sname saddress sport <<< "${SNMP[*]}"
    podman rm -f -t 1 "$sname" >/dev/null 2>&1 || true
    say "SNMP agent at 127.0.0.1:$sport/udp"
    podman run -d --name "$sname" --network "$NET" --ip "$saddress" -p "127.0.0.1:$sport:1161/udp" "$SNMP_IMAGE" >/dev/null
    for spec in "${OPENWRTS[@]}"; do
        read -r name address port mode <<< "$spec"
        for _ in $(seq 30); do podman exec "$name" sh -c 'ubus list session >/dev/null 2>&1' && break; sleep 1; done
        podman cp "$acl" "$name:/tmp/pengumon-acl.json"
        podman cp "$ROOT/tests/lab/openwrt-setup.sh" "$name:/tmp/openwrt-setup.sh"
        podman exec "$name" sh /tmp/openwrt-setup.sh "$address" "$mode"
    done
    rm -f "$acl"
    say "Lab is up"
}

upnp_url() {
    if [ "${LAB_UPNP:-1}" = "0" ]; then return; fi
    if [ -n "${LAB_UPNP_URL:-}" ]; then echo "$LAB_UPNP_URL"; return; fi
    # the first Internet Gateway Device that answers an SSDP search on the LAN: its host
    python3 - <<'PY'
import re, socket, time
msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
       "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.0)
s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
s.sendto(msg, ("239.255.255.250", 1900))
end = time.time() + 3
while time.time() < end:
    try:
        data, (host, _) = s.recvfrom(4096)
    except socket.timeout:
        continue
    if re.search(rb"(?im)^location:\s*http://", data):
        print(f"http://{host}")
        break
PY
}

config() {
    local upnp="$1"
    cat <<EOF
# The device lab's Pengumon (scripts/device-lab.sh): only lab devices, one target (loopback), nothing else probed.
targets:
  - {id: loopback, name: "This host", category: local, address: 127.0.0.1}
dns_probes: []
discovery: {background: false}
devices:
  - {id: owrt_fresh, driver: openwrt, url: "http://127.0.0.1:18101"}
  - {id: owrt_root, driver: openwrt, url: "http://127.0.0.1:18102"}
  - {id: owrt_readonly, driver: openwrt, url: "http://127.0.0.1:18102", username: pengumon}
  - {id: owrt_wrong_password, driver: openwrt, url: "http://127.0.0.1:18102"}
  - {id: snmp_v2c, driver: snmp, url: "snmp://127.0.0.1:18161"}
  - {id: snmp_v3, driver: snmp, url: "snmp://127.0.0.1:18161", username: labv3, options: {version: "3"}}
EOF
    if [ -n "$upnp" ]; then echo "  - {id: upnp_router, driver: upnp_igd, url: \"$upnp\"}"; fi
}

check() {
    podman container exists pengumon-lab-owrt || up
    . "$ROOT/scripts/lib-test-image.sh"
    build_test_image pengumon-test "$ROOT" Dockerfile.test
    local upnp work; upnp="$(upnp_url)"; work="$(mktemp -d)"
    if [ -n "$upnp" ]; then say "UPnP router: $upnp"; else say "No UPnP router (left out)"; fi
    config "$upnp" > "$work/config.yaml"; chmod a+r "$work/config.yaml"
    podman rm -f -t 1 "$APP" >/dev/null 2>&1 || true
    say "Pengumon (this work tree) at http://127.0.0.1:$APP_PORT"
    # The host's network: the lab's published ports and the LAN's router. Passwords as the environment gives them.
    podman run -d --name "$APP" --network host --user 10001:0 --cap-drop=ALL --cap-add=NET_RAW \
        --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,size=256m -v "$work/config.yaml:/lab/config.yaml:ro,Z" \
        -e HOME=/tmp -e PYTHONPATH=/app/src -e PENGUMON_CONFIG=/lab/config.yaml -e PENGUMON_DATA=/tmp/data \
        -e PENGUMON_HOST=127.0.0.1 -e PENGUMON_PORT=$APP_PORT \
        -e PENGUMON_DEVICE_OWRT_ROOT_PASSWORD=labroot -e PENGUMON_DEVICE_OWRT_READONLY_PASSWORD=labpass \
        -e PENGUMON_DEVICE_OWRT_WRONG_PASSWORD_PASSWORD=not-the-password \
        -e PENGUMON_DEVICE_SNMP_V2C_PASSWORD=labcommunity -e PENGUMON_DEVICE_SNMP_V3_PASSWORD=labauthpass \
        -e PENGUMON_DEVICE_SNMP_V3_PRIVACY_PASSWORD=labprivpass \
        "$built" python -c "import sys; from pengumon.cli import main; sys.exit(main(['run']))" >/dev/null
    for _ in $(seq 60); do curl -sf "http://127.0.0.1:$APP_PORT/api/health" >/dev/null && break; sleep 1; done
    say "Checking every device"
    local status=0
    python3 "$ROOT/tests/lab/check.py" "http://127.0.0.1:$APP_PORT" || status=$?
    if [ "${LAB_KEEP:-0}" != "1" ]; then podman rm -f -t 1 "$APP" >/dev/null; fi
    rm -rf "$work"
    return "$status"
}

down() {
    podman rm -f -i -t 1 "$APP" "${SNMP[0]}" $(for s in "${OPENWRTS[@]}"; do echo "${s%% *}"; done) >/dev/null 2>&1 || true
    podman network rm "$NET" >/dev/null 2>&1 || true
    say "Lab removed (images kept: $OPENWRT_IMAGE, $SNMP_IMAGE)"
}

case "${1:-}" in
    up) up ;;
    check) check ;;
    down) down ;;
    *) sed -n '2,8p' "$0"; exit 2 ;;
esac
