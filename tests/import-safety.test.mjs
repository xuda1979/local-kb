// import-safety.test.mjs — .kb import hardening:
//   * importing a backup from a DIFFERENT workspace over an existing vault is
//     refused (would otherwise orphan every stored document)
//   * re-importing the SAME backup is idempotent (no dupes)
//   * skipping malformed doc rows doesn't abort the import
//   * importing an OLD backup cannot resurrect a doc deleted after the export
//     (user manual 3.3: tombstones outlive backups)

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

test('old .kb backup cannot resurrect a doc deleted after the export', async () => {
  // Single vault flow: the vault records the tombstone when 'gone' is deleted,
  // and importing an OLD backup (captured while the doc was alive) into the
  // same vault must respect that tombstone — no resurrection (user manual 3.3).
  const path = tempDbPath();
  const { b, r } = await makeKb(path, 'vault-tomb', [['gone', 'Gone Doc', 'doomed body']]);
  // export the backup while the doc is still alive
  const rows = [];
  for (const { id } of await b.listDocs()) rows.push(await b.getDoc(id));
  const kb = exportKB(await b.getParams(), rows);
  const bytes = serializeKB(kb);
  // then delete the doc on the same store (records a tombstone)
  await r.store.delete('gone');
  assert.equal((await r.store.list()).length, 0, 'doc deleted locally');
  assert.ok((await b.listTombstones()).some((t) => t.id === 'gone'), 'tombstone recorded');

  // importing the OLD backup must respect the tombstone: no resurrection
  const res = await importKB(b, deserializeKB(bytes));
  assert.ok(!res.imported.includes('gone'), 'tombstoned doc must not be re-imported');
  const remaining = await b.listDocs();
  assert.ok(!remaining.some((r) => r.id === 'gone'), 'doc stays gone after old-backup import');
  b.close();
});
