// What the Settings dialog's modules share: the element lookup, whether it is open, and the state of the open dialog
// (module-level `let`s cannot be assigned from another module, so the values that change live as properties of one object).

export const $ = (id) => document.getElementById(id);
export const MODAL = 'settingsModal';
export const isOpen = () => $(MODAL).classList.contains('active');

export const U = {
  saving: false,
  draftSource: '', // '' | 'restore' (a loaded version) | 'yaml_import' (an imported file): sent as ?source= on Save
  versions: null, // GET /api/settings/versions, once read
  versionsShown: false,
  validateSeq: 0, // the newest validation; an answer for an older one is dropped
  openSeq: 0, // the newest openSettings; one that was overtaken (closed, or opened again) stops after its await
  problemsShown: [], // what stops Save now (problems.js), for the summary above the footer and its jump
  notes: [] // [{level, html}] at the top of the page
};
export const editors = {}; // list name -> the editor of lists/, bound to its container while rendered
