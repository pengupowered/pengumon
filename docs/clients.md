# Clients and mesh (capabilities `client_inventory` and `mesh`)

A driver whose device can list its connected devices offers `client_inventory`: each client has a connection type
(wired or Wi-Fi), a band, a signal strength, the negotiated Wi-Fi link rate, how long it has been connected and the mesh
node it is on, and whatever the device knows about its name, vendor and addresses. A value the device lacks is empty. A
driver that knows its mesh also offers `mesh` (the nodes and their client counts). The dashboard lists the clients in
the **Local network** modal (the "Local network" button or "Active Clients" on the router card) only when the `clients`
feature is available; otherwise "Active Clients" stays a plain number and its tooltip says what is missing. Many
installations have no such device, and nothing else changes for them.

The AsusWRT driver reads both from the `get_clientlist()` answer it already asks for every
`clientlist_interval_seconds`, so they cost no extra request. The mesh node list is asked of the router with
`get_cfg_clientlist()` at most once a minute (`mesh_nodes_interval_seconds`; `mesh_node_names: false` never asks). It
gives each node's name (alias, else model name), whether it is online and how it reaches its parent: `wired` when a
node's own MAC is in another node's `wired_mac` list (a main router lists the satellites it reaches by cable there)
and it names no parent radio; `wifi` with the band and signal when one of its `pap2g`/`pap5g`/`pap5g1`/`pap6g` fields
holds the parent's MAC (the `rssi*` field beside it is the signal). The Wi-Fi case follows the field names only: the
firmware checked left every `pap*` empty. A node that carries no client is listed too, so an offline node shows; one
the router says is offline makes the router card warn. A node is known by its own MAC and its radios' MACs; its
`wired_mac` list is not a name source.
Each client also has a `kind` (phone, computer, game, network, printer) from the firmware's numeric `type` (or
`isPrinter` / `isGateway`) when it is one of the few types mapped in `_CLIENT_KINDS`; any other type is left unknown. Without names, nodes are "Node 1", "Node 2", ... in the order of their ids. `mesh_node_labels`
overrides any of them. The link rate the router reports is the Wi-Fi rate negotiated with the device, **not traffic**:
the client list carries no per-client traffic. Since 0.27 the router's own Traffic Analyzer gives the bytes per client
and hour instead ([devices.md](devices.md#asuswrt-traffic-uptime-wan-link-and-temperature)): each row of the list then
has a `traffic_hint`, the app the router counted at least 80 % of the client's last 24 hours as (a recognized app
such as "Sony PlayStation", never a protocol label like "SSL/TLS"; only when the client moved at least 50 MB in a window
that ended less than 48 hours ago; null otherwise). It is a hint shown beside the
name, never a rename.

* **Privacy** (`privacy.client_details`, enforced by the API, not the page): `token` (default) sends full names, IP and
  MAC addresses only to a request signed in with the password (Settings > Sign-in), so only someone with it can read them
  (a viewer without it, [view only](security.md#view-only-without-the-password), gets the masked list); `full` always
  sends them; `masked` never does: a client is then its vendor (or "Device") and the last four hex digits of its MAC,
  with no IP or MAC. The answer's `privacy` says which applied and how to see more. Counts are never hidden.
* **Where it lives.** The client list is kept in memory (the last reading) and is not in `/metrics`, the reports, the
  dossier, the websocket or the logs. Only aggregate counts are stored (schema v10): per client-list reading, the
  online, wired and per-band counts as `device_samples` columns (`clients_online`, `clients_wired`,
  `clients_wifi_2g`, `clients_wifi_5g`, `clients_wifi_other`; NULL on polls that did not read the list), and the count
  per mesh node as rows of `mesh_samples(device_id, ts, node_id, clients)`. The per-node counts are a table, not JSON
  in a column, because they are structured (the number of nodes varies) and a long range is then bucketed by a plain
  `GROUP BY` in SQLite. Both are pruned by retention like the other device samples.
  The one exception is the router's traffic per client (schema v19, `traffic_hourly` and `traffic_apps`), stored by
  MAC so that its history exists: `GET /api/devices/{id}/traffic` names the clients by the rule above, and the dossier,
  the AI pack and the MCP tools describe a client only by its kind and maker ("a game console"), never by name or
  address. Kept for `database.traffic_retention_days` (400 by default). And the names given to devices in Pengumon
  (schema v20, `client_names`, by MAC; set with the pencil in a device's traffic dialog, or `PUT
  /api/devices/{id}/clients/{key}/name`): shown instead of the router's name wherever names are shown in full, never
  when they are masked, and never in the dossier, the AI pack or MCP. Only a request that may see names may set one.
