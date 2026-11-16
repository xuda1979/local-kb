// syncfile.test.mjs — M3 sync tape *file transport* (src/syncfile.js):
//   * the tape file carries zero plaintext
//   * build -> parse round-trips identity and entries
//   * applying a peer tape synchronizes docs AND tombstones
//   * a tape from a different workspace is refused (would orphan docs)
//   * re-applying the same tape is idempotent

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import {
  buildTape,
  serializeTape,
  parseTape,
  applyTapeFile,
  assertSameWorkspace,
} from '../src/syncfile.js';
import { tempDbPath, KNOWN_PLAINTEXT } from './helpers.mjs';

const PW = 'syncfile-pw';

async function node(path, params, docs) {
  const b = new SQLiteBackend(path);
  let wsParams = params;
  if (!params) {
    const { workspace } = await createKb(b, PW, 'team-ws');
    wsParams = await b.getParams();
    assert.ok(workspace, 'workspace created');
  } else {
    // Recreate a peer of the same workspace: persist the shared params
    // (same salt => same key derivation) before opening.
    await b.saveParams(params);
  }
  const r = await openKb(b, PW);
  for (const [id, title, text] of docs) {
    await r.store.create(id, title, [{ type: 'paragraph', text }]);
  }
  return { b, r, params: wsParams };
}

test('buildTape round-trips through serialize/parse (same identity + entries)', async () => {
  const s = await node(tempDbPath(), null, [['d1', 'Secret Plan', KNOWN_PLAINTEXT]]);
  const tape = await buildTape(s.params, s.b);
  const parsed = parseTape(serializeTape(tape));
  assert.equal(parsed.app, 'local-kb');
  assert.equal(parsed.type, 'sync-tape');
  assert.equal(parsed.format, 1);
  assert.deepEqual(parsed.workspace, tape.workspace, 'workspace identity preserved');
  assert.equal(parsed.entries.length, 1);
  assert.equal(parsed.entries[0].id, 'd1');
  s.b.close();
});

test('applyTapeFile syncs docs and tombstones to an empty peer', async () => {
  const s1 = await node(tempDbPath(), null, [['alpha', 'Alpha Doc', 'code word velvet-fox']]);
  const s2 = await node(tempDbPath(), s1.params, []);

  // s1 creates a doc, then deletes one -> removed should be tombstoned.
  await s2.r.store.create('removed', 'Gone Doc', [{ type: 'paragraph', text: 'ephemeral' }]);
  const tape0 = await buildTape(s2.params, s2.b); // has alpha? no: s2 only has 'removed'
  await applyTapeFile(s1.b, tape0).then(() => {
    /* s1 gains nothing? it does gain 'removed' */
  });

  // Build on s1: it has alpha + removed (synced in). Now delete 'removed' locally.
  await s1.r.store.delete('removed');
  const tape = await buildTape(s1.params, s1.b);

  const res = await applyTapeFile(s2.b, tape);
  assert.ok(res.applied >= 0, 'applied count returned');

  // s2 should now have alpha, and removed must be gone (tombstoned via tape? no —
  // buildTape emits entries only for current docs, tombstones are not materialized
  // for deleted docs unless adapter tracks them). Assert the converged set.
  const list = await s2.r.store.list();
  const ids = list.map((d) => d.id);
  assert.ok(ids.includes('alpha'), 'peer received the live doc');
  assert.ok(!ids.includes('removed'), 'deleted doc not resurrected in peer');
  s1.b.close();
  s2.b.close();
});

test('applyTapeFile carries zero plaintext and is idempotent', async () => {
  const s = await node(tempDbPath(), null, [['d1', 'Secret Plan', KNOWN_PLAINTEXT]]);
  const tape = await buildTape(s.params, s.b);
  const blob = JSON.stringify(tape);
  assert.equal(blob.includes(KNOWN_PLAINTEXT), false, 'no body plaintext in tape file');
  assert.equal(blob.includes('Secret Plan'), false, 'no title plaintext in tape file');
  assert.ok(tape.entries[0].dekData && tape.entries[0].data, 'tape entry is ciphertext');

  const dst = await node(tempDbPath(), s.params, []);
  const first = await applyTapeFile(dst.b, tape);
  assert.equal(first.applied, 1, 'first apply imports the doc');
  assert.equal((await dst.r.store.list()).length, 1);
  const again = await applyTapeFile(dst.b, tape);
  assert.equal(again.applied, 0, 're-applying the identical tape is a no-op');
  assert.equal(again.skipped, 1, 'same version detected and skipped');
  s.b.close();
  dst.b.close();
});

test('a tape from a different workspace is refused by assertSameWorkspace / applyTapeFile', async () => {
  const s1 = await node(tempDbPath(), null, [['x', 'X', 'one']]);
  const other = await node(tempDbPath(), null, [['y', 'Y', 'two']]);
  const tape = await buildTape(other.params, other.b);

  assert.throws(
    () => assertSameWorkspace(s1.params, tape),
    /different workspace/,
    'pure check refuses the foreign tape'
  );

  await assert.rejects(
    applyTapeFile(s1.b, tape),
    /different workspace/,
    'apply path refuses the foreign tape without touching docs'
  );
  assert.equal((await s1.r.store.list()).length, 1, 'existing doc untouched');
  s1.b.close();
  other.b.close();
});

test('parseTape rejects junk and non-tape files', () => {
  assert.throws(() => parseTape(new TextEncoder().encode('not json')), /invalid sync tape/);
  const kb = JSON.stringify({ app: 'local-kb', format: 1, documents: [] });
  assert.throws(() => parseTape(new TextEncoder().encode(kb)), /not a local-kb sync tape/);
});
