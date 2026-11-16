// idb.js — IndexedDB adapter for the browser (matches the DocStore adapter
// interface). FTS lives in memory (per spec, plaintext stays in RAM).

'use strict';

export function openIndexedDB(name = 'local-kb', version = 2) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(name, version);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('workspace')) db.createObjectStore('workspace', { keyPath: 'id' });
      if (!db.objectStoreNames.contains('docs')) db.createObjectStore('docs', { keyPath: 'id' });
      if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', { keyPath: 'key' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export class IndexedDBBackend {
  constructor(name = 'local-kb') {
    this.name = name;
    this.db = null;
    this.fts = new Map();
  }

  async ready() {
    if (!this.db) this.db = await openIndexedDB(this.name);
    return this.db;
  }

  idbGet(store, key) {
    return new Promise(async (resolve, reject) => {
      const db = await this.ready();
      const tr = db.transaction(store, 'readonly');
      const req = tr.objectStore(store).get(key);
      req.onsuccess = () => resolve(req.result ?? null);
      req.onerror = () => reject(req.error);
    });
  }

  idbPut(store, value) {
    return new Promise(async (resolve, reject) => {
      const db = await this.ready();
      const tr = db.transaction(store, 'readwrite');
      const req = tr.objectStore(store).put(value);
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  idbDelete(store, key) {
    return new Promise(async (resolve, reject) => {
      const db = await this.ready();
      const tr = db.transaction(store, 'readwrite');
      const req = tr.objectStore(store).delete(key);
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  idbAll(store) {
    return new Promise(async (resolve, reject) => {
      const db = await this.ready();
      const tr = db.transaction(store, 'readonly');
      const req = tr.objectStore(store).getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  // ---------- workspace ----------

  async getParams() {
    return this.idbGet('workspace', 1);
  }
  async saveParams(params) {
    await this.idbPut('workspace', { id: 1, ...params });
  }
  async deleteParams() {
    await this.idbDelete('workspace', 1);
  }

  // ---------- app metadata ----------

  async getMeta(key) {
    const v = await this.idbGet('meta', key);
    return v ? v.value : null;
  }
  async setMeta(key, value) {
    await this.idbPut('meta', { key, value });
  }

  // ---------- docs ----------

  async listDocs() {
    const all = await this.idbAll('docs');
    return all.map((d) => ({ id: d.id }));
  }
  async getDoc(id) {
    const d = await this.idbGet('docs', id);
    if (!d) return null;
    return {
      id: d.id,
      dekIv: d.dekIv,
      dekData: d.dekData,
      iv: d.iv,
      data: d.data,
      createdAt: d.createdAt,
      updatedAt: d.updatedAt,
    };
  }
  async putDoc(entry) {
    await this.idbPut('docs', entry);
  }
  async deleteDoc(id) {
    await this.idbDelete('docs', id);
  }

  // Record a tombstone (non-secret updatedAt) so deletes propagate to peers
  // via the sync tape (metadata store, same as the SQLite adapter).
  async markDeleted(id, updatedAt) {
    await this.idbPut('meta', { key: 'tombstone:' + id, value: updatedAt });
  }
  async delMeta(key) {
    await this.idbDelete('meta', key);
  }
  async listTombstones() {
    const all = await this.idbAll('meta');
    const out = [];
    for (const r of all || []) {
      if (r.key && r.key.startsWith('tombstone:')) out.push({ id: r.key.slice(10), updatedAt: r.value });
    }
    return out;
  }

  // ---------- FTS (in-memory) ----------

  async ftsPut(docId, text) {
    this.fts.set(docId, text);
  }
  async ftsDelete(docId) {
    this.fts.delete(docId);
  }
  // 用户手册 3.4 技巧: "关键词可连续用空格分隔" — a space-separated query is
  // an AND of keyword tokens, matching the SQLite/FTS5 backend's implicit AND
  // for bare terms. Matching is per-token substring, so words can be separated
  // by any other content (the whole-string substring match could never hit).
  async ftsSearch(query) {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return [];
    const out = [];
    for (const [id, text] of this.fts) {
      const hay = text.toLowerCase();
      if (tokens.every((t) => hay.includes(t))) out.push({ id });
    }
    return out;
  }
}
