// crypto.js — zero-knowledge primitives. Uses WebCrypto (no native deps).
// All plaintext stays in memory; everything that touches disk is ciphertext.
//
// Key hierarchy (1Password-style):
//   masterPassword --PBKDF2+HMAC--> unlockKey --HKDF--> workspaceKey
//   workspaceKey  --WRAP(AES-256-GCM)--> per-document DEK (stored in DB)
//   DEK           --ENCRYPT(AES-256-GCM)--> document plaintext -> ciphertext
//
// Unlock proof: salt+verifierBlob (random 32 bytes) encrypted with unlockKey,
// stored in cleartext]. Password wrong => authTag mismatch => open() rejects.
//
// Everything is stored as URL-safe base64 strings (IndexedDB + SQLite + .kb
// friendly). Binary-safe.

export const PBKDF2_ITERATIONS = 310000;

function getCrypto() {
  return globalThis.crypto;
}

export function randomBytes(n) {
  const c = getCrypto();
  const buf = new Uint8Array(n);
  c.getRandomValues(buf);
  return buf;
}

// URL-safe base64 (no padding). btoa/atob are globals in browsers and Node >= 16.
export function toBase64(buf) {
  let binary = '';
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < u8.length; i++) binary += String.fromCharCode(u8[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function fromBase64(s) {
  const std = s.replace(/-/g, '+').replace(/_/g, '/');
  const padded = std + '='.repeat((4 - (std.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

// ---------- password -> key ----------

export async function deriveUnlockKey(password, saltB64, iterations = PBKDF2_ITERATIONS, hash = 'SHA-256') {
  const keyMaterial = await getCrypto().subtle.importKey(
    'raw',
    new TextEncoder().encode(password),
    'PBKDF2',
    false,
    ['deriveKey']
  );
  const salt = fromBase64(saltB64);
  return getCrypto().subtle.deriveKey(
    {
      name: 'PBKDF2',
      salt,
      iterations,
      hash,
    },
    keyMaterial,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt', 'wrapKey', 'unwrapKey']
  );
}

// Derive a stable, reusable AES key for wrapping DEKs and DEK-node CHECK hooks.
// We cannot export the AES key itself (subtle returns a handle), so we derive
// this via HKDF from the raw pbkdf2 bytes + per-purpose info.
export async function deriveKeyBytes(
  password,
  saltB64,
  iterations = PBKDF2_ITERATIONS,
  hash = 'SHA-256'
) {
  const keyMaterial = await getCrypto().subtle.importKey(
    'raw',
    new TextEncoder().encode(password),
    'PBKDF2',
    false,
    ['deriveKey', 'deriveBits']
  );
  const salt = fromBase64(saltB64);
  const bits = await getCrypto().subtle.deriveBits(
    { name: 'PBKDF2', salt, iterations, hash },
    keyMaterial,
    256
  );
  return new Uint8Array(bits);
}

export async function hkdfExpand(masterKeyBytes, info, length = 32) {
  const key = await getCrypto().subtle.importKey(
    'raw',
    masterKeyBytes,
    'HKDF',
    false,
    ['deriveBits']
  );
  const bits = await getCrypto().subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: new TextEncoder().encode(info) },
    key,
    length * 8
  );
  return new Uint8Array(bits);
}

// WorkspaceKey: a raw 32-byte AES-256 key derived from master password only.
export async function deriveWorkspaceKey(rawKeyBytes, wsName) {
  return hkdfExpand(rawKeyBytes, 'local-kb/ws:' + wsName, 32);
}

// turn a raw 32 byte key into an AES-GCM CryptoKey handle
export async function importAesGcm(rawKeyBytes) {
  return getCrypto().subtle.importKey(
    'raw',
    rawKeyBytes,
    { name: 'AES-GCM' },
    false,
    ['encrypt', 'decrypt', 'wrapKey', 'unwrapKey']
  );
}

// ---------- AEAD helpers ----------

export async function encrypt(key, plaintextBytes) {
  const iv = randomBytes(12);
  const ct = new Uint8Array(
    await getCrypto().subtle.encrypt({ name: 'AES-GCM', iv }, key, plaintextBytes)
  );
  return { iv: toBase64(iv), data: toBase64(ct) };
}

export async function decrypt(key, ivB64, dataB64) {
  const pt = await getCrypto().subtle.decrypt(
    { name: 'AES-GCM', iv: fromBase64(ivB64) },
    key,
    fromBase64(dataB64)
  );
  return new Uint8Array(pt);
}

export const utf8e = new TextEncoder();
export const utf8d = new TextDecoder();

// ---------- Node-only helpers (kept out of the browser bundle) ----------
// eslint-disable-next-line no-undef
export const isNode =
  typeof process !== 'undefined' && !!process && !!process.versions && !!process.versions.node;
