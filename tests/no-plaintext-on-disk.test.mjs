// no-plaintext-on-disk.test.mjs — AC2 (negative testing).
// After writing documents, grep the SQLite file byte-for-byte for the known
// plaintext (title, body, block text, master password, app marker). None may
// appear — everything stored is ciphertext only.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createKb, openKb } from '../src/factory.js';
import { SQLiteBackend } from '../src/db.js';
import { tempDbPath, KNOWN_PLAINTEXT, KNOWN_TITLE } from './helpers.mjs';

const PW = 'master-pw-123';

test('no plaintext anywhere in the SQLite file', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('d1', KNOWN_TITLE, [
    { type: 'heading', text: 'Top Secret Section' },
    { type: 'paragraph', text: KNOWN_PLAINTEXT },
    { type: 'code', text: 'var key = "shhh";' },
    { type: 'table', text: 'alpine::mount::delta' },
  ]);
  await r.store.edit('d1', KNOWN_TITLE + ' (updated)', [
    { type: 'paragraph', text: 'second version ' + KNOWN_PLAINTEXT },
  ]);
  b.close();

  const dbBytes = readFileSync(path);
  const raw = dbBytes.toString('latin1'); // byte-accurate (no encoding loss)

  // The verifier itself is encrypted too; but the salt is stored. Salt isn't plaintext.
  const needles = [
    KNOWN_PLAINTEXT,
    KNOWN_TITLE,
    'Top Secret Section',
    'var key = "shhh";',
    'alpine::mount::delta',
    'second version',
    'master-pw-123',
    'super-secret',
    'canyon-hideout',
    'Alladin', // any plausible cleartext remnant — will not appear
  ];
  for (const n of needles) {
    assert.equal(
      raw.includes(n),
      false,
      `plaintext string "${n}" must NOT appear in the DB file`
    );
  }
});

test('doc title/body not discoverable from the .kb export string', async () => {
  const path = tempDbPath();
  const b = new SQLiteBackend(path);
  await createKb(b, PW);
  const r = await openKb(b, PW);
  await r.store.create('x1', 'Oppression Studies Manual', [
    { type: 'paragraph', text: 'Cryo-Freeze Chamber access codes: 8-9-2' },
  ]);
  // assemble an export like bak.exportKB
  const rows = [];
  for (const { id } of await b.listDocs()) {
    rows.push(await b.getDoc(id));
  }
  const { exportKB, serializeKB } = await import('../src/bak.js');
  const json = new TextDecoder().decode(serializeKB(exportKB(await b.getParams(), rows)));
  assert.equal(json.includes('Oppression Studies Manual'), false);
  assert.equal(json.includes('8-9-2'), false);
  b.close();
});
