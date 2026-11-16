// dek-wrap.test.mjs — per-document DEK wrapping semantics:
//   * every doc gets its own random DEK
//   * tampering with the verifier blocks unlock
//   * tampering with a wrapped DEK makes the doc unreadable

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath } from './helpers.mjs';

const PW = 'some-password';

test('each document has a distinct wrapped DEK', async () => {
  const b = new SQLiteBackend(':memory:');
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('a', 'Doc A', [{ type: 'paragraph', text: 'aaa' }]);
  await r.store.create('b', 'Doc B', [{ type: 'paragraph', text: 'bbb' }]);
  const da = await b.getDoc('a');
  const db_ = await b.getDoc('b');
  assert.notEqual(da.dekIv, db_.dekIv, 'fresh random IV per DEK wrap');
  assert.notEqual(da.dekData, db_.dekData, 'distinct wrapped DEKs');
});

test('tampering with the stored verifier blocks unlock', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  // tamper: flip a char in the verifier blob
  const params = await b.getParams();
  const ver = params.verifier; // already parsed object
  ver.data = (ver.data[0] === 'A' ? 'B' : 'A') + ver.data.slice(1);
  await b.saveParams({ ...params, verifier: ver });

  const r = await openKb(b, PW);
  assert.equal(r.ok, false, 'tampered verifier must not unlock');
});

test('tampering with a wrapped DEK makes the document unreadable', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('d1', 'Doc One', [{ type: 'paragraph', text: 'secret text' }]);

  const doc = await b.getDoc('d1');
  // corrupt the wrapped DEK (not the doc ciphertext)
  await b.putDoc({ ...doc, dekData: (doc.dekData[0] === 'A' ? 'B' : 'A') + doc.dekData.slice(1) });

  const got = await r.store.get('d1');
  assert.equal(got, null, 'corrupted DEK must not yield a readable doc');
});

test('unlock fails on a non-workspace file', async () => {
  const b = new SQLiteBackend(':memory:');
  // blank DB, no workspace row
  const r = await openKb(b, PW);
  assert.equal(r.ok, false);
  assert.equal(r.error, 'no-workspace');
});
