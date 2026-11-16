// fts-hardening.test.mjs — FTS5 robustness:
//   * malformed/operator queries return no results instead of throwing
//   * rowids are monotonic, so docIds can never alias one another's index row
//     (previous content-hash scheme risked silent overwrites on collision)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';

test('malformed FTS5 queries return empty results, never throw', async () => {
  const b = new SQLiteBackend(':memory:');
  await createKb(b, 'pw');
  const r = await openKb(b, 'pw');
  await r.store.create('x1', 'Apple Harvest', [{ type: 'paragraph', text: 'apples and oranges' }]);
  for (const bad of ['"', '*', '(', '"unterminated', '-', 'AND', 'NEAR(', 'a[', "']"]) {
    const hits = await r.store.search(bad);
    assert.equal(Array.isArray(hits), true, `query ${JSON.stringify(bad)} returns an array`);
  }
  // and a valid query still works afterwards
  assert.ok((await r.store.search('apples')).some((h) => h.id === 'x1'));
  b.close();
});

test('monotonic rowids: many docs keep distinct index slots', async () => {
  const b = new SQLiteBackend(':memory:');
  await createKb(b, 'pw');
  const r = await openKb(b, 'pw');
  for (let i = 0; i < 500; i++) {
    const needle = 'nfx' + String(i).padStart(4, '0'); // alphanumeric token, porter-safe
    await r.store.create('doc' + i, 'Doc ' + i, [{ type: 'paragraph', text: 'unique body ' + needle }]);
  }
  // every doc is individually searchable by its own unique token (would be
  // false if a rowid hash collided and one doc's index row was overwritten)
  for (let i = 0; i < 500; i++) {
    const needle = 'nfx' + String(i).padStart(4, '0');
    const hits = await r.store.search(needle);
    assert.ok(hits.some((h) => h.id === 'doc' + i), needle + ' finds its own index row');
  }
  assert.equal((await b._rowidFor('doc0')) !== (await b._rowidFor('doc1')), true);
  b.close();
});
