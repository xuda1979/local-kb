// syncfile.js — plaintext-free sync tape file transport (M3, optional).
//
// Bridges the sync plane (src/sync.js) to a portable file, mirroring the .kb
// backup flow (src/bak.js): you "Sync out" a tape, carry the file to another
// machine, and "Sync in" there. The tape contains ONLY ciphertext — workspace
// params (cleartext, non-sensitive) plus one plaintext-free entry per document.
//
// A tape is *not* a backup: entries are LWW-merged per docId by (updatedAt, id)
// and tombstones are carried as deleted:true (so a delete on one node reaches
// its peers), whereas .kb import is a filtered restore.
//
// Format (JSON, base64url payloads) — .kbsync:
//
//   {
//     "app": "local-kb",
//     "type": "sync-tape",
//     "format": 1,
//     "createdAt": "...",
//     "workspace": { name, version, salt, iterations, verifier },  // identity only
//     "entries": [ { id, dekIv, dekData, iv, data, createdAt, updatedAt, deleted } ]
//   }
//
// The workspace object is included purely to bind the tape to a workspace
// identity (same salt => same key derivation). Its params are never written to
// the target store — applying a tape never changes the workspace parameters.

import { localTape, mergeTape, applyTape } from './sync.js';

const APP_ID = 'local-kb';
const TYPE = 'sync-tape';
const FORMAT = 1;

// Build a full tape object from the current doc set.
export async function buildTape(params, adapter) {
  return {
    app: APP_ID,
    type: TYPE,
    format: FORMAT,
    createdAt: new Date().toISOString(),
    workspace: {
      name: params.name,
      version: params.version,
      salt: params.salt,
      iterations: params.iterations,
      verifier: params.verifier,
    },
    entries: await localTape(adapter),
  };
}

export function serializeTape(tape) {
  return new TextEncoder().encode(JSON.stringify(tape, null, 2));
}

export function parseTape(buf) {
  let obj;
  try {
    obj = JSON.parse(new TextDecoder().decode(buf));
  } catch {
    throw new Error('invalid sync tape');
  }
  if (!obj || obj.app !== APP_ID || obj.type !== TYPE) {
    throw new Error('not a local-kb sync tape');
  }
  if (obj.format !== FORMAT) throw new Error('unsupported sync tape format');
  return obj;
}

// Refuse to apply a tape built from a different workspace: merging entries into
// a store whose params differ would silently orphan every existing document.
export function assertSameWorkspace(existing, tape) {
  const ws = tape && tape.workspace;
  if (!ws || !ws.salt || !ws.verifier) throw new Error('broken sync tape: missing workspace');
  if (existing && existing.salt !== ws.salt) {
    throw new Error(
      'sync refused: this tape belongs to a different workspace; applying it would orphan existing documents'
    );
  }
}

// Merge the tape's entries into the adapter (idempotent; LWW per docId), then
// report what changed. Returns { applied, skipped } exactly like applyTape.
export async function applyTapeFile(adapter, tape) {
  if (!Array.isArray(tape.entries)) throw new Error('broken sync tape: no entries');
  const existing = await adapter.getParams();
  assertSameWorkspace(existing, tape);
  return applyTape(tape.entries, adapter);
}
