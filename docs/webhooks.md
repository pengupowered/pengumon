# Alerts by webhook

Webhooks are a second alert channel next to [ntfy](telemetry.md#-notifications). They receive exactly the alerts ntfy
does, after the same rules, so you can use either, both, or several webhooks:

- an incident starts (not for expected `busy` conditions), at most one message per cause every 5 minutes;
- an announced incident resolves;
- one of your [own apps](services.md) has failed for `notify_after_seconds`, and when it answers again.

Delivery runs in a background task with a timeout (5 s, `timeout_seconds`). A failed delivery is retried once after
2 seconds when the cause looks temporary (timeout, connection error, HTTP 429 or 5xx) and then logged as
`Webhook <name> not delivered: <reason>`; it never stops the monitor. Logs never contain the URL (a webhook id or token
is often part of it) or a header value.

## Configuration

In `config.yaml`:

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

The **Alerts** dialog lists every configured channel (name, format and host; no path, query or header) and **Send
test** sends one message through all of them and shows what each answered. The same is `GET /api/notifications` and
`POST /api/notifications/test`.

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
  "message": "Cause: internet_backbone_transit_loss\nHub unreachable\nLine load: 72%",
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
that your provider's route differs from the applied path. `severity` is `info`, `incident_id`, `cause` and `service_id`
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
