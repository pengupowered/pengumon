"""`infer` pinned on 600 seeded random networks (hops of every address class, silent hops, names, ASNs, pages, SNMP,
UPnP gateways, off-path devices): the digest of every result must not change when `infer` is restructured. A
legitimate change of inference rules changes the digest; update it then, after reading the diff of the cases that
moved (`CASES` lists them in order)."""

import hashlib
import random

from pengumon.engine.discovery.devices import CABLE_MODEM_ADDRESS, Fingerprint, PageInfo
from pengumon.engine.discovery.infer import Facts, OffPathDevice, infer
from pengumon.engine.discovery.lookups import AsnInfo
from pengumon.engine.discovery.traces import MergedPath, PathHop
from pengumon.engine.discovery.upnp import IgdInfo

EXPECTED_DIGEST = "4ce86da47cfafb876a56bdc293863c32e8c7b14b783a027e4c1fe34fc472710c"
CASES = 600

_ADDRESSES = ["192.168.88.1", "192.168.88.2", "192.168.1.254", "10.0.0.1", "169.254.1.1", "100.64.0.1", "100.64.3.2",
              "203.0.113.1", "203.0.113.2", "203.0.113.9", "198.51.100.7", "198.51.100.8", "198.18.0.1"]
_NAMES = ["po-1.cr1.city.example.net", "be-2.core1.example.net", "edge1.example.org", "gw.example.co.uk",
          "x.other.example.com"]
_ASNS = [AsnInfo(64500, "EXAMPLE-NET - Example Networks, LLC, ZZ"), AsnInfo(64511, "EXAMPLE-ORG - Example Org, ZZ"),
         AsnInfo(64522)]
_PRINTS = [None, Fingerprint("xfinity_xb", "an Xfinity XB gateway's login page"),
           Fingerprint("asuswrt", "an Asus router's login page")]
_EXTERNAL = [None, "203.0.113.50", "100.64.9.9", "192.168.1.5", "169.254.2.2"]


def _facts(rnd):
    n = rnd.randint(0, 9)
    pool = list(_ADDRESSES)
    rnd.shuffle(pool)
    hops = []
    for ttl in range(1, n + 1):
        silent = rnd.random() < 0.2
        address = None if silent or not pool else pool.pop()
        rtt = None if address is None or rnd.random() < 0.1 else round(rnd.uniform(0.3, 30), 1)
        hops.append(PathHop(ttl, address, rtt, ("1.1.1.1",)))
    divergence = rnd.choice([None, -1] + list(range(n)))
    path = MergedPath(hops, divergence, "1.1.1.1" if n else None)
    addresses = [h.address for h in hops if h.address]
    facts = Facts(path)
    for a in addresses:
        if rnd.random() < 0.6:
            facts.names[a] = rnd.choice(_NAMES)
        if rnd.random() < 0.6:
            facts.asns[a] = rnd.choice(_ASNS)
        if rnd.random() < 0.25:
            facts.pages[a] = PageInfo(a, "http", 200, rnd.choice([None, "Login"]), rnd.choice([None, "lighttpd"]),
                                      rnd.choice(_PRINTS), rnd.random() < 0.1)
        if rnd.random() < 0.15:
            facts.snmp[a] = "Example SNMP agent"
        if rnd.random() < 0.3:
            facts.igds.append(IgdInfo(a, "Example-Mesh", external_ip=rnd.choice(_EXTERNAL)))
    if addresses and rnd.random() < 0.5:
        facts.gateway = rnd.choice(addresses)
    off_path = []
    for address in rnd.sample([CABLE_MODEM_ADDRESS, "192.168.100.2", rnd.choice(_ADDRESSES)], rnd.randint(0, 3)):
        page = PageInfo(address, "http", 200, "Modem", None, rnd.choice(_PRINTS)) if rnd.random() < 0.6 else None
        off_path.append(OffPathDevice(address, page, rnd.random() < 0.5))
    return facts, off_path


def test_infer_matches_its_recorded_digest():
    rnd = random.Random(20261111)
    digest = hashlib.sha256()
    for _ in range(CASES):
        facts, off_path = _facts(rnd)
        digest.update(repr(infer(facts, off_path)).encode())
    assert digest.hexdigest() == EXPECTED_DIGEST
