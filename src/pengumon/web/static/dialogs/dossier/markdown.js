// A small Markdown renderer for the dossier text (pengumon.reports.render_dossier_markdown), which uses a fixed few
// constructs: # and ## and ### headings, bold, `code`, bullet lists, pipe tables and one fenced block. Everything is
// HTML-escaped first, so the server's text (target names, notes) can never become markup. Returns the document's title,
// its bold "key: value" lines under the title (meta), and the sections (body) as markup.

import { escapeHtml } from '../../js/escape.js';

const inline = (text) => escapeHtml(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
const numeric = (cell) => /^[+\-−]?[\d.,]+( ?(%|ms|s|dB|dBmV|MHz))?$/.test(cell.trim());
const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

function table(rows) {
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells); // rows[1] is the |:---| separator
  const right = head.map((_, i) => body.length > 0 && body.every((r) => numeric(r[i] || '')));
  return `<div class="doc-scroll"><table class="dlist"><thead><tr>${head.map((h, i) => `<th${right[i] ? ' class="r"' : ''}>${inline(h)}</th>`).join('')}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c, i) => (i === 0
    ? `<td class="lead">${inline(c)}</td>`
    : `<td data-l="${escapeHtml(head[i] || '')}"${right[i] ? ' class="r num"' : ''}>${inline(c)}</td>`)).join('')}</tr>`).join('')}</tbody></table></div>`;
}

export function renderMarkdown(markdown) {
  const lines = markdown.split('\n');
  let title = '';
  const meta = [];
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    let m;
    if ((m = /^# (.+)/.exec(line))) {
      title = inline(m[1]);
      i += 1;
      while (i < lines.length && /^\*\*.+\*\*/.test(lines[i])) { meta.push(`<p class="doc-meta">${inline(lines[i])}</p>`); i += 1; }
    } else if ((m = /^## (?:\d+\. )?(.+)/.exec(line))) {
      out.push(`<h4>${inline(m[1])}</h4>`);
      i += 1;
    } else if ((m = /^### (.+)/.exec(line))) {
      out.push(`<h5>${inline(m[1])}</h5>`);
      i += 1;
    } else if (/^```/.test(line)) {
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre class="dossier-code">${escapeHtml(code.join('\n'))}</pre>`);
    } else if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { rows.push(lines[i]); i += 1; }
      out.push(rows.length >= 2 ? table(rows) : `<p>${inline(rows[0])}</p>`);
    } else if (/^- /.test(line)) {
      const items = [];
      while (i < lines.length && /^- /.test(lines[i])) { items.push(`<li>${inline(lines[i].slice(2))}</li>`); i += 1; }
      out.push(`<ul>${items.join('')}</ul>`);
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#|\||- |```)/.test(lines[i])) { para.push(lines[i]); i += 1; }
      out.push(`<p>${inline(para.join(' '))}</p>`);
    }
  }
  return { title, meta: meta.join(''), body: out.join('') };
}
