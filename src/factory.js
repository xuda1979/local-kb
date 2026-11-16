// factory.js — end-to-end wiring: create/unlock a workspace over any adapter.

import {
  importAesGcm,
  deriveKeyBytes,
  deriveWorkspaceKey,
} from './crypto.js';
import { createWorkspace, unlockWorkspace } from './workspace.js';
import { DocStore } from './store.js';
import { exportKB, serializeKB, deserializeKB, importKB } from './bak.js';
import { createDoc } from './docmodel.js';
import { nowISO, uuid } from './util.js';

export async function createKb(adapter, masterPassword, wsName = 'default') {
  const ws = await createWorkspace(masterPassword, wsName || 'default');
  await adapter.saveParams({
    name: ws.name,
    version: ws.version,
    salt: ws.salt,
    iterations: ws.iterations,
    verifier: ws.verifier,
  });
  const wsHandle = await importAesGcm(ws.workspaceKey);
  return { workspace: ws.workspaceKey, store: new DocStore(adapter, wsHandle) };
}

export async function openKb(adapter, masterPassword) {
  const params = await adapter.getParams();
  if (!params) return { ok: false, error: 'no-workspace' };
  const result = await unlockWorkspace(params, masterPassword);
  if (!result.ok) return { ok: false, error: result.error };
  const wsHandle = await importAesGcm(result.workspace.workspaceKey);
  const store = new DocStore(adapter, wsHandle);
  await store.reindexAll(); // rebuild the in-memory FTS index on unlock
  return {
    ok: true,
    workspace: result.workspace,
    store,
    params,
  };
}

// A line is a pipe-table row if it starts with `|` or contains a bare pipe —
// the same heuristic the markdown renderer uses, so preview and block parsing
// always agree on what is a table.
function isTableRow(l) {
  return l.startsWith('|') || l.includes('|');
}

// A fenced code block starts with an opening fence, optionally carrying a
// language tag, and runs until a closing fence — the exact shape the markdown
// renderer consumes (public/markdown.js). The whole region is stored verbatim
// (fences, tag, body lines) inside one code block, so code round-trips
// losslessly (user manual 3.5) and its body stays searchable (user manual 3.4).
// An unclosed fence runs to the end of the document, mirroring the renderer.
function codeFenceMatch(l) {
  const m = /^\s*(```|~~~)\s*([\w+-]*)\s*$/.exec(l);
  return m ? { marker: m[1], lang: m[2] } : null;
}

export function textToBlocks(text) {
  const blocks = [];
  const tableGroup = [];
  const flushTable = () => {
    if (tableGroup.length) {
      blocks.push({ type: 'table', text: tableGroup.join('\n') });
      tableGroup.length = 0;
    }
  };

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; ) {
    const l = lines[i].trim();
    if (!l) {
      flushTable();
      i++;
      continue;
    }
    if (codeFenceMatch(l)) {
      // One code block per fenced region, verbatim from opening to closing fence.
      flushTable();
      const raw = [lines[i]];
      i++;
      while (i < lines.length) {
        const body = lines[i];
        raw.push(body);
        i++;
        if (/^\s*(```|~~~)\s*$/.test(body)) break; // closing fence wraps the region
      }
      blocks.push({ type: 'code', text: raw.join('\n') });
      continue;
    }
    // Consecutive pipe rows (header, separator, body) group into one table
    // block; they round-trip verbatim through blocksToMarkdown.
    if (isTableRow(l)) {
      tableGroup.push(l);
      i++;
      continue;
    }
    flushTable();
    if (/^#{1,6}\s/.test(l)) blocks.push({ type: 'heading', text: l.replace(/^#{1,6}\s*/, '') });
    else if (/^>\s?/.test(l)) blocks.push({ type: 'quote', text: l.replace(/^>\s?/, '') });
    else blocks.push({ type: 'paragraph', text: l });
    i++;
  }
  flushTable();
  return blocks;
}

export function blocksToMarkdown(blocks) {
  return blocks
    .map((b) => {
      // code and table blocks carry source verbatim (fences / pipe rows), so
      // they round-trip unchanged; every other block re-emits its own marker.
      switch (b.type) {
        case 'heading':
          return '# ' + b.text;
        case 'quote':
          return '> ' + b.text;
        default:
          return b.text;
      }
    })
    .join('\n');
}

export { createDoc, nowISO, uuid };
