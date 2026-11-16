// crypto.test.mjs — cryptology primitives.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  deriveKeyBytes,
  deriveWorkspaceKey,
  importAesGcm,
  encrypt,
  decrypt,
  toBase64,
  fromBase64,
  utf8e,
  utf8d,
} from '../src/crypto.js';

test('base64url round trip preserves bytes', () => {
  for (const n of [1, 3, 16, 32, 33, 64, 129, 1024]) {
    const buf = new Uint8Array(n);
    for (let i = 0; i < n; i++) buf[i] = i % 256;
    const b64 = toBase64(buf);
    const back = fromBase64(b64);
    assert.deepEqual(back, buf);
  }
});

test('keys derive deterministically from same password+salt', async () => {
  const a = new Uint8Array(await deriveKeyBytes('hunter2', 'c2FsdA'));
  const b = new Uint8Array(await deriveKeyBytes('hunter2', 'c2FsdA'));
  assert.deepEqual(a, b);
});

test('keys differ with different password', async () => {
  const a = new Uint8Array(await deriveKeyBytes('hunter2', 'c2FsdA'));
  const b = new Uint8Array(await deriveKeyBytes('hunter3', 'c2FsdA'));
  assert.notDeepEqual(a, b);
});

test('workspace key derivation is namespaced', async () => {
  const a = new Uint8Array(await deriveWorkspaceKey(await deriveKeyBytes('pw', 'c2FsdA'), 'team1'));
  const b = new Uint8Array(await deriveWorkspaceKey(await deriveKeyBytes('pw', 'c2FsdA'), 'team2'));
  assert.notDeepEqual(a, b);
});

test('AES-256-GCM encrypt/decrypt round trip with tamper detection', async () => {
  const key = await importAesGcm(new Uint8Array(await deriveKeyBytes('pw', 'c2FsdA')));
  const msg = utf8e.encode('attack at dawn');
  const { iv, data } = await encrypt(key, msg);
  assert.equal(data.length >= msg.length + 16, true, 'ciphertext includes GCM tag');
  const plain = utf8d.decode(await decrypt(key, iv, data));
  assert.equal(plain, 'attack at dawn');

  // tamper => throws
  await assert.rejects(() =>
    decrypt(key, iv, 'AAA' + data.slice(3))
  );
});

test('random IV means same plaintext encrypts differently', async () => {
  const key = await importAesGcm(new Uint8Array(await deriveKeyBytes('pw', 'c2FsdA')));
  const a = await encrypt(key, utf8e.encode('same'));
  const b = await encrypt(key, utf8e.encode('same'));
  assert.notEqual(a.iv, b.iv);
  assert.notEqual(a.data, b.data);
});
