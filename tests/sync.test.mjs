// sync.test.mjs — P3/M3 ciphertext sync plane:
//   * tapes carry zero plaintext
//   * exchanging tapes between two stores converges both (runs a real
//     reindex, so the FTS index is rebuilt from the converged docs)
//   * applyTape is idempotent
//   * the merge rule is LWW per docId, deterministic given full information

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { mergeTape, applyTape, localTape } from '../src/sync.js';
import { tempDbPath, KNOWN_PLAINTEXT } from './helpers.mjs';

const PW = 'sync-pw';

// Create a node that shares the SAME workspace as any other node created from
// the same seed (real sync model: all peers derive keys from the identical
// workspace params, e.g. via the same .kb / salt).
async function openSeed(path, docs, params) {
  const b = new SQLiteBackend(path);
  if (params) {
    await b.saveParams(params); // same workspace identity
  } else {
    await createKb(b, PW, 'team-ws');
    params = await b.getParams();
  }
  const r = await openKb(b, PW);
  for (const [id, title, text] of docs) {
    await r.store.create(id, title, [{ type: 'paragraph', text }]);
  }
  return { b, r, params };
}

test('localTape carries only ciphertext (zero-knowledge), applyTape idempotent', async () => {
  const path = tempDbPath();
  const s1 = await openSeed(path, [['d1', 'Secret Plan', KNOWN_PLAINTEXT]]);
  const tape = await localTape(s1.b);
  assert.equal(tape.length, 1);
  const blob = JSON.stringify(tape);
  assert.equal(blob.includes(KNOWN_PLAINTEXT), false, 'no plaintext in the tape');
  assert.equal(blob.includes('Secret Plan'), false, 'no title plaintext in the tape');
  assert.ok(tape[0].dekData && tape[0].data, 'tape entry carries wrapped DEK + ciphertext');

  // peer shares the same workspace but starts empty
  const dst = tempDbPath();
  const s2 = await openSeed(dst, [], s1.params);
  // first apply writes the doc...
  const first = await applyTape(tape, s2.b);
  assert.equal(first.applied, 1, 'first apply imports the doc');
  const list = await s2.r.store.list();
  assert.equal(list.length, 1, 'doc present after first apply');
  // ...second apply of the identical tape is a no-op
  const again = await applyTape(tape, s2.b);
  assert.equal(again.applied, 0, 're-applying the same tape changes nothing');
  assert.equal(again.skipped, 1, 'same version detected and skipped');
  assert.equal((await s2.r.store.list()).length, 1, 'no duplicate rows created');
  s1.b.close();
  s2.b.close();
});

test('two stores converge after exchanging tapes (with in-memory FTS rebuild)', async () => {
  const p1 = tempDbPath();
  const p2 = tempDbPath();
  const s1 = await openSeed(p1, [['a', 'Doc A', 'alpha body']]);
  const s2 = await openSeed(p2, [['b', 'Doc B', 'beta body']], s1.params);

  // sync A -> B, then B -> A (both now have the full set)
  await applyTape(await localTape(s1.b), s2.b);
  await applyTape(await localTape(s2.b), s1.b);

  for (const [label, s, p] of [['store1', s1, p1], ['store2', s2, p2]]) {
    // reopen fresh: reindex runs from the merged plaintext
    s.b.close();
    const b = new SQLiteBackend(p);
    const r = await openKb(b, PW);
    assert.equal(r.ok, true, label + ' opens');
    const docs = await r.store.list();
    assert.deepEqual(docs.map((d) => d.id).sort(), ['a', 'b'], label + ' has both docs');
    const a = await r.store.get('a');
    assert.equal(a.blocks[0].text, 'alpha body', label + ' doc a readable');
    // the merged state was reindexed: search works on either node
    assert.ok((await r.store.search('beta')).some((h) => h.id === 'b'), label + ' searches merged doc');
    b.close();
  }
});

test('LWW merge converges deterministically for concurrent writes', async () => {
  // same doc edited on two nodes with distinct timestamps
  const mk = (updatedAt, id, tag) => ({
    id: 'shared',
    updatedAt,
    dekIv: 'iv-' + tag,
    dekData: 'dek-' + tag,
    iv: 'ivc-' + tag,
    data: 'data-' + tag + '-' + new Date(updatedAt).toISOString(),
    createdAt: '',
    deleted: false,
  });
  const older = mk('2026-01-01T00:00:00.000Z', 'w1', 'O');
  const newer = mk('2026-01-02T00:00:00.000Z', 'w2', 'N');
  // both orders converge to the same single winner
  const ab = mergeTape([older], [newer]);
  const ba = mergeTape([newer], [older]);
  assert.equal(ab.length, 1);
  assert.equal(ab[0].data, ba[0].data, 'order-independent winner');
  assert.equal(ab[0].updatedAt, '2026-01-02T00:00:00.000Z', 'newest wins');
});

test('mergeTape ignores malformed entries', async () => {
  const good = { id: 'x', updatedAt: '2026-02-01T00:00:00.000Z', iv: 'i', data: 'd' };
  const merged = mergeTape([null, undefined, { updatedAt: '2026-02-02T00:00:00.000Z' }, good], []);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, 'x');
});
