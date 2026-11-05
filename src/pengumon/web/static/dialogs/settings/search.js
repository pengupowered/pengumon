// The search box above the Settings page: shows only the rows whose label, help text or key contain every word typed,
// across all sections (a section or group with nothing left is hidden too). Escape clears it (and does not close the
// dialog); with nothing typed Escape closes the dialog as always. Hiding is the `hidden` attribute, so nothing is rebuilt
// and the draft is untouched.

const $ = (id) => document.getElementById(id);

// The things a search can hide: a setting row (also the secret slots and the layout row) and a list editor.
const UNITS = '.set-row, .set-list-box';

function words() {
  return ($('settingsSearch')?.value || '').toLowerCase().split(/\s+/).filter(Boolean);
}

// The units inside `box` that are not inside another unit (a list editor's cards hold rows of their own).
const outer = (box) => [...box.querySelectorAll(UNITS)].filter((u) => !u.parentElement.closest(UNITS));

function haystack(unit) {
  return `${unit.dataset.key || unit.dataset.keys || ''} ${unit.textContent}`.toLowerCase();
}

// Applies the box's text to the rendered page; also called after the page is rebuilt.
export function applySearch() {
  const wanted = words();
  const root = $('settingsSections');
  if (!root) return;
  let shown = 0;
  outer(root).forEach((unit) => {
    const text = wanted.length ? haystack(unit) : '';
    unit.hidden = wanted.length > 0 && !wanted.every((w) => text.includes(w));
    if (!unit.hidden) shown += 1;
  });
  root.querySelectorAll('.set-group, .set-sec, .set-adv, .set-list').forEach((box) => {
    if (box.parentElement.closest(UNITS)) return; // inside a list editor: it is the editor's unit that hides
    box.hidden = wanted.length > 0 && !outer(box).some((u) => !u.hidden);
  });
  root.querySelectorAll('.set-adv:not([hidden])').forEach((box) => { if (wanted.length) box.open = true; });
  root.querySelectorAll('#set-sec-about').forEach((box) => { box.hidden = wanted.length > 0; });
  $('settingsSearchStatus').textContent = !wanted.length ? '' : shown ? `${shown} setting${shown === 1 ? '' : 's'} shown` : 'No setting matches. Clear the box to see all.';
}

export function clearSearch() {
  const box = $('settingsSearch');
  if (!box || !box.value) return false;
  box.value = '';
  applySearch();
  return true;
}

// Wired once: typing filters; Escape in the box clears it first.
export function initSearch() {
  const box = $('settingsSearch');
  box.addEventListener('input', applySearch);
  box.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && box.value) {
      ev.preventDefault(); // the dialog's own Escape (close) skips a prevented event
      clearSearch();
    }
  });
}
