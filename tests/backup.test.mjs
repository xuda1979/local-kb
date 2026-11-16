// backup.test.mjs — AC4: .kb export/import across "machines" (fresh sqlite file).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { exportKB, serializeKB, deserializeKB, importKB } from '../src/bak.js';
import { tempDbPath } from './helpers.mjs';

const PW = 'master-pw';

test('export -> import on a blank machine recovers all docs', async () => {
  const srcPath = tempDbPath();
  const b = new SQLiteBackend(srcPath);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('doc-a', 'Alpha', [{ type: 'paragraph', text: 'first' }]);
  await r.store.create('doc-b', 'Beta', [{ type: 'heading', text: 'second' }]);

  // build .kb payload
  const rows = [];
  for (const { id } of await b.listDocs()) rows.push(await b.getDoc(id));
  const kb = exportKB(await b.getParams(), rows);
  const bytes = serializeKB(kb);
  b.close();

  // "machine 2": brand new sqlite file, no workspace
  const dstPath = tempDbPath();
  const b2 = new SQLiteBackend(dstPath);
  const before = await openKb(b2, PW);
  assert.equal(before.ok, false, 'fresh machine must have no workspace yet');

  const parsed = deserializeKB(bytes);
  const imp = await importKB(b2, parsed);
  assert.ok(imp.params.salt, 'workspace params restored');
  assert.equal(imp.imported.length, 2, 'both docs imported');

  const r2 = await openKb(b2, PW);
  assert.equal(r2.ok, true, 'imported vault opens with the same master password');
  const docA = await r2.store.get('doc-a');
  assert.equal(docA.title, 'Alpha');
  assert.equal(docA.blocks[0].text, 'first');
  const docB = await r2.store.get('doc-b');
  assert.equal(docB.title, 'Beta');
  b2.close();
});

test('import rejects non-kb payloads', async () => {
  const b = new SQLiteBackend(':memory:');
  await assert.rejects(() => importKB(b, { app: 'other', documents: [] }));
  await assert.rejects(() => importKB(b, null));
});
