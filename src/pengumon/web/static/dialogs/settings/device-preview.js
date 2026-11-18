// Settings > Display, "Preview without a device": for each saved device, how this browser shows the dashboard (as set up, not
// there, or a router as a basic UPnP one). Start writes the choice (js/device-preview.js, the cookie the server reads) and
// loads the page again; it is not a setting and Save does not touch it. Only this browser changes; the devices are still
// read and stored.

import { escapeHtml as e } from '../../js/escape.js';
import { MODES, currentPreview, startPreview } from '../../js/device-preview.js';
import { registerActions } from '../../js/dispatch.js';
import { S, listState } from './store.js';

const drivers = () => (S.schema && S.schema.lists && S.schema.lists.devices && S.schema.lists.devices.drivers) || {};
const saved = () => listState('devices').items || [];

export function devicePreviewHtml() {
  const devices = saved();
  const now = currentPreview();
  const on = Object.keys(now).length > 0;
  const rows = devices.map((d) => {
    const driver = drivers()[d.driver] || {};
    const basic = (driver.offers || []).includes('wan_counters') && (driver.offers || []).length > 1;
    const options = [['', 'As set up'], ['absent', 'Not there'], ...(basic ? [['basic', 'As a basic UPnP router']] : [])];
    const id = `set-prev-${e(d.id)}`;
    return `<div class="set-row set-prev-row"><label for="${id}"><b>${e(driver.label || d.driver)}</b> <span class="muted">${e(d.id)}</span></label>
      <select class="input" id="${id}" data-preview-device="${e(d.id)}" data-preview-label="${e(driver.label || d.driver)}">${options.map(([v, w]) => `<option value="${v}"${(now[d.id] || '') === v ? ' selected' : ''}>${e(w)}</option>`).join('')}</select></div>`;
  }).join('');
  return `<div class="set-group" id="set-device-preview"><h4 class="set-group-t">Preview without a device</h4>
    <p class="fine">See the dashboard as a household without your router or modem would see it, or with a basic UPnP router that only counts the internet traffic. Only this browser changes: Pengumon still reads every device and keeps its history, and ending the preview brings everything back. This is not a setting: Save does not change it.</p>
    ${devices.length ? rows : '<p class="fine">No device is set up, so the dashboard already looks like this.</p>'}
    ${devices.length ? `<p class="set-prev-btns"><button type="button" class="btn sm primary" data-action="startDevicePreview">${on ? 'Change the preview' : 'Start the preview'}</button>${on ? ' <button type="button" class="btn sm" data-action="endDevicePreview">End the preview</button>' : ''}</p>` : ''}
  </div>`;
}

registerActions('click', {
  startDevicePreview: () => {
    const modes = {};
    const names = {};
    document.querySelectorAll('#set-device-preview [data-preview-device]').forEach((sel) => {
      if (sel.value in MODES) { modes[sel.dataset.previewDevice] = sel.value; names[sel.dataset.previewDevice] = sel.dataset.previewLabel; }
    });
    startPreview(modes, names);
  }
});
