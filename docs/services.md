# App health checks

Add your own apps on the **Settings** page (Devices and services, Host app checks): name, health address, and the
options below. A save applies at once ([below](#changing-the-checks-without-a-restart)). The YAML here is the syntax
that **Settings > Export** writes and **Import** reads; a `config.yaml` with a `service_checks` section is read only once,
on the first start (see [configuration.md](configuration.md)).

Pengumon also watches the other apps on this host over plain HTTP(S): every `interval_seconds` (30) it fetches
each service's health URL. The dashboard shows them in a **Host services** card (after the hop matrix), and only when
the `service_checks.services` setting lists one: with none, nothing about services appears (`/api/features` reports
`host_services` as unavailable). Each row has the status (`up`, `degraded` after 1 or 2 failed checks, `down` once
`failures_for_incident` (3) failed in a row, `checking` before the first answer), the latency, when it was last
checked and since when it has been up or failing, the reason when it is not up, the check address with a copy button,
a History toggle with its last five outages, and an **Open** link when `open_url` is set.

```yaml
service_checks:
  notify_after_seconds: 300         # ntfy hears of an outage only after this long of failing checks
  services:
    - id: my_app
      name: "My app"
      url: "http://127.0.0.1:9000/health"       # checked from the container; never shown as a link
      open_url: "http://myhost.lan:9000/"     # optional link for browsers on the LAN: http(s), no credentials, no ?query
      description: "What it does"               # optional
      notify: true                              # false: shown, but never sent to ntfy
      ca_file: "/app/certs/home-ca.pem"         # optional: verify an HTTPS endpoint against this CA
```

Failures are explained, not just named: `timeout`, `refused` ("connection refused (stopped or restarting)"), `tls`
("certificate not trusted" or "expired"), `dns`, `unreachable`, `http` ("HTTP 503") or `config` (the check could not
be set up, e.g. an unreadable `ca_file`). `failures_for_incident` failed checks in a row open a "<name> is not
responding" incident (kind `service_down`, cause `service_unavailable`) and the next good answer resolves it. These
incidents are kept apart from the network's: `GET /api/incidents` and the live feed list network incidents only
(`?scope=service` or `?scope=all` for the others), `pengumon_incidents_total` counts network incidents only, and the
network report and dossier leave them out. `GET /api/services` returns each service with its status, timings, error
and last five incidents (it never includes the CA file path); the Prometheus endpoint adds `pengumon_service_up` and
`pengumon_service_check_seconds` per service, and OpenTelemetry gets `pengumon.service.up` and
`pengumon.service.check.duration`.

Notifications (ntfy) are separate from incidents: a service that keeps failing for `notify_after_seconds` (300) sends
one "<name> not responding: <reason>" message at normal priority, and a "back" message when it recovers, but only if
the down message went out, so a redeploy-length restart stays quiet. `notify: false` silences a service entirely.
An HTTPS endpoint is verified against `ca_file` when one is given (the compose file mounts the home CA at
`/app/certs/home-ca.pem`). The containers have no podman healthchecks of their own: rootless podman runs those as
systemd timers, which broke whenever the user manager's socket did, and nothing read their status.

## Changing the checks without a restart

The list, `interval_seconds`, `failures_for_incident` and `notify_after_seconds` can be edited on the Settings page
and apply at once: the interval is read again every round (a shortened one is noticed within two seconds), a service
added is checked at the next round, and a service removed, disabled, or given another URL, CA file or timeout is
forgotten (its open incident is resolved and a message about it that was waiting for quiet hours is dropped) and
starts afresh. Renaming it or changing its notify switch keeps its history.
