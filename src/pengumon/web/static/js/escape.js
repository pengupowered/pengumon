// What goes into markup: HTML escaping and numbers as digits.

// A number for markup: its digits, or '' for anything that is not a finite number (a value that is a string by mistake
// cannot carry markup into a template that expects a number).
export function numText(value) {
  const n = Number(value);
  return value !== null && value !== undefined && String(value).trim() !== '' && Number.isFinite(n) ? String(n) : '';
}

export function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text).replace(/[&<>"']/g, function(m) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }[m];
  });
}
