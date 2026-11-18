#!/bin/sh
# Runs inside an OpenWrt container of the device lab (scripts/device-lab.sh) after it booted: $1 is the container's
# address on the lab network, $2 "fresh" (root keeps no password, as a new router) or "set-up" (root gets a password
# and a read-only `pengumon` login with docs/devices.md's ACL, copied in as /tmp/pengumon-acl.json).
set -eu
address="$1"; mode="$2"
# The container's one network port is the WAN (static, the address podman gave it), so the driver has WAN counters.
uci -q del network.@device[0].ports || true
uci set network.wan=interface
uci set network.wan.device=eth0
uci set network.wan.proto=static
uci set network.wan.ipaddr="$address"
uci set network.wan.netmask=255.255.255.0
uci commit network
/etc/init.d/network reload
if [ "$mode" = "set-up" ]; then
    roothash="$(uhttpd -m labroot)"  # root's system password, which rpcd's `$p$root` login checks
    sed -i "s|^root:[^:]*:|root:${roothash}:|" /etc/shadow
    cp /tmp/pengumon-acl.json /usr/share/rpcd/acl.d/pengumon.json
    hash="$(uhttpd -m labpass)"
    printf '\nconfig login\n\toption username pengumon\n\toption password %s\n\tlist read pengumon\n' "$hash" \
        >> /etc/config/rpcd
    /etc/init.d/rpcd restart
fi
for i in 1 2 3 4 5 6 7 8 9 10; do
    ubus call network.interface.wan status 2>/dev/null | grep -q '"up": true' && exit 0
    sleep 1
done
echo "the WAN interface did not come up" >&2
exit 1
