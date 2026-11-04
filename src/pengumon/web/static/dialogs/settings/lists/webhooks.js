// Webhooks (notifications.webhooks): a name, a format, a timeout, and two kinds of secret that the page never reads back:
// the address (slot webhook.<id>.url) and each header's value (slot webhook_header.<name>; the item keeps the reference
// "secret:<name>"). A webhook without an address is "URL needed" and cannot be saved. Applies without a restart.

import { escapeHtml } from '../../../js/util.js';
import { S, setSecret } from '../store.js';
import { rememberSlot, slotHtml, slotState } from '../secrets.js';
import { makeListEditor, freshId } from './kit.js';

const SECRET = 'secret:';
const urlSlot = (id) => `webhook.${id}.url`;
const headerSlot = (ref) => `webhook_header.${ref.startsWith(SECRET) ? ref.slice(SECRET.length) : ref}`;

// An address is there when the server has one or this draft sets one (and does not clear it).
function hasUrl(id) {
  if (urlSlot(id) in S.draft.secrets) return S.draft.secrets[urlSlot(id)] !== '';
  return !!slotState(urlSlot(id)).set;
}

// Keeps Save off while a webhook has no address; the same rule the server applies.
function checkUrls(items) {
  Object.keys(S.invalid).filter((k) => k.startsWith('webhook-url:')).forEach((k) => delete S.invalid[k]);
  items.forEach((item) => { if (!hasUrl(item.id)) S.invalid[`webhook-url:${item.id}`] = 'URL needed'; });
}

const urlSpec = { label: 'Address (URL)', kind: 'url', env: null };

const editor = makeListEditor({
  id: 'webhooks',
  label: 'Webhooks',
  noun: 'webhook',
  addLabel: 'Add a webhook',
  empty: 'No webhooks. Add one to send alerts to Slack, Discord or any service that takes a JSON post.',
  note: 'The address and header values are secrets: you can replace or remove them, but they are never shown again or exported.',
  resetLabel: 'Remove all webhooks',
  fields: [
    { f: 'name', label: 'Name', kind: 'text', required: true },
    { f: 'format', label: 'Format', kind: 'choice', choices: ['json', 'slack', 'discord'], required: true },
    { f: 'timeout_seconds', label: 'Timeout (seconds)', kind: 'number', required: false, hint: 'Above 0 and up to 60.' }
  ],
  blank: (items) => {
    const item = { id: freshId('webhook', items.map((i) => i.id)), name: 'New webhook', format: 'json', headers: {}, timeout_seconds: 5 };
    return item;
  },
  removed: (item) => {
    delete S.draft.secrets[urlSlot(item.id)];
    Object.values(item.headers || {}).forEach((ref) => { delete S.draft.secrets[headerSlot(ref)]; });
    delete S.invalid[`webhook-url:${item.id}`];
  },
  title: (item) => item.name,
  summary: (item) => `${item.format}${hasUrl(item.id) ? '' : ' · URL needed'}`,
  chips: (item) => (hasUrl(item.id) ? '' : '<span class="chip st st-crit" data-chip="url">URL needed</span>'),
  extra: (item) => {
    rememberSlot(urlSlot(item.id), urlSpec);
    const headers = Object.entries(item.headers || {}).map(([name, ref]) => {
      const slot = headerSlot(ref);
      rememberSlot(slot, { label: name, kind: 'token', env: null });
      return `<div class="set-hdr"><div class="set-hdr-row">${slotHtml(slot, { label: `Header ${name}`, kind: 'token', env: null })}
        <button type="button" class="btn sm" data-la="header-remove" data-hdr="${escapeHtml(name)}" data-needs-edit>Remove header</button></div></div>`;
    }).join('');
    return `<h5 class="set-sub">Address</h5>${slotHtml(urlSlot(item.id), urlSpec)}
      <h5 class="set-sub">Headers</h5>${headers || '<p class="fine top">No headers.</p>'}
      <div class="set-hdr-add"><input class="input" type="text" data-hdr-name placeholder="Header name, e.g. Authorization" maxlength="100" aria-label="New header name">
        <button type="button" class="btn sm" data-la="header-add" data-needs-edit>Add header</button></div>`;
  },
  action: (act, button, items, ctx, { draw }) => {
    const card = button.closest('.set-card');
    const item = items[Number(card.dataset.i)];
    if (act === 'header-add') {
      const name = card.querySelector('[data-hdr-name]').value.trim();
      if (!name || item.headers[name]) return true;
      item.headers[name] = `${SECRET}${freshId(`${item.id}_${name}`, Object.values(item.headers).map((r) => r.slice(SECRET.length)))}`;
      ctx.markDirty();
      draw();
      return true;
    }
    if (act === 'header-remove') {
      const name = button.dataset.hdr;
      setSecret(headerSlot(item.headers[name]), undefined);
      delete item.headers[name];
      ctx.markDirty();
      draw();
      return true;
    }
    return false;
  },
  after: (el, item, i, card, type, ctx, items, rename) => {
    // an unsaved webhook's id follows its name (its address slot moves with it); a saved one's id never changes
    if (el.dataset.f === 'name' && type === 'change' && !ctx.savedIds().includes(item.id) && /^webhook(_\d+)?$/.test(item.id)) {
      const next = freshId(item.name, items.filter((o) => o !== item).map((o) => o.id));
      if (next !== item.id) {
        const old = urlSlot(item.id);
        if (old in S.draft.secrets) { const v = S.draft.secrets[old]; delete S.draft.secrets[old]; S.draft.secrets[urlSlot(next)] = v; }
        rename(item.id, next);
        item.id = next;
        card.dataset.id = next;
        ctx.redraw();
      }
    }
  }
});

// Called after every edit of the draft: the "URL needed" marks, and Save held off while a changed list has a webhook without one.
editor.sync = () => {
  const items = editor.collect();
  if ('webhooks' in S.draft.lists) checkUrls(items);
  else Object.keys(S.invalid).filter((k) => k.startsWith('webhook-url:')).forEach((k) => delete S.invalid[k]);
  document.querySelectorAll('[data-list=webhooks] .set-card').forEach((card) => {
    const item = items[Number(card.dataset.i)];
    if (!item) return;
    const sum = card.querySelector('summary');
    const chip = sum.querySelector('[data-chip=url]');
    const has = hasUrl(item.id);
    if (!has && !chip) sum.insertAdjacentHTML('beforeend', '<span class="chip st st-crit" data-chip="url">URL needed</span>');
    if (has && chip) chip.remove();
    card.querySelector('.set-card-s').textContent = `${item.format}${has ? '' : ' · URL needed'}`;
  });
};
export default editor;
