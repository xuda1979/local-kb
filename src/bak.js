// bak.js — .kb export/import (machine migration).
//
// The .kb file is the same nested-DEK design as the live DB — it contains ONLY
// ciphertext (workspace params + wrapped DEKs), no plaintext. It can be imported
// on a different machine using the master password.
//
// Format (JSON, base64url):
//
//   {
//     "app": "local-kb",
//     "format": 1,
//     "exportedAt": "...",
//     "workspace": { name, version, salt, iterations, verifier },   // cleartext params
//     "documents": [
//       {
//         "id": "...",
//         "dekIv": "...", "dekData": "...",          // wrapped by workspace key
//         "iv": "...", "data": "..."                  // encrypted with per-doc DEK
//       }
//     ]
//   }

import { toBase64, utf8e } from './crypto.js';

const APP_ID = 'local-kb';
const FORMAT = 1;

export function exportKB(params, docs) {
  const wsMeta = {
    name: params.name,
    version: params.version,
    salt: params.salt,
    iterations: params.iterations,
    verifier: params.verifier,
  };
  return {
    app: APP_ID,
    format: FORMAT,
    exportedAt: new Date().toISOString(),
    workspace: wsMeta,
    documents: docs.map((d) => ({
      id: d.id,
      dekIv: d.dekIv,
      dekData: d.dekData,
      iv: d.iv,
      data: d.data,
      createdAt: d.createdAt,
      updatedAt: d.updatedAt,
    })),
  };
}

export function serializeKB(kb) {
  return new TextEncoder().encode(JSON.stringify(kb, null, 2));
}

export function deserializeKB(buf) {
  let obj;
  try {
    obj = JSON.parse(new TextDecoder().decode(buf));
  } catch {
    throw new Error('invalid .kb file');
  }
  if (!obj || obj.app !== APP_ID) throw new Error('not a local-kb file');
  return obj;
}

// Import from a plain object (from deserializeKB) applying the passed adapter.
// Returns the workspace params, plus { imported, skipped, duplicate } counts.
//
// The workspace params stored by importKB are authoritative: the import is a
// full restore of that backup. If the target already has a workspace, only the
// *same* workspace identity may be overwritten (same salt => same key
// derivation); importing a different workspace over existing data is refused
// because it would silently orphan every already-stored document.
export async function importKB(adapter, kb) {
  if (!kb || kb.app !== APP_ID) throw new Error('broken .kb file');
  const ws = kb.workspace;
  if (!ws || !ws.salt || !ws.verifier) throw new Error('broken .kb file: missing workspace');

  const imported = [];
  const skipped = [];
  let duplicate = 0;

  const existing = await adapter.getParams();
  if (existing && existing.salt !== ws.salt) {
    throw new Error(
      'import refused: this vault belongs to a different workspace; restoring it would orphan existing documents'
    );
  }

  await adapter.saveParams(ws);
  const existingIds = new Set((await adapter.listDocs()).map((r) => r.id));
  // A doc deleted (tombstoned) after a backup was taken must NOT come back from
  // an old .kb restore: manual 3.3 promises even old backups cannot resurrect a
  // deleted doc. Tombstone ids are skipped below, exactly like ids already live.
  const tombstoneIds = new Set(
    typeof adapter.listTombstones === 'function'
      ? (await adapter.listTombstones()).map((t) => t.id)
      : []
  );
  if (Array.isArray(kb.documents)) {
    for (const d of kb.documents) {
      if (!d || !d.dekIv || !d.dekData || !d.iv || !d.data) {
        skipped.push(d && d.id ? d.id : '?');
        continue;
      }
      if (existingIds.has(d.id)) {
        duplicate++;
        continue;
      }
      if (tombstoneIds.has(d.id)) {
        skipped.push(d.id);
        continue;
      }
      await adapter.putDoc(d);
      imported.push(d.id);
    }
  }
  return { params: ws, imported, skipped, duplicate };
}
