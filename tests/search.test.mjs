// search.test.mjs — AC5: FTS hit over the local index.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath } from './helpers.mjs';

const PW = 'pw-for-search';

async function seeded() {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('d-apple', 'Apple Pie Recipe', [{ type: 'paragraph', text: 'uses granny smith apples and cinnamon' }]);
  await r.store.create('d-orange', 'Orange Zest', [{ type: 'paragraph', text: 'citrus, vitamin C, quite sunny' }]);
  await r.store.create('d-crypto', 'Encryption Notes', [
    { type: 'heading', text: 'AES' },
    { type: 'paragraph', text: 'talking about keys and ciphertext and gcm' },
  ]);
  return { b, r };
}

test('FT5 finds matching document by body word', async () => {
  const { r, b } = await seeded();
  const hits = await r.store.search('apples');
  assert.ok(hits.some((h) => h.id === 'd-apple'), 'keyword "apples" should match Apple Pie doc');
  assert.ok(!hits.some((h) => h.id === 'd-orange'), 'keyword should not match unrelated doc');
  b.close();
});

test('search matches ciphertext-ish keyword', async () => {
  const { r, b } = await seeded();
  const hits = await r.store.search('ciphertext');
  assert.ok(hits.some((h) => h.id === 'd-crypto'), 'should match crypto doc');
  b.close();
});

test('search for missing term returns no hits', async () => {
  const { r, b } = await seeded();
  const hits = await r.store.search('zanzibarbazooka');
  assert.equal(hits.length, 0);
  b.close();
});

test('search result honors edits (reindex on save)', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('d1', 'One Fish', [{ type: 'paragraph', text: 'underwater' }]);
  await r.store.edit('d1', 'Two Fish', [{ type: 'paragraph', text: 'red fish blue fish' }]);
  assert.ok((await r.store.search('blue')).some((h) => h.id === 'd1'));
  assert.equal((await r.store.search('underwater')).length, 0);
  b.close();
});
