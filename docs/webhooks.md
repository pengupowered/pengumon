# Alerts by webhook

Webhooks are a second alert channel next to [ntfy](telemetry.md#-notifications). They receive exactly the alerts ntfy
does, after the same rules, so you can use either, both, or several webhooks:

- an incident starts (not for expected `busy` conditions), at most one message per cause every 5 minutes (see
  [When alerts go out](#when-alerts-go-out));
- an announced incident resolves;
- one of your [own apps](services.md) has failed for `notify_after_seconds`, and when it answers again;
- your network path changed ([discovery](discovery.md)).

Delivery runs in a background task with a timeout (5 s, `timeout_seconds`). A failed delivery is retried once after
2 seconds when the cause looks temporary (timeout, connection error, HTTP 429 or 5xx) and then logged as
`Webhook <name> not delivered: <reason>`; it never stops the monitor. Logs never contain the URL (a webhook id or token
is often part of it) or a header value.

## Configuration

Add webhooks on the **Settings** page (Alerts, Channels): name, URL, format, headers and timeout. The settings are shown below as YAML,
the syntax that **Settings > Export** writes and **Import** reads. A `config.yaml` is read only for a one-time import on
the first start of 0.21; after that, edits to it are ignored (see [configuration.md](configuration.md)).

```yaml
notifications:
  public_url: "http://pengu.lan:8000"   # optional: where people open the dashboard (http or https, no query)
  webhooks:
    - name: "home-assistant"            # optional: shown in the Alerts dialog and the log
      url: "http://homeassistant.lan:8123/api/webhook/pengumon-alerts"
      format: json                      # json (default), slack or discord
      headers:                          # optional
        Authorization: "secret:ha_token"
      timeout_seconds: 5                # optional
```

- `url` must be `http` or `https` and carry no `user:password@`; put credentials in `headers`. Redirects are followed
  only within the same host (at most 3); one to another host fails the delivery.
- A header value is sent as written, or, when it is `secret:<name>`, replaced by the secret `<name>`, looked up in
  this order: the environment variable `PENGUMON_WEBHOOK_SECRET_<NAME>` (upper case, other characters as `_`), the file
  `/run/secrets/pengumon_webhook_<name>` (Podman or Docker secret), then `webhooks.<name>` in `data/secrets.json`
  (`{"webhooks": {"ha_token": "Bearer ..."}}`, keep it mode 0600). `docker-compose.yml` passes none of the secret
  variables on, so use the secrets file or a secret mount. A webhook whose secret cannot be found is not used, and
  the log names the webhook and the secret (never a value).
- Names must be unique. `public_url` is the "link" of a `json` message and a last line of Slack and Discord messages;
  without it there is no link.
- The URL and the secret header values are entered on the Settings page. They are kept in
  `data/secrets.json` (`webhook_urls.<id>` and `webhooks.<name>`), never shown again, and a webhook added, changed
  or removed there is in use at once, without a restart. A webhook with no URL set is not used.
- The ntfy topic address and token are the secrets `ntfy.url` and `ntfy.token` (`PENGUMON_NOTIFY_URL` and
  `PENGUMON_NOTIFY_TOKEN` still win when set and not empty).

The **Alerts** dialog lists every configured channel (name, format and host; no path, query or header) and **Send
test** sends one message through all of them and shows what each answered. The same is `GET /api/notifications` and
`POST /api/notifications/test`.

## When alerts go out

The `alerts:` rules decide when a message leaves, for ntfy and every webhook alike (Settings, Alerts):

```yaml
alerts:
  min_severity: warning        # warning (default) or critical: send only critical network incidents
  delay_seconds: 0             # wait this long (0-3600) before announcing a network incident
  repeat_window_seconds: 300   # the same cause is announced at most once in this time (0: every one)
  quiet_hours:
    enabled: false
    start: "22:00"             # in general.timezone; may be later than end, which crosses midnight
    end: "07:00"
    critical_still: true       # a critical network incident goes out at once even in quiet hours
general:
  timezone: Europe/Berlin      # defaults to the server's time zone
```

- **Only critical** (`min_severity: critical`) applies to **network incidents** only. A warning incident is not
  announced when it starts; if it later turns critical it is considered then, as if it had started at that moment.
  Messages about your own apps and route changes are not filtered by severity (an app has its own `notify` switch).
- **Delay** counts from the start of the incident, which is dated to its first bad probe round, so "5 minutes" means
  an outage that lasted 5 minutes. An incident that ends first sends nothing, no start message and no "resolved".
- **Quiet hours** hold a message until they end. What is still going on at that moment goes out once, with "Ongoing since 03:12:"
  after "Pengumon:" in the title (the time the incident or outage began, in your time zone). Something that ended in
  the meantime is dropped: an incident that resolved, an app that came back (both its messages). A route-change message is
  sent at the end (a newer one replaces one still waiting). A "resolved" message for an incident you were told about waits too, unless the incident was critical and
  `critical_still` let it through. The **Send test** message is never held or filtered.
- The window follows the wall clock of `general.timezone`, also on the days the clocks change: "22:00-07:00" ends at
  07:00 on the clock whether the night had 23, 24 or 25 hours. A time that does not exist (the skipped hour in spring)
  counts from the moment the clock passes it; one that happens twice (autumn) counts at both.
- Changes made on the Settings page apply at once. A message that is waiting is timed again under the new rules
  (switching quiet hours off sends what they held), and what was announced before stays known, so its "resolved"
  still goes out.

The time zone is read from the system's zone data (`/usr/share/zoneinfo`, present in the container image; on Windows,
which has none, from the `tzdata` package that is installed with Pengumon there); `UTC` and
fixed offsets such as `+01:00` always work.

## Formats

### `json`

A POST with `Content-Type: application/json`. Fields are stable: new ones may be added, none is renamed or changes
meaning (`version` rises if that ever has to happen).

```json
{
  "version": 1,
  "source": "pengumon",
  "event": "incident_started",
  "severity": "critical",
  "title": "Pengumon: ISP Outage",
  "message": "Cause: Internet Backbone Transit Loss\nHub unreachable\nLine load: 72%",
  "incident_id": 42,
  "cause": "internet_backbone_transit_loss",
  "service_id": null,
  "started_at": "2026-11-02T18:04:11Z",
  "ended_at": null,
  "duration_seconds": null,
  "load_pct": 72.0,
  "link": "http://pengu.lan:8000"
}
```

- `event`: `incident_started`, `incident_resolved`, `service_down`, `service_up`, `path_changed`, `test`
- `severity`: `info`, `warning` or `critical` (the incident's; resolved, back and test are `info`, an app that is down `warning`)
- `title`, `message`: The text ntfy shows
- `incident_id`, `cause`: The incident (network incidents only; `null` otherwise). The same id is in the dashboard
- `service_id`: The `service_checks` id, for `service_down` and `service_up`
- `started_at`, `ended_at`: UTC, ISO 8601; `ended_at` is `null` until it has ended. For services, the start is when the checks began failing
- `duration_seconds`, `load_pct`: When known, else `null`
- `link`: `notifications.public_url`, or `null`; may end in a page (`#live`)

### `slack`

`{"text": "*Title*\nmessage\n<link|Open the dashboard>"}`. `&`, `<` and `>` are escaped as Slack requires.

### `discord`

`{"content": "**Title**\nmessage\nlink", "allowed_mentions": {"parse": []}}`. The content is cut to Discord's limit of
2000 characters (the link is kept), and `allowed_mentions` stops anything in a message from pinging @everyone.

### `path_changed`

Sent once per set of changes when the background route check (discovery rerun daily and after an incident ends) finds
that the route differs from the applied path (your own hops, the line or your provider's). `severity` is `info`, `incident_id`, `cause` and `service_id`
are `null`, and `message` lists the changes in plain words, one per line (at most six, then "...and N more"). `link`
is `notifications.public_url` followed by `#live`, which opens Path health with its "Your path changed · Review"
notice; ntfy gets the same link as its click action.

## Examples

### Home Assistant

1. In Home Assistant, create an automation whose trigger is **Webhook**, with a webhook id such as
   `pengumon-alerts` and "Allow … only from local network" left on. Its URL is
   `http://<home-assistant>:8123/api/webhook/pengumon-alerts`.
2. In Pengumon:

   ```yaml
   notifications:
     webhooks:
       - { name: home-assistant, url: "http://homeassistant.lan:8123/api/webhook/pengumon-alerts", format: json }
   ```

3. In the automation, the payload is `trigger.json`. For example, a notification action with
   `title: "{{ trigger.json.title }}"` and `message: "{{ trigger.json.message }}"`, or a condition
   `{{ trigger.json.event == 'incident_started' and trigger.json.severity == 'critical' }}` to flash a light.

The webhook id is the secret: it is in the URL, so it is kept out of logs and the dialog. A Home Assistant REST
endpoint that needs a token works too, with `headers: { Authorization: "secret:ha_token" }`.

### Discord

Server settings, Integrations, Webhooks, New Webhook, Copy Webhook URL:

```yaml
notifications:
  webhooks:
    - { name: discord, url: "https://discord.com/api/webhooks/<id>/<token>", format: discord }
```

### Slack

Create an app with Incoming Webhooks and copy the webhook URL:

```yaml
notifications:
  webhooks:
    - { name: slack, url: "https://hooks.slack.com/services/<T>/<B>/<token>", format: slack }
```

### Trying it without a receiver

```bash
python3 - <<'PY'
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        print(self.rfile.read(int(self.headers["Content-Length"])).decode(), flush=True)
        self.send_response(204); self.end_headers()
http.server.HTTPServer(("0.0.0.0", 9099), H).serve_forever()
PY
```

Point a webhook at `http://<this host>:9099/`, open the Alerts dialog and press **Send test**.
