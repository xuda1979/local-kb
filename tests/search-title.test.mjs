// search-title.test.mjs — title-text matching through FTS, and list ordering by updatedAt.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath } from './helpers.mjs';

test('title words are searchable', async () => {
  const b = new SQLiteBackend(':memory:');
  await createKb(b, 'pw');
  const r = await openKb(b, 'pw');
  await r.store.create('t1', 'Quarterly Roadmap Review', [{ type: 'paragraph', text: 'planning discussion' }]);
  const hits = await r.store.search('roadmap');
  assert.equal(hits.some((h) => h.id === 't1'), true);
});

test('FTS index is rebuilt on unlock (reopen searchable)', async () => {
  const path = tempDbPath();
  let b = new SQLiteBackend(path);
  await createKb(b, 'pw');
  let r = await openKb(b, 'pw');
  await r.store.create('t1', 'Roadmap Archive', [{ type: 'paragraph', text: 'secret launch plan' }]);
  b.close();

  // fresh backend = fresh in-memory FTS index; search must still hit (rebuild)
  b = new SQLiteBackend(path);
  r = await openKb(b, 'pw');
  assert.equal(r.ok, true);
  const hits = await r.store.search('launch');
  assert.equal(hits.some((h) => h.id === 't1'), true, 'search works after reopen');
  b.close();
});

test('idb adapter rebuilds FTS on unlock', async () => {
  const auto = await import('../vendor/fake-indexeddb/auto/index.mjs');
  const { IndexedDBBackend } = await import('../public/idb.js');
  const dbName = 'fts-rebuild-' + Date.now();
  const ad = new IndexedDBBackend(dbName);
  await createKb(ad, 'pw', 'ws');
  let r = await openKb(ad, 'pw');
  await r.store.create('q1', 'Quarterly Goals', [{ type: 'paragraph', text: 'double revenue by q4' }]);

  // simulate a fresh browser session on the same persisted store
  const ad2 = new IndexedDBBackend(dbName);
  r = await openKb(ad2, 'pw');
  const hits = await r.store.search('revenue');
  assert.equal(hits.some((h) => h.id === 'q1'), true, 'idb search works after reopen');
});
