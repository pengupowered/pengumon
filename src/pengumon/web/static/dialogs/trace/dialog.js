// Trace route: the MTR hops to a host (POST /api/mtr/trace), in the trace dialog. A hop that answered can be added as a
// probe target with one button (POST /api/targets), as the path stage picked beside it; hops that already are one say
// so.

import { registerActions } from '../../js/dispatch.js';
import { escapeHtml, icon, initTargets, keepKeyboard } from '../../js/util.js';
import { ROLE_SHORT } from '../../js/path-api.js';

const PRIVATE = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|127\.|f[cd]|fe80)/i;
let hops = [];
let monitored = new Set(); // addresses that are probe targets already
let traced = ''; // the host of the last trace

const $ = (id) => document.getElementById(id);

// The path stages a hop can be added as (Path health's words), and the one suggested: the first hop is the router; a
// private address after it is most often the modem or the provider's box (a cable provider's first router can be one
// too, hence the choice); a public one is an ISP hop, and the traced host itself the internet.
const STAGES = ['local', 'gateway', 'isp', 'public', 'custom'].map((id) => [id, ROLE_SHORT[id]]);

function suggestedStage(h) {
  if (h.hop_number === 1) return 'local';
  if (PRIVATE.test(h.ip)) return 'gateway';
  const last = hops.filter((x) => x.ip).slice(-1)[0];
  return last && last.hop_number === h.hop_number && h.ip === traced ? 'public' : 'isp';
}

function row(h) {
  const reply = h.ip && h.rtt_ms !== null && h.rtt_ms !== undefined;
  let action = '';
  if (h.ip) {
    const pick = suggestedStage(h);
    action = monitored.has(h.ip)
      ? '<span class="chip">monitored</span>'
      : `<select class="input sm-select" id="traceStage${h.hop_number}" aria-label="Add hop ${h.hop_number} as">${STAGES.map(([v, w]) => `<option value="${v}"${v === pick ? ' selected' : ''}>${w}</option>`).join('')}</select>`
        + ` <button type="button" class="btn sm outline" data-action="monitorHop" data-needs-edit data-arg="${h.hop_number}">${icon('plus')}Monitor</button>`;
  }
  return `<tr>
    <td class="lead num" data-l="Hop">${h.hop_number}</td>
    <td class="mono aside" data-l="Address">${escapeHtml(h.ip || '*')}</td>
    <td data-l="Name">${escapeHtml(h.label || '-')}</td>
    <td class="r num" data-l="RTT">${reply ? `${h.rtt_ms} ms` : '<span class="lost">no reply</span>'}</td>
    <td class="r tr-act">${action}</td>
  </tr>`;
}

function render() {
  const answered = hops.filter((h) => h.ip).length;
  if (!hops.length) {
    $('tracerouteResults').innerHTML = `<div class="empty st st-warn">${icon('warn')}<div><b>No hops came back</b>The trace finished without a single hop. Check the address and try again.</div></div>`;
    return;
  }
  $('tracerouteResults').innerHTML = `<div class="table-wrap"><table class="dlist trace"><thead><tr><th>Hop</th><th>Address</th><th>Name</th><th class="r">RTT</th><th><span class="sr-only">Monitor</span></th></tr></thead><tbody>${hops.map(row).join('')}</tbody></table></div>
    <p class="fine">${hops.length} hops, ${answered} answered. A hop that never replies cannot be probed; <b>Monitor</b> adds one that did as a target, probed every second, as the stage picked beside it.</p><div id="traceNote" role="status" aria-live="polite"></div>`;
}

async function loadMonitored() {
  try {
    const res = await fetch('/api/targets');
    if (res.ok) monitored = new Set((await res.json()).map((t) => t.address).filter(Boolean));
  } catch (_) { /* the Monitor buttons then simply show for every hop */ }
}

export async function runTracerouteModal(ev) {
  if (ev) ev.preventDefault();
  const host = ($('traceTargetInput').value || '1.1.1.1').trim();
  traced = host;
  const resultsDiv = $('tracerouteResults');
  const button = $('btnRunTrace');
  button.disabled = true;
  resultsDiv.innerHTML = `<div class="skel-stack" aria-busy="true"><div class="skel line" style="width:92%"></div><div class="skel line" style="width:76%"></div><div class="skel line" style="width:84%"></div></div><p class="fine">Tracing the hops to ${escapeHtml(host)}: usually 5 to 15 s, up to about 45 s when hops do not answer.</p>`;

  try {
    const [res] = await Promise.all([fetch('/api/mtr/trace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_host: host, family: Number(($('traceFamily') || {}).value || 4) })
    }), loadMonitored()]);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = Array.isArray(err.detail) ? err.detail.map((d) => String(d.msg || '').replace(/^Value error, /, '')).join(' ') : err.detail;
      throw new Error(detail || (res.status === 429 ? 'Too many traces in a minute. Wait a little and try again.' : 'Traceroute request failed'));
    }
    hops = await res.json();
    render();
  } catch (err) {
    resultsDiv.innerHTML = `<div class="empty st st-crit">${icon('warn')}<div><b>The trace failed</b>${escapeHtml(err.message)}</div></div>`;
  } finally {
    button.disabled = false;
  }
}

async function monitorHop(button) {
  const h = hops.find((x) => x.hop_number === Number(button.dataset.arg));
  if (!h || !h.ip) return;
  const note = $('traceNote');
  button.disabled = true;
  const name = (h.label || `Hop ${h.hop_number} (${h.ip})`).slice(0, 100);
  const body = {
    id: `hop_${h.hop_number}_${h.ip.replace(/[^a-zA-Z0-9]/g, '_')}`.slice(0, 64),
    name, address: h.ip, category: ($(`traceStage${h.hop_number}`) || {}).value || suggestedStage(h), type: 'icmp', enabled: true
  };
  try {
    const res = await fetch('/api/targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join(' ') : err.detail;
      throw new Error(/already exists/i.test(detail || '') ? 'That hop is a target already.' : (detail || `HTTP ${res.status}`));
    }
    monitored.add(h.ip);
    await initTargets();
    render();
    $('traceNote').innerHTML = `<div class="result st-good">${icon('ok')}<span><b>${escapeHtml(name)}</b> is a target now, probed every second.</span></div>`;
  } catch (err) {
    button.disabled = false;
    note.innerHTML = `<div class="result st-crit">${icon('down')}<span><b>Could not add the target.</b> ${escapeHtml(err.message)}</span></div>`;
  }
}

registerActions('submit', {
  runTraceroute: (el, ev) => runTracerouteModal(ev)
});
registerActions('click', {
  monitorHop: (el) => monitorHop(el)
});
keepKeyboard($('tracerouteModal'), '#traceTargetInput');
