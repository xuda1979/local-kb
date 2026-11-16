// workspace.js — workspace lifecycle: create, lock, unlock (password gate).
//
// A workspace is a set of parameters + verifier stored in cleartext,
// plus (in the DB adapters) a table of wrapped per-document DEKs.
// Nothing about document contents is stored in cleartext.

import {
  randomBytes,
  toBase64,
  fromBase64,
  deriveKeyBytes,
  deriveWorkspaceKey,
  importAesGcm,
  encrypt,
  decrypt,
  utf8e,
  utf8d,
} from './crypto.js';
import { stillStore } from './util.js';

export const VERIFIER_PLAINTEXT = 'local-kb::unlock-verifier::v1';

export const VERSION = 1;

export async function createWorkspace(masterPassword, wsName = 'default') {
  const salt = toBase64(randomBytes(16));
  const rawKey = new Uint8Array(await deriveKeyBytes(masterPassword, salt));
  const workspaceKey = new Uint8Array(await deriveWorkspaceKey(rawKey, wsName));
  const wsKeyHandle = await importAesGcm(workspaceKey);

  const proof = utf8e.encode(VERIFIER_PLAINTEXT);
  const { iv, data } = await encrypt(wsKeyHandle, proof);
  return {
    version: VERSION,
    name: wsName,
    salt,
    iterations: stillStore.iterations,
    verifier: { iv, data },
    // ephemeral binding this unlock creates; not persisted
    workspaceKey,
  };
}

// Try to unlock with a password. Returns { ok:true, workspace } or { ok:false }.
// The verifier check is a zero-knowledge gate: with the wrong password the
// derived key will fail to authenticate the verifier blob (and, later, DEKs).
export async function unlockWorkspace(params, masterPassword) {
  try {
    const rawKey = new Uint8Array(
      await deriveKeyBytes(masterPassword, params.salt, params.iterations)
    );
    const workspaceKey = new Uint8Array(await deriveWorkspaceKey(rawKey, params.name));
    const wsKeyHandle = await importAesGcm(workspaceKey);
    const proofBuf = await decrypt(wsKeyHandle, params.verifier.iv, params.verifier.data);
    const proof = utf8d.decode(proofBuf);
    if (proof !== VERIFIER_PLAINTEXT) return { ok: false, error: 'unlock-failed' };
    return { ok: true, workspace: { ...params, workspaceKey } };
  } catch {
    // decryption auth failure = wrong key
    return { ok: false, error: 'unlock-failed' };
  }
}

// Table of contents metadata held in memory after unlock (re-derived per load).
