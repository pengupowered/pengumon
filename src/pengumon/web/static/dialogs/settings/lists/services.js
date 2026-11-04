// The host app checks (service_checks.services): an http(s) address Pengumon fetches every few seconds and opens an
// incident for when it fails. Applies without a restart.

import { makeListEditor, freshId } from './kit.js';

export default makeListEditor({
  id: 'services',
  label: 'Host app checks',
  noun: 'check',
  addLabel: 'Add a check',
  empty: 'No checks yet. Add one to watch an app on this network.',
  note: 'Each check fetches its address every interval and opens an incident after the failures set above. An id is fixed once the check is saved.',
  ordered: true,
  resetLabel: 'Remove all checks',
  fields: [
    { f: 'name', label: 'Name', kind: 'text', required: true },
    { f: 'id', label: 'Id', kind: 'text', required: true, mono: true, hint: 'Letters, digits and dashes. Fixed once saved.' },
    { f: 'url', label: 'Address to fetch', kind: 'text', required: true, mono: true, placeholder: 'http://192.0.2.10:8080/health' },
    { f: 'timeout_seconds', label: 'Timeout (seconds)', kind: 'number', required: false, hint: '0.5 to 60.' },
    { f: 'open_url', label: 'Link to open the app', kind: 'text', nullable: true, mono: true },
    { f: 'description', label: 'Description', kind: 'text', nullable: true },
    { f: 'ca_file', label: 'CA file (for a private certificate)', kind: 'text', nullable: true, mono: true },
    { f: 'enabled', label: 'Checked', kind: 'bool' },
    { f: 'notify', label: 'Send an alert when it fails', kind: 'bool' }
  ],
  blank: (items) => ({ id: freshId('service', items.map((i) => i.id)), name: 'New check', url: '', enabled: true, timeout_seconds: 5, notify: true }),
  title: (item) => item.name,
  summary: (item) => item.url,
  after: (el, item, i, card, type, ctx, items) => {
    // an unsaved check's id follows its name until it is typed over
    if (el.dataset.f === 'name' && type === 'change' && !card.dataset.idEdited && item.id.startsWith('service')) {
      item.id = freshId(item.name, items.filter((o) => o !== item).map((o) => o.id));
      card.querySelector('[data-f=id]').value = item.id;
    }
    if (el.dataset.f === 'id') card.dataset.idEdited = '1';
  }
});
