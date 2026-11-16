// workspace.test.mjs — create / unlock / wrong password.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath } from './helpers.mjs';

test('create workspace then unlock with correct password', async () => {
  const path = tempDbPath();
  const backend = new SQLiteBackend(path);
  await createKb(backend, 'correct horse battery staple');
  // fresh backend simulates a reopened app (new process)
  const backend2 = new SQLiteBackend(path);
  const res = await openKb(backend2, 'correct horse battery staple');
  assert.equal(res.ok, true);
  assert.ok(res.store, 'store configured');
  backend2.close();
  backend.close();
});

test('open fails with wrong password', async () => {
  const path = tempDbPath();
  const backend = new SQLiteBackend(path);
  await createKb(backend, 'right-password');
  const backend2 = new SQLiteBackend(path);
  const res = await openKb(backend2, 'wrong-password');
  assert.equal(res.ok, false);
  backend2.close();
});

test('open fails when workspace does not exist', async () => {
  const backend = new SQLiteBackend(':memory:');
  const res = await openKb(backend, 'pw');
  assert.equal(res.ok, false);
  assert.equal(res.error, 'no-workspace');
});
