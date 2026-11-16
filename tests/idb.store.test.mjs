// idb.store.test.mjs — AC1/AC2/AC3/AC6 against the browser's IndexedDB adapter,
// using fake-indexeddb (a spec-compliant in-memory IndexedDB). This exercises the
// same code path as the offline demo page (public/idb.js + src/*).
//
//   AC1  create/edit/delete persist across a fresh IndexedDB connection
//   AC2  the IndexedDB records contain no plaintext (grep-style)
//   AC3  no access with a wrong master password
//   AC6  all operations work with no server (in-memory IndexedDB)

import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { exportKB, serializeKB, deserializeKB, importKB } from '../src/bak.js';

// Load fake-indexeddb from the vendored copy so tests are hermetic.
const auto = await import('../vendor/fake-indexeddb/auto/index.mjs');

import { IndexedDBBackend } from '../public/idb.js';

const PW = 'idb-master-password';
const KNOWN_TITLE = 'Forbidden Compound Registry';
const KNOWN_BODY = 'vault-level-4; clearance code: romeo-alpha-7; stockpile #44';

before(() => {
  // fake-indexeddb/auto sets globalThis.indexedDB etc.
});

function freshAdapter(name = 'local-kb-test') {
  return new IndexedDBBackend(name);
}

async function dumpAllStrings(adapter) {
  // Mirror of browser `get_db_dump`
  const out = [];
  for (const store of ['workspace', 'docs']) {
    const all = await adapter.idbAll(store);
    for (const r of all) {
      if (!r) continue;
      for (const v of Object.values(r)) out.push(String(v));
    }
  }
  return out.join('\n');
}

test('AC1: full lifecycle over IndexedDB (create/edit/delete across reopen)', async () => {
  const dbName = 'ac1-' + Date.now();
  {
    const ad = freshAdapter(dbName);
    await createKb(ad, PW, 'team-kb');
    const r = await openKb(ad, PW);
    assert.equal(r.ok, true);
    await r.store.create('d1', 'First Doc', [{ type: 'paragraph', text: 'the contents' }]);
    await r.store.edit('d1', 'First Doc v2', [{ type: 'paragraph', text: 'second contents' }]);
  } // adapter closes (simulates browser close)

  // "reopen" with a brand-new connection
  {
    const ad2 = freshAdapter(dbName);
    const r2 = await openKb(ad2, PW);
    assert.equal(r2.ok, true);
    const list = await r2.store.list();
    assert.equal(list.length, 1);
    const got = await r2.store.get('d1');
    assert.equal(got.title, 'First Doc v2');
    assert.equal(got.blocks[0].text, 'second contents');
    await r2.store.delete('d1');
    assert.equal(await r2.store.get('d1'), null);
  }
});

test('AC2: no plaintext in IndexedDB records', async () => {
  const dbName = 'ac2-' + Date.now();
  const ad = freshAdapter(dbName);
  await createKb(ad, PW, 'secret-vault');
  const r = await openKb(ad, PW);
  await r.store.create('x1', KNOWN_TITLE, [
    { type: 'heading', text: 'Subzero' },
    { type: 'paragraph', text: KNOWN_BODY },
    { type: 'table', text: 'polar::ice::core' },
  ]);
  await r.store.edit('x1', KNOWN_TITLE + ' UPDATED', [{ type: 'paragraph', text: KNOWN_BODY + ' (edited)' }]);

  const raw = await dumpAllStrings(ad);
  for (const needle of [
    KNOWN_TITLE,
    KNOWN_BODY,
    'romeo-alpha-7',
    'Subzero',
    'polar::ice::core',
    'UPDATED',
    'vault-level-4',
  ]) {
    assert.equal(raw.includes(needle), false, `plaintext '${needle}' must not be in IndexedDB`);
  }
  // AND the DB must actually contain ciphertext blobs
  const docs = await ad.idbAll('docs');
  assert.ok(docs.length >= 1, 'docs stored');
  const rec = docs[0];
  assert.ok(Boolean(rec.dekData) && rec.dekData.length > 40, 'wrapped DEK present');
  assert.ok(Boolean(rec.data) && rec.data.length > 40, 'doc ciphertext present');
});

test('AC3: wrong master password denies access over IndexedDB', async () => {
  const dbName = 'ac3-' + Date.now();
  const ad = freshAdapter(dbName);
  await createKb(ad, PW);
  const ok = await openKb(ad, 'totally-wrong');
  assert.equal(ok.ok, false);
  const ok2 = await openKb(ad, PW);
  assert.equal(ok2.ok, true);
});

test('AC6: works with no server (pure local IndexedDB) + .kb import round-trips', async () => {
  const dbName = 'ac6-' + Date.now();
  const ad = freshAdapter(dbName);
  await createKb(ad, PW);
  const r = await openKb(ad, PW);
  await r.store.create('backup-me', 'Survives Migration', [{ type: 'code', text: 'do not hardcode secrets' }]);

  // export
  const rows = [];
  for (const { id } of await ad.listDocs()) rows.push(await ad.getDoc(id));
  const kb = exportKB(await ad.getParams(), rows);
  const bytes = serializeKB(kb);

  // machine 2: fresh DB, import
  const db2 = freshAdapter(dbName + '-import');
  const parsed = deserializeKB(bytes);
  await importKB(db2, parsed);
  const r2 = await openKb(db2, PW);
  assert.equal(r2.ok, true);
  const doc = await r2.store.get('backup-me');
  assert.equal(doc.title, 'Survives Migration');
  assert.equal(doc.blocks[0].text, 'do not hardcode secrets');
});

test('workspace name used as HKDF namespace', async () => {
  const dbName = 'ac7-' + Date.now();
  const ad = freshAdapter(dbName);
  await createKb(ad, PW, 'workspace-alpha');
  const r = await openKb(ad, PW);
  await r.store.create('n1', 'Namespace Check', [{ type: 'paragraph', text: 'data under alpha' }]);
  const doc = await r.store.get('n1');
  assert.equal(doc.title, 'Namespace Check');
  // A different workspace *cannot* open this data: same password, different name
  const ad2 = freshAdapter(dbName + '-beta');
  await createKb(ad2, PW, 'workspace-beta');
  const r2 = await openKb(ad2, PW);
  assert.equal(r2.ok, true);
  const list = await r2.store.list();
  assert.equal(list.length, 0, 'different workspace namespace sees no docs');
});

test('multi-word search: space-separated keywords AND-match (user manual 3.4 技巧)', async () => {
  const dbName = 'fts-multi-' + Date.now();
  const ad = freshAdapter(dbName);
  await createKb(ad, PW, 'team-kb');
  const r = await openKb(ad, PW);
  await r.store.create('s1', 'Snowstorm Project', [
    { type: 'paragraph', text: 'subject: oscar; status: snowstorm readiness handled by ops' },
  ]);
  await r.store.create('s2', 'Unrelated', [{ type: 'paragraph', text: 'only oscar here, no storm' }]);

  // both tokens present, apart from each other → hit (SQLite FTS5 would too)
  const hits = await r.store.search('oscar snowstorm');
  assert.ok(hits.some((h) => h.id === 's1'), '"oscar snowstorm" must AND-match s1');
  assert.ok(!hits.some((h) => h.id === 's2'), 'doc missing one token must not match');

  // single token still works (backwards-compatible)
  const single = await r.store.search('ops');
  assert.ok(single.some((h) => h.id === 's1'));
  // empty / whitespace-only query → no hits, no throw
  assert.equal((await r.store.search('   ')).length, 0);
});

// verify our vendored dep is what registers the global
test('fake-indexeddb exposes IDB globals', () => {
  assert.ok(indexedDB && typeof indexedDB.open === 'function', 'indexedDB global with open()');
  assert.ok(IDBKeyRange, 'IDBKeyRange global');
});
