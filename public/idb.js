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

  // ---------- FTS (in-memory) ----------

  async ftsPut(docId, text) {
    this.fts.set(docId, text);
  }
  async ftsDelete(docId) {
    this.fts.delete(docId);
  }
  async ftsSearch(query) {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const [id, text] of this.fts) {
      if (text.toLowerCase().includes(q)) out.push({ id });
    }
    return out;
  }
}
