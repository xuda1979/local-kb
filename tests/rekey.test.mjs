// rekey.test.mjs — master-password rotation (src/rekey.js):
//   * wrong current password is refused and mutates nothing
//   * a successful re-key re-wraps every DEK under the new workspace key
//   * after re-key the OLD password no longer unlocks; the NEW one does
//   * documents survive the rotation intact (ciphertext decrypts with new key)
//   * no plaintext leaks onto disk during/after the rotation
//   * refusing to operate on a workspace that does not exist yet
//   * re-key is idempotent / stable across a second rotation (old -> new -> new2)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { changeMasterPassword } from '../src/rekey.js';
import {
  tempDbPath,
  rmrf,
  KNOWN_PLAINTEXT,
  KNOWN_TITLE,
  WRONG_PASSWORD,
} from './helpers.mjs';

const PW = 'correct horse battery staple';
const NEW_PW = 'a brand new master password 7381';

// Known plaintext needles that must never appear on disk, keys included.
const NEEDLES = [
  KNOWN_PLAINTEXT,
  KNOWN_TITLE,
  'spelunking',
  PW,
  NEW_PW,
  WRONG_PASSWORD,
];

function assertNoPlaintextOnDisk(dbPath) {
  const blob = readFileSync(dbPath).toString('latin1');
  for (const needle of NEEDLES) {
    assert.equal(blob.includes(needle), false, `disk must not contain '${needle}'`);
  }
}

async function seededVault() {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  const { workspace, store } = await createKb(b, PW, 'memoir-ws');
  assert.ok(workspace, 'workspace created');
  await store.create('d1', KNOWN_TITLE, [
    { type: 'paragraph', text: KNOWN_PLAINTEXT },
    { type: 'paragraph', text: 'spelunking the deep caves' },
  ]);
  await store.create('d2', 'Second Note', [{ type: 'paragraph', text: 'auxiliary item' }]);
  return { path, b, store, params: await b.getParams() };
}

test('changeMasterPassword refuses a wrong current password and mutates nothing', async () => {
  const { path, b, params } = await seededVault();
  const before = await b.getParams();
  const res = await changeMasterPassword(b, WRONG_PASSWORD, NEW_PW);
  assert.deepEqual(res, { ok: false, error: 'wrong-master-password' });
  // Params untouched, docs untouched, key bytes unchanged.
  assert.deepEqual(await b.getParams(), before, 'params unchanged after refusal');
  const r = await openKb(b, PW);
  const list = await r.store.list();
  assert.equal(list.length, 2, 'docs still intact under the old password');
  assertNoPlaintextOnDisk(path);
  b.close();
  rmrf(path);
});

test('changeMasterPassword re-keys the vault: old pw denied, new pw opens docs', async () => {
  const { path, b } = await seededVault();
  const res = await changeMasterPassword(b, PW, NEW_PW);
  assert.equal(res.ok, true);
  assert.equal(res.rewrapped, 2, 'both DEKs re-wrapped under the new workspace key');

  // Old password refused at the verifier gate.
  const denied = await openKb(b, PW);
  assert.equal(denied.ok, false, 'old master password no longer unlocks');

  // New password opens the same docs, decrypts the same plaintext.
  const fresh = await openKb(b, NEW_PW);
  assert.equal(fresh.ok, true, 'new master password unlocks');
  const docs = await fresh.store.list();
  assert.equal(docs.length, 2, 'both documents intact after rotation');
  const d1 = docs.find((d) => d.id === 'd1');
  assert.equal(d1.title, KNOWN_TITLE);
  const body = d1.blocks.map((bl) => bl.text).join(' ');
  assert.ok(body.includes(KNOWN_PLAINTEXT), 'plaintext round-trips after re-key');

  // FTS still hits after reopening (index rebuilt from decrypted docs).
  const hits = await fresh.store.search('spelunk');
  assert.ok(hits.some((h) => h.id === 'd1'), 'search works after re-key');
  assertNoPlaintextOnDisk(path);
  b.close();
  rmrf(path);
});

test('re-key is stable across a second rotation and keeps old passwords inert', async () => {
  const { path, b } = await seededVault();
  await changeMasterPassword(b, PW, NEW_PW);

  // Second rotation: NEW_PW -> NEW_PW2.
  const NEW2 = 'third master password round 42';
  const again = await changeMasterPassword(b, NEW_PW, NEW2);
  assert.equal(again.ok, true);
  assert.equal(again.rewrapped, 2);

  assert.equal((await openKb(b, PW)).ok, false, 'original password still inert');
  assert.equal((await openKb(b, NEW_PW)).ok, false, 'previous rotated password inert');
  const current = await openKb(b, NEW2);
  assert.equal(current.ok, true, 'latest password unlocks');
  const docs = await current.store.list();
  assert.equal(docs.length, 2);
  assert.ok(docs.find((d) => d.id === 'd1').title === KNOWN_TITLE);
  assertNoPlaintextOnDisk(path);
  b.close();
  rmrf(path);
});

test('changeMasterPassword on an empty adapter is refused (no-workspace)', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  const res = await changeMasterPassword(b, PW, NEW_PW);
  assert.deepEqual(res, { ok: false, error: 'no-workspace' });
  b.close();
  rmrf(path);
});

test('re-key causes every stored wrapper to differ from the old key (new DEK wrappings)', async () => {
  const { path, b } = await seededVault();
  const before = (await b.getDoc('d1')).dekData;
  const res = await changeMasterPassword(b, PW, NEW_PW);
  assert.equal(res.ok, true);
  const after = (await b.getDoc('d1')).dekData;
  assert.notEqual(after, before, 'DEK re-wrapped under a different workspace key');
  const verifier = (await b.getParams()).verifier;
  assert.ok(verifier && verifier.iv && verifier.data, 'verifier rebuilt after re-key');
  assertNoPlaintextOnDisk(path);
  b.close();
  rmrf(path);
});
