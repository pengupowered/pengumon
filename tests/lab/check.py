"""The device lab's check (scripts/device-lab.sh check): asks a running Pengumon (argv[1], its base URL) how each lab
device reads, through the same API the dashboard uses, and compares that with EXPECTED. Prints one line per device
and exits 1 when any differs. Standard library only."""

import json
import sys
import time
import urllib.error
import urllib.request

# device id -> what must hold: the status, the error kind, and which figures are there (True) or missing (False).
EXPECTED = {
    # a fresh OpenWrt: root has no password and none is saved; root's ACL has no /proc/stat (no CPU %)
    "owrt_fresh": {"status": "healthy", "wan": True, "mem": True, "cpu": False},
    # root with its password, on the router that has docs/devices.md's ACL file: root's `read '*'` takes it in, so
    # CPU % too (as the docs say)
    "owrt_root": {"status": "healthy", "wan": True, "mem": True, "cpu": True},
    # the read-only login docs/devices.md sets up: everything, CPU % from the second poll on
    "owrt_readonly": {"status": "healthy", "wan": True, "mem": True, "cpu": True},
    # a wrong password: sign-in refused, said as such
    "owrt_wrong_password": {"status": "error", "error_kind": "auth"},
    "snmp_v2c": {"status": "healthy", "wan": True, "mem": True, "cpu": True},
    "snmp_v3": {"status": "healthy", "wan": True, "mem": True, "cpu": True},
    # the LAN's own router over UPnP (left out when the lab found none): WAN counters only
    "upnp_router": {"status": "healthy", "wan": True},
}
FIGURES = {"wan": "wan_rx_bytes", "mem": "mem_usage_pct", "cpu": "cpu_usage_pct"}


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as answer:
        return json.load(answer)


def main(base: str, wait_s: float = 90.0) -> int:
    deadline = time.monotonic() + wait_s
    while True:
        devices = {d["id"]: d for d in get(base, "/api/devices")}
        polled = all(d.get("last_poll") for d in devices.values())
        # CPU % needs two polls: wait for the readings that need them too (or the deadline)
        details = {i: get(base, f"/api/devices/{i}") for i in devices if devices[i].get("last_poll")}
        cpu_due = [i for i in devices if EXPECTED.get(i, {}).get("cpu") and
                   (details.get(i) or {}).get("cpu_usage_pct") is None]
        if (polled and not cpu_due) or time.monotonic() > deadline:
            break
        time.sleep(3)
    failures = 0
    for device_id, want in EXPECTED.items():
        device = devices.get(device_id)
        if device is None:
            print(f"  -   {device_id:20} not in this lab")
            continue
        detail = details.get(device_id) or {}
        got = {"status": device.get("status"), "error_kind": device.get("error_kind")}
        for name, field in FIGURES.items():
            got[name] = detail.get(field) is not None
        wrong = [f"{k}: {got.get(k)!r}, wanted {v!r}" for k, v in want.items() if got.get(k) != v]
        shown = ", ".join(f"{n} {'yes' if got[n] else 'no'}" for n in FIGURES)
        why = f" ({device.get('error_message')})" if device.get("error_message") else ""
        print(f"  {'ok ' if not wrong else 'BAD'} {device_id:20} {device['kind']:16} {got['status'] or 'not read':6} "
              f"{shown}{why}")
        for line in wrong:
            print(f"        {line}")
        failures += bool(wrong)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1].rstrip("/")))
    except urllib.error.URLError as e:
        print(f"Pengumon at {sys.argv[1]} did not answer: {e}", file=sys.stderr)
        sys.exit(2)
