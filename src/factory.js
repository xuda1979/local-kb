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

export function textToBlocks(text) {
  return text
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => {
      if (/^#{1,6}\s/.test(l)) return { type: 'heading', text: l.replace(/^#{1,6}\s*/, '') };
      if (/^\s*```/.test(l)) return { type: 'code', text: l.replace(/^\s*```\s*/, '') };
      if (/^\s*>\s?/.test(l)) return { type: 'quote', text: l.replace(/^\s*>\s?/, '') };
      if (/^\s*[-*]\s/.test(l)) return { type: 'paragraph', text: l };
      return { type: 'paragraph', text: l };
    });
}

export function blocksToMarkdown(blocks) {
  return blocks
    .map((b) => {
      switch (b.type) {
        case 'heading':
          return '# ' + b.text;
        case 'quote':
          return '> ' + b.text;
        case 'code':
          return '```\n' + b.text + '\n```';
        case 'table':
          return b.text;
        default:
          return b.text;
      }
    })
    .join('\n');
}

export { createDoc, nowISO, uuid };
