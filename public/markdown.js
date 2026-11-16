// markdown.js — tiny offline Markdown → HTML renderer for the demo UI.
//
// Deliberately small and dependency-free (the spec keeps the client 100%
// offline). It supports the block types the editor produces — headings, fenced
// code, blockquotes, bullet/numbered lists, hr, paragraphs — plus the inline
// spans people actually use (bold, italic, code, links) and basic tables.
// Everything is escaped before any HTML is emitted, and link targets are
// validated so raw-HTML injection and `javascript:` URLs never survive a
// render. Rendering happens in the browser over plaintext that is in RAM
// anyway; nothing here touches disk or the encrypted store.

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Only http/https/mailto/#/relative links survive the renderer. Anything else
// (javascript:, data:, vbscript:) degrades to an inert emphasized label.
const SAFE_URL = /^(https?:|mailto:|#|\/)/i;

function safeHref(raw) {
  const href = raw.trim().replace(/\s+/g, '');
  if (!href) return null;
  return SAFE_URL.test(href) ? href : null;
}

// Inline spans: `code`, **bold**, *italic*, and [text](url).
// Escaping runs first so any raw HTML the user typed is inert literal text.
export function renderInline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  out = out.replace(
    /\[([^\]\n]+)\]\(((?:[^()\s]|\([^()]*\))*)\)/g,
    (_, label, target) => {
      const href = safeHref(target);
      // target is already escaped (renderInline escapes before matching)
      return href ? `<a href="${href}">${label}</a>` : `<em>${label}</em>`;
    }
  );
  return out;
}

export function renderMarkdown(src) {
  const lines = String(src == null ? '' : src).replace(/\r\n/g, '\n').split('\n');
  const html = [];
  const open = (t) => html.push(t);
  const close = (t) => html.push(t);

  let listType = null; // 'ul' | 'ol' | null
  let inTable = false;
  let tableRows = [];
  let tableHeader = null;

  const flushList = () => {
    if (listType) {
      close(`</${listType}>`);
      listType = null;
    }
  };
  const closeTable = () => {
    if (!inTable) return;
    html.push('<table>');
    if (tableHeader) {
      open('<thead><tr>');
      for (const c of tableHeader) open(`<th>${c}</th>`);
      close('</tr></thead>');
      tableHeader = null;
    }
    if (tableRows.length) {
      for (const row of tableRows) {
        open('<tr>');
        for (const c of row) open(`<td>${c}</td>`);
        close('</tr>');
      }
      tableRows = [];
    }
    close('</table>');
    inTable = false;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (!line.trim()) {
      closeTable();
      flushList();
      continue;
    }

    // headings #..######
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      closeTable();
      flushList();
      const level = h[1].length;
      html.push(`<h${level}>${renderInline(h[2])}</h${level}>`);
      continue;
    }

    // horizontal rule: ***, ---, ___
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      closeTable();
      flushList();
      html.push('<hr>');
      continue;
    }

    // fenced code block
    const fence = /^\s*(```|~~~)\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      closeTable();
      flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*(```|~~~)\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      const lang = fence[2] ? ` class="lang-${escapeHtml(fence[2])}"` : '';
      html.push(`<pre${lang}><code>${escapeHtml(buf.join('\n'))}</code></pre>`);
      continue;
    }

    // blockquote
    const q = /^>\s?(.*)$/.exec(line);
    if (q) {
      closeTable();
      flushList();
      html.push(`<blockquote>${renderInline(q[1])}</blockquote>`);
      continue;
    }

    // list items
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      closeTable();
      const body = (ul || ol)[1];
      const type = ul ? 'ul' : 'ol';
      if (listType !== type) {
        flushList();
        listType = type;
        html.push(`<${type}>`);
      }
      html.push(`<li>${renderInline(body)}</li>`);
      continue;
    }

    // table: lines whose first cell is a leading | or contain a bare |
    const isTableRow = /^\s*\|/.test(line) || /[^\\]?\|/.test(line.replace(/^\s*/, ''));
    if (isTableRow) {
      flushList();
      if (!inTable && /^\s*\|\s*[-: ]+\s*\|/.test(line)) {
        // a lone separator before any header row — skip, nothing to render
        continue;
      }
      const cells = line
        .replace(/^\s*\|/, '')
        .replace(/\|\s*$/, '')
        .split('|')
        .map((c) => renderInline(c.trim()));
      if (!inTable) {
        inTable = true;
        tableRows = [];
        tableHeader = cells;
      } else if (/^\s*\|?[\s-]*[-:]{1,}[\s-]*(\|[\s-]*[-:]{1,}[\s-]*)*\|?\s*$/.test(line)) {
        // header separator row (| --- | :--: |) — already captured as header
        continue;
      } else {
        tableRows.push(cells);
      }
      continue;
    }

    closeTable();
    flushList();
    html.push(`<p>${renderInline(line)}</p>`);
  }

  closeTable();
  flushList();
  return html.join('');
}

export default renderMarkdown;
