// The words for a hop's role and an incident's cause, from the one table each; no page code is imported here.

// The roles a hop can have (TargetConfig.category), nearest first: the one table of their words. `short` is the name a
// panel gives a hop of that role (Router, ISP hop 2); `long` is the role in the editor's plain words (Your network).
// Every place that says a role, a stage or a category reads it from here.
const ROLE_TABLE = {
  local: { short: 'Router', long: 'Your network' },
  gateway: { short: 'Gateway', long: 'Gateway (modem)' },
  isp: { short: 'ISP hop', long: 'Provider' },
  public: { short: 'Internet', long: 'Internet' },
  dns: { short: 'DNS lookup', long: 'DNS server' },
  custom: { short: 'Other', long: 'Other' }
};
export const ROLES = Object.entries(ROLE_TABLE).map(([id, w]) => [id, w.long]); // [id, long word] for a select
export const ROLE_WORD = Object.fromEntries(ROLES);
export const ROLE_SHORT = Object.assign(Object.create(null), Object.fromEntries(Object.entries(ROLE_TABLE).map(([id, w]) => [id, w.short]))); // (no prototype: a category named constructor finds nothing)

// Where an incident's fault sat (the server's `cause` category), in the timeline's lane order: `short` is the lane and
// the chip, `long` says what it means in the report. Router (local probes), Gateway (the gateway / modem on its home-side
// address, which does not use the line), Line (loss from the provider's first router on while the gateway answers: the
// line or that router), ISP (the provider's network beyond it).
export const CAUSES = [
  ['router', 'Router', 'Router: home network'],
  ['gateway', 'Gateway', 'Gateway (modem)'],
  ['line', 'Line', 'Line or first ISP router'],
  ['isp', 'ISP', 'ISP: beyond its first router'],
  ['internet', 'Internet', 'Internet servers'],
  ['dns', 'DNS', 'DNS'],
  ['load', 'Load', 'Busy line'],
  ['service', 'Service', 'Host service'],
  ['unknown', 'Other', 'Unknown']
];
export const CAUSE_SHORT = Object.fromEntries(CAUSES.map(([id, short]) => [id, short]));
export const CAUSE_LONG = Object.fromEntries(CAUSES.map(([id, , long]) => [id, long]));
export const ROLE_HINT = {
  local: 'router or LAN device', gateway: 'modem or the provider\'s gateway in your home', isp: 'a router of your provider',
  public: 'a server on the internet', dns: 'a DNS server', custom: 'not a stage of the path (a NAS, a VPN, a server)'
};

// What each role changes, in one plain line, shown where the role is picked (the classifier's rules: docs/diagnosis.md).
export const ROLE_EFFECT = {
  local: 'If this stops answering, the outage is placed in your home network (router or Wi-Fi), not on your provider; the provider dossier quotes its loss to show your own network is fine.',
  gateway: 'Probed on its home-side address, so these pings never use the line: loss here while your router is fine is placed at the gateway itself (or the link to it), not on the line; loss at the router before it is yours.',
  isp: 'Loss only here, while later hops answer, is not counted as packet loss (provider routers deprioritize ping); loss that carries on to the internet is your provider\'s.',
  public: 'Every stage before it fine but this failing is an internet outage; the Internet figure on Path health is the median of these servers.',
  dns: 'Failures here are reported as a DNS problem, not as loss on the path.',
  custom: 'Probed and charted, but never blamed for an outage and left out of the dossier\'s findings.'
};

// The roles whose hops can be a configured device (engine/device_links.py): your network and the gateway.
export const DEVICE_ROLES = ['local', 'gateway'];
