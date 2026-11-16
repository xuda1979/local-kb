// store.js — document store with per-doc DEK wrapping and FTS index.
//
// This is the browser-facing persistence API. The concrete backend (IndexedDB
// in the browser, node:sqlite in tests) is passed in as `dbAdapter` implementing:
//
//   getParams()/saveParams(params)
//   listDocs()            -> [{id, ...}]
//   getDoc(id)            -> {id, dekIv, dekData, iv, data, createdAt, updatedAt} | null
//   putDoc(entry)
//   deleteDoc(id)
//   ftsPut(docId, text)   (cleartext index for search — in-memory per spec)
//   ftsDelete(docId)
//   ftsSearch(query)      -> [{id}]
//

import {
  importAesGcm,
  randomBytes,
  toBase64,
  encrypt,
  decrypt,
  utf8e,
  utf8d,
} from './crypto.js';
import { serializeDoc, deserializeDoc } from './docmodel.js';
import { nowISO } from './util.js';

export class DocStore {
  constructor(dbAdapter, workspaceKeyHandle) {
    this.db = dbAdapter;
    this.wsKey = workspaceKeyHandle;
  }

  // ---------- DEK wrap/unwrap (raw AES-256-GCM keys) ----------

  async newDEK() {
    // raw 32-byte DEK; keep CryptoKey handle in memory only
    const raw = randomBytes(32);
    const key = await importAesGcm(raw);
    return { raw, key };
  }

  // wrap a raw DEK with the workspace key (store in DB)
  async wrapDEK(rawDEK) {
    return encrypt(this.wsKey, rawDEK);
  }

  // unwrap a DEK from the DB (workspace key handle needed)
  async unwrapDEK(wrappedIv, wrappedData) {
    const raw = new Uint8Array(await decrypt(this.wsKey, wrappedIv, wrappedData));
    return importAesGcm(raw);
  }

  // ---------- document CRUD ----------

  async create(docId, title, blocks) {
    const doc = { title, blocks, createdAt: nowISO(), updatedAt: nowISO() };
    const { raw, key } = await this.newDEK();
    const wrapped = await this.wrapDEK(raw);
    const ciphertext = await encrypt(key, utf8e.encode(serializeDoc(doc)));
    const entry = {
      id: docId,
      dekIv: wrapped.iv,
      dekData: wrapped.data,
      iv: ciphertext.iv,
      data: ciphertext.data,
      createdAt: doc.createdAt,
      updatedAt: doc.updatedAt,
    };
    await this.db.putDoc(entry);
    await this.db.ftsPut(docId, this.plaintextForIndex(title, blocks));
    return { id: docId, ...doc };
  }

  async edit(docId, title, blocks) {
    const existing = await this.db.getDoc(docId);
    if (!existing) throw new Error('document not found: ' + docId);
    const dek = await this.unwrapDEK(existing.dekIv, existing.dekData);
    const doc = {
      title,
      blocks,
      createdAt: existing.createdAt || '',
      updatedAt: nowISO(),
    };
    const ciphertext = await encrypt(dek, utf8e.encode(serializeDoc(doc)));
    await this.db.putDoc({
      ...existing,
      iv: ciphertext.iv,
      data: ciphertext.data,
      createdAt: doc.createdAt,
      updatedAt: doc.updatedAt,
    });
    await this.db.ftsPut(docId, this.plaintextForIndex(title, blocks));
    return { id: docId, ...doc };
  }

  async delete(docId) {
    await this.db.deleteDoc(docId);
    await this.db.ftsDelete(docId);
  }

  async list() {
    const rows = await this.db.listDocs();
    const out = [];
    for (const r of rows) {
      const found = await this.db.getDoc(r.id);
      if (!found) continue;
      const decoded = await this.tryDecrypt(found);
      if (decoded) out.push({ id: r.id, ...decoded });
    }
    return out;
  }

  async get(id) {
    const found = await this.db.getDoc(id);
    if (!found) return null;
    const decoded = await this.tryDecrypt(found);
    return decoded ? { id, ...decoded } : null;
  }

  async tryDecrypt(entry) {
    try {
      const dek = await this.unwrapDEK(entry.dekIv, entry.dekData);
      const plaintext = utf8d.decode(await decrypt(dek, entry.iv, entry.data));
      return deserializeDoc(plaintext);
    } catch {
      return null;
    }
  }

  // ---------- FTS ----------

  plaintextForIndex(title, blocks) {
    return [title, ...(blocks || []).map((b) => b.text || '')].join(' ');
  }

  // Rebuild the in-memory FTS index from decrypted documents. Called on open:
  // the index is plaintext-only and never persisted, so it must be re-derived
  // every time the workspace is unlocked (zero-knowledge: RAM only).
  async reindexAll() {
    const docs = await this.list();
    for (const doc of docs) {
      await this.db.ftsPut(doc.id, this.plaintextForIndex(doc.title, doc.blocks));
    }
    return docs.length;
  }

  async search(query) {
    const hits = await this.db.ftsSearch(query);
    const out = [];
    for (const hit of hits) {
      try {
        const doc = await this.get(hit.id);
        if (doc) out.push({ id: doc.id, title: doc.title, updatedAt: doc.updatedAt });
      } catch {
        /* skip */
      }
    }
    return out;
  }
}
