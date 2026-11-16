// db.js — SQLite backend via node:sqlite. Implements the adapter interface used
// by DocStore.
//
//   docs/workspace tables  -> on-disk SQLite file (ciphertext only)
//   fts5 index             -> separate in-memory SQLite database (plaintext
//                            never touches the disk, satisfying zero-knowledge)
//
// Search runs over the in-memory FTS5 index (spec: local index in memory).

import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';

export class SQLiteBackend {
  constructor(absPath, { readOnly = false } = {}) {
    this.absPath = absPath;
    this.readOnly = readOnly;
    if (absPath !== ':memory:') {
      const dir = absPath.slice(0, absPath.lastIndexOf('/'));
      if (dir) mkdirSync(dir, { recursive: true });
    }
    this.db = new DatabaseSync(absPath || ':memory:');
    this.init(this.db);
    // In-memory FTS5 index (cleartext, never persisted to disk)
    this.ftsDb = new DatabaseSync(':memory:');
    this.initFts(this.ftsDb);
    // map docId (string) <-> fts rowid (int). FTS5 requires integer rowids;
    // rowids are allocated monotonically so a docId can never collide with
    // another (this replaces the old content-hash scheme).
    this.ftsRowids = new Map();
    this.ftsIdByRow = new Map();
    this._ftsNextRowid = 1;
  }

  init() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS workspace (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        name TEXT NOT NULL,
        version INTEGER NOT NULL,
        salt TEXT NOT NULL,
        iterations TEXT NOT NULL,
        verifier TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS docs (
        id TEXT PRIMARY KEY,
        dek_iv TEXT NOT NULL,
        dek_data TEXT NOT NULL,
        iv TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
    `);
  }

  initFts(db) {
    db.exec(`CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(body, tokenize='porter')`);
  }

  async _rowidFor(docId) {
    // stable int rowid for a docId; FTS5 requires integer rowids. Allocated
    // from a monotonic counter (never reused, never derived from content), so
    // distinct docIds always get distinct rowids for the life of the process.
    if (this.ftsRowids.has(docId)) return this.ftsRowids.get(docId);
    const rid = this._ftsNextRowid++;
    this.ftsRowids.set(docId, rid);
    this.ftsIdByRow.set(rid, docId);
    return rid;
  }

  async docIdForRow(rid) {
    return this.ftsIdByRow.get(rid) || null;
  }

  close() {
    try {
      this.db.close();
    } catch {}
    try {
      this.ftsDb.close();
    } catch {}
  }

  // ---------- workspace params ----------

  async getParams() {
    const row = this.db.prepare('SELECT name, version, salt, iterations, verifier FROM workspace WHERE id=1').get();
    if (!row) return null;
    return {
      name: row.name,
      version: row.version,
      salt: row.salt,
      iterations: row.iterations,
      verifier: JSON.parse(row.verifier),
    };
  }

  async saveParams(params) {
    this.db
      .prepare(
        `INSERT INTO workspace (id, name, version, salt, iterations, verifier)
         VALUES (1, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           name=excluded.name, version=excluded.version, salt=excluded.salt,
           iterations=excluded.iterations, verifier=excluded.verifier`
      )
      .run(
        params.name,
        params.version,
        params.salt,
        params.iterations,
        JSON.stringify(params.verifier)
      );
  }

  async deleteParams() {
    this.db.prepare('DELETE FROM workspace WHERE id=1').run();
  }

  // ---------- app metadata (small non-secret key/value store) ----------

  async getMeta(key) {
    const row = this.db.prepare('SELECT value FROM meta WHERE key = ?').get(key);
    return row ? row.value : null;
  }

  async setMeta(key, value) {
    this.db
      .prepare(
        'INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value'
      )
      .run(key, value);
  }

  // ---------- docs ----------

  async listDocs() {
    return this.db.prepare('SELECT id FROM docs ORDER BY updated_at DESC').all();
  }

  async getDoc(id) {
    const row = this.db.prepare('SELECT * FROM docs WHERE id = ?').get(id);
    if (!row) return null;
    return {
      id: row.id,
      dekIv: row.dek_iv,
      dekData: row.dek_data,
      iv: row.iv,
      data: row.data,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  async putDoc(entry) {
    this.db
      .prepare(
        `INSERT INTO docs (id, dek_iv, dek_data, iv, data, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           dek_iv=excluded.dek_iv, dek_data=excluded.dek_data, iv=excluded.iv,
           data=excluded.data, created_at=excluded.created_at, updated_at=excluded.updated_at`
      )
      .run(
        entry.id,
        entry.dekIv,
        entry.dekData,
        entry.iv,
        entry.data,
        entry.createdAt,
        entry.updatedAt
      );
  }

  async deleteDoc(id) {
    this.db.prepare('DELETE FROM docs WHERE id = ?').run(id);
  }

  // ---------- FTS5 (in-memory, cleartext index) ----------

  async ftsPut(docId, text) {
    const rid = await this._rowidFor(docId);
    this.ftsDb.prepare('DELETE FROM docs_fts WHERE rowid = ?').run(rid);
    this.ftsDb.prepare('INSERT INTO docs_fts(rowid, body) VALUES (?, ?)').run(rid, text);
  }

  async ftsDelete(docId) {
    const rid = await this._rowidFor(docId);
    this.ftsDb.prepare('DELETE FROM docs_fts WHERE rowid = ?').run(rid);
  }

  async ftsSearch(query) {
    if (!query || !query.trim()) return [];
    const stmt = this.ftsDb.prepare('SELECT rowid AS rid FROM docs_fts WHERE docs_fts MATCH ?');
    let rows;
    try {
      rows = stmt.all(query);
    } catch {
      // FTS5 has its own query grammar; operator/syntax errors in a user query
      // must surface as "no results", never as an uncaught exception.
      rows = [];
    }
    const out = [];
    for (const r of rows) {
      const id = await this.docIdForRow(r.rid);
      if (id) out.push({ id });
    }
    return out;
  }
}
