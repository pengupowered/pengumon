// DNS lookups (dns_probes): a name looked up every second, shown in Path health beside the hops. Applies without a restart.

import { makeListEditor, freshId, slug } from './kit.js';

const TYPES = ['A', 'AAAA', 'CNAME', 'MX', 'NS', 'TXT'];
const NO_SERVER = 'No nameserver: the lookup uses the system resolver and the record type is ignored.';

export default makeListEditor({
  id: 'dns_probes',
  label: 'DNS lookups',
  noun: 'lookup',
  addLabel: 'Add a lookup',
  empty: 'No lookups. Without them Path health shows no DNS stage.',
  note: 'An id is fixed once the lookup is saved, and may not be the id of a hop in the probe path.',
  resetLabel: 'Back to the two default lookups',
  fields: [
    { f: 'name', label: 'Name', kind: 'text', required: true },
    { f: 'id', label: 'Id', kind: 'text', required: true, mono: true, hint: 'Letters, digits and dashes. Fixed once saved.' },
    { f: 'domain', label: 'Name to look up', kind: 'text', required: true, mono: true, placeholder: 'example.com' },
    { f: 'nameserver', label: 'Nameserver', kind: 'text', nullable: true, mono: true, placeholder: '1.1.1.1', hint: NO_SERVER, hintIf: (item) => !item.nameserver },
    { f: 'record_type', label: 'Record type', kind: 'choice', choices: TYPES, required: true },
    { f: 'color', label: 'Line color', kind: 'text', nullable: true, mono: true, placeholder: '#58a6ff' },
    { f: 'enabled', label: 'Looked up', kind: 'bool' }
  ],
  blank: (items) => ({ id: freshId('dns_lookup', items.map((i) => i.id)), name: 'DNS Lookup', domain: '', nameserver: null, record_type: 'A', enabled: true, color: null }),
  title: (item) => item.name,
  summary: (item) => `${item.domain || ''}${item.nameserver ? ` via ${item.nameserver}` : ' (system resolver)'}`,
  after: (el, item, i, card, type, ctx, items) => {
    if (el.dataset.f === 'nameserver') {
      const hint = card.querySelector('[data-hint=nameserver]');
      if (hint) hint.hidden = !!item.nameserver;
    }
    // the id of a new lookup follows the domain until it is edited by hand or saved
    if (el.dataset.f === 'domain' && type === 'change' && !card.dataset.idEdited && !ctx.savedIds().includes(item.id)
        && (item.id.startsWith('dns_lookup') || item.id === card.dataset.autoId)) {
      item.id = freshId(`dns_${slug(item.domain)}`, items.filter((o) => o !== item).map((o) => o.id));
      card.dataset.autoId = item.id;
      card.querySelector('[data-f=id]').value = item.id;
    }
    if (el.dataset.f === 'id') card.dataset.idEdited = '1';
  }
});
