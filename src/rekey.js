// rekey.js — change the master password (re-key) without losing the vault.
//
// The workspace key is derived from the master password only (never stored);
// every per-document DEK is wrapped by it along with the unlock verifier. To
// rotate the password we therefore:
//
//   1. unlock with the current password (wrong password => refused, no mutation)
//   2. re-derive the workspace key from the NEW password using the SAME salt +
//      iterations — the salt is the public workspace identity (the .kbsync
//      gate), so re-keying keeps peers/backups recognizable
//   3. re-wrap every stored DEK under the new workspace key
//   4. re-derive the verifier under the new key and save the params
//
// The params are updated LAST: if a re-wrap were interrupted, the old password
// would still authenticate every unfinished DEK, so the vault is never locked
// out half-way through.

import {
  deriveKeyBytes,
  deriveWorkspaceKey,
  importAesGcm,
  encrypt,
  decrypt,
  utf8e,
} from './crypto.js';
import { unlockWorkspace, VERIFIER_PLAINTEXT } from './workspace.js';

export async function changeMasterPassword(adapter, currentPassword, newPassword) {
  const params = await adapter.getParams();
  if (!params) return { ok: false, error: 'no-workspace' };

  const existing = await unlockWorkspace(params, currentPassword);
  if (!existing.ok) return { ok: false, error: 'wrong-master-password' };
  const oldWsHandle = await importAesGcm(existing.workspace.workspaceKey);

  const rawKey = new Uint8Array(
    await deriveKeyBytes(newPassword, params.salt, params.iterations)
  );
  const newWsKey = new Uint8Array(await deriveWorkspaceKey(rawKey, params.name));
  const newWsHandle = await importAesGcm(newWsKey);

  // Re-wrap every DEK. A doc whose DEK cannot be unwrapped with the old key
  // (already re-keyed, or corrupt) is skipped rather than aborting the rotation.
  let rewrapped = 0;
  for (const { id } of await adapter.listDocs()) {
    const doc = await adapter.getDoc(id);
    if (!doc) continue;
    try {
      const dekRaw = new Uint8Array(await decrypt(oldWsHandle, doc.dekIv, doc.dekData));
      const wrapped = await encrypt(newWsHandle, dekRaw);
      await adapter.putDoc({ ...doc, dekIv: wrapped.iv, dekData: wrapped.data });
      rewrapped++;
    } catch {
      /* keep the existing wrapping; the old key no longer exists after the final
         saveParams, so such a doc is unreadable — same as any corrupt entry */
    }
  }

  // New verifier under the new workspace key; save params only now. With this
  // write the old password stops working and the new one takes over.
  const { iv, data } = await encrypt(newWsHandle, utf8e.encode(VERIFIER_PLAINTEXT));
  await adapter.saveParams({
    name: params.name,
    version: params.version,
    salt: params.salt,
    iterations: params.iterations,
    verifier: { iv, data },
  });

  return { ok: true, rewrapped, workspaceKey: newWsKey };
}
