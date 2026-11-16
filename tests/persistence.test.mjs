// persistence.test.mjs — AC1: create/edit/delete survive a "reopen".

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath } from './helpers.mjs';

const PW = 's3cret-master-password';

test('create -> reopen -> read; edit -> reopen -> read; delete -> reopen -> gone', async () => {
  const path = tempDbPath();

  // session 0: create workspace
  let b = new SQLiteBackend(path);
  await createKb(b, PW);
  b.close();

  // session 1: create a doc
  b = new SQLiteBackend(path);
  let r = await openKb(b, PW);
  assert.equal(r.ok, true);
  const doc = await r.store.create('doc-1', 'My Travel Notes', [
    { type: 'paragraph', text: 'Paris in the springtime' },
  ]);
  assert.equal(doc.title, 'My Travel Notes');
  b.close();

  // session 2 (fresh): doc still there
  b = new SQLiteBackend(path);
  r = await openKb(b, PW);
  const got = await r.store.get('doc-1');
  assert.ok(got, 'doc survived a fresh open');
  assert.equal(got.title, 'My Travel Notes');
  assert.equal(got.blocks[0].text, 'Paris in the springtime');

  // edit
  await r.store.edit('doc-1', 'My Travel Notes v2', [
    { type: 'paragraph', text: 'Berlin in the autumn' },
  ]);
  b.close();

  // session 3 (fresh): edited value present
  b = new SQLiteBackend(path);
  r = await openKb(b, PW);
  const edited = await r.store.get('doc-1');
  assert.equal(edited.title, 'My Travel Notes v2');
  assert.equal(edited.blocks[0].text, 'Berlin in the autumn');

  // delete
  await r.store.delete('doc-1');
  b.close();

  // session 4 (fresh): gone
  b = new SQLiteBackend(path);
  r = await openKb(b, PW);
  const after = await r.store.get('doc-1');
  assert.equal(after, null);
  const list = await r.store.list();
  assert.equal(list.length, 0);
  b.close();
});
