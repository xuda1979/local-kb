// import-safety.test.mjs — .kb import hardening:
//   * importing a backup from a DIFFERENT workspace over an existing vault is
//     refused (would otherwise orphan every stored document)
//   * re-importing the SAME backup is idempotent (no dupes)
//   * skipping malformed doc rows doesn't abort the import

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { exportKB, serializeKB, deserializeKB, importKB } from '../src/bak.js';
import { tempDbPath } from './helpers.mjs';

const PW = 'import-pw';

async function makeKb(path, wsName, docs) {
  const b = new SQLiteBackend(path);
  await createKb(b, PW, wsName);
  const r = await openKb(b, PW);
  for (const [id, title, text] of docs) {
    await r.store.create(id, title, [{ type: 'paragraph', text }]);
  }
  return { b, r };
}

test('import of a different workspace over existing vault is refused', async () => {
  const path = tempDbPath();
  const { b, r } = await makeKb(path, 'vault-blue', [['keep', 'Keep Me', 'original']]);
  // build a backup from a different workspace (different salt => different key)
  const other = tempDbPath();
  const src = await makeKb(other, 'vault-red', [['intruder', 'Intruder', 'from another vault']]);
  const rows = [];
  for (const { id } of await src.b.listDocs()) rows.push(await src.b.getDoc(id));
  const kb = exportKB(await src.b.getParams(), rows);
  const bytes = serializeKB(kb);
  src.b.close();

  await assert.rejects(
    () => importKB(b, deserializeKB(bytes)),
    /different workspace|orphan/,
    'cross-workspace import must throw'
  );

  // local data is untouched
  const after = await r.store.get('keep');
  assert.ok(after, 'existing doc still present after refused import');
  assert.equal(after.title, 'Keep Me');
  b.close();
});

test('re-importing the same backup is idempotent', async () => {
  const path = tempDbPath();
  const src = await makeKb(path, 'vault-same', [['d1', 'Same', 'dupe test']]);
  const rows = [];
  for (const { id } of await src.b.listDocs()) rows.push(await src.b.getDoc(id));
  const kb = exportKB(await src.b.getParams(), rows);
  const bytes = serializeKB(kb);
  src.b.close();

  const dstPath = tempDbPath();
  const b2 = new SQLiteBackend(dstPath);
  const parsed = deserializeKB(bytes);
  const first = await importKB(b2, parsed);
  assert.equal(first.imported.length, 1);
  const second = await importKB(b2, deserializeKB(bytes));
  assert.equal(second.imported.length, 0, 'second import must not duplicate docs');
  assert.equal(second.duplicate, 1, 'second import counts the existing doc as duplicate');
  b2.close();
});

test('malformed doc rows are skipped, valid rows still import', async () => {
  const b = new SQLiteBackend(':memory:');
  const kb = {
    app: 'local-kb',
    format: 1,
    exportde2At: new Date().toISOString(),
    workspace: {
      name: 'w',
      version: 1,
      salt: 'c2FsdA',
      iterations: '310000',
      verifier: { iv: 'I'.repeat(16), data: 'D'.repeat(32) },
    },
    documents: [
      { id: 'good', dekIv: 'a', dekData: 'b', iv: 'c', data: 'd', createdAt: '', updatedAt: '' },
      null,
      { id: 'broken', dekIv: '', dekData: '', iv: '', data: '' },
    ],
  };
  const res = await importKB(b, kb);
  assert.equal(res.imported.length, 1);
  assert.equal(res.skipped.length, 2, 'null + malformed row are skipped, not fatal');
  assert.equal(res.duplicate, 0);
  const rows = await b.listDocs();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, 'good');
  b.close();
});
