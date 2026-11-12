// Copying text to the clipboard.

// Copies `text` to the clipboard; resolves true when it did. The Clipboard API exists only in a secure context (HTTPS or
// localhost): a page served over plain HTTP on the LAN has no navigator.clipboard, so it copies through a hidden
// textarea and execCommand instead.
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  const back = document.activeElement;
  document.body.appendChild(area);
  area.select();
  let done = false;
  try { done = document.execCommand('copy'); } catch { done = false; }
  area.remove();
  if (back && typeof back.focus === 'function') back.focus();
  return done;
}

// The body of an Export view (Settings, the path editor): the YAML in a textarea, Copy, and a download link. Chrome blocks or
// warns about a file downloaded over plain HTTP from another machine ("insecure download"), whatever the page does;
