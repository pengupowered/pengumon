"""Network discovery: the path from this host to the internet, hop by hop, with a best guess of who operates each
hop (docs/discovery.md). Discovery only proposes: it returns a draft of the probe path, which the path editor shows
and nothing applies until the owner does.

* addresses.py  address classes (private, CGNAT, public, ...) and the registrable-domain heuristic
* traces.py     the traces to the three public servers, merged into one path with its divergence point
* lookups.py    reverse DNS and the Team Cymru ASN lookup (DNS TXT), with their parsers
* devices.py    credential-free device checks: web page title and server header, SNMP sysDescr
* upnp.py       UPnP IGD: SSDP search, the device description and three read-only SOAP actions
* infer.py      the evidence per hop and the ownership (home / provider / internet) it adds up to
* draft.py      the contract-shaped draft hops, their ids and names, and the diff against the live path
* engine.py     one discovery run, start to finish
* jobs.py       the runs behind the API: single flight, results kept for a while
* api.py        POST /api/discovery, GET /api/discovery/{id}
* component.py  the component that makes the job store
"""
