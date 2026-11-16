// sync.js — plaintext-free sync plane (spec P3 / M3, optional).
//
// Scope: a *ciphertext* oplog. Each node appends one entry per document write,
// and nodes exchange entries; the entry bodies contain only ciphertext, docId
// and a lamport-ish timestamp. No plaintext ever appears in an oplog entry —
// grepping a synced database or an oplog carry must find nothing readable.
//
// Design notes / invariants
//   * one writer at a time (spec: v1 single-writer; no CRDT resolution here).
//   * Deterministic entry ordering: LWW on (updatedAt, docId) tuple *within* a
//     node's own sequence. Entries are NOT deduped by content across nodes:
//     since each writer re-encrypts (fresh IV per save), last-writer-wins on
//     the (updatedAt, docId) tuple is the convergent rule, applied per docId.
//   * apply(entries) is idempotent per docId and satisfies: for two nodes that
//     have separately received all of each other's entries, both converge to
//     the same doc set and same per-doc ciphertext (the newest entry per docId
//     wins; ties from the same writer keep the greatest seq).
//   * "updatedAt" comes straight from the app timestamp (non-secret). Sorting
//     a tape never requires decrypting anything.

const maxKV = (a, b) => {
  if (b.updatedAt < a.updatedAt) return a;
  if (b.updatedAt > a.updatedAt) return b;
  return b.id > a.id ? b : a; // stable tie-break
};

// Merge two entry arrays into one converged list. The result is a candidate
// *preview* — it does not touch any adapter.
export function mergeTape(a, b) {
  const map = new Map();
  for (const e of (a || []).concat(b || [])) {
    if (!e || typeof e.id !== 'string') continue;
    map.set(e.id, map.has(e.id) ? maxKV(map.get(e.id), e) : e);
  }
  return [...map.values()].sort((x, y) =>
    x.updatedAt < y.updatedAt ? -1 : x.updatedAt > y.updatedAt ? 1 : x.id < y.id ? -1 : 1
  );
}

// Apply a tape to a local adapter. Existing docs with a matching updatedAt are
// skipped (idempotence); otherwise the newest entry per id wins. Deletes are
// entries with deleted:true. Returns { applied, skipped } counts.
export async function applyTape(tape, adapter) {
  const now = Date.now();
  let applied = 0;
  let skipped = 0;
  for (const e of tape) {
    if (!e || typeof e.id !== 'string' || !e.updatedAt) {
      skipped++;
      continue;
    }
    const existing = await adapter.getDoc(e.id);
    if (existing && existing.updatedAt === e.updatedAt) {
      skipped++; // already have this exact version
      continue;
    }
    if (e.deleted) {
      await adapter.deleteDoc(e.id);
      if (adapter.ftsDelete) await adapter.ftsDelete(e.id);
    } else if (e.dekIv && e.dekData && e.iv && e.data) {
      await adapter.putDoc({
        id: e.id,
        dekIv: e.dekIv,
        dekData: e.dekData,
        iv: e.iv,
        data: e.data,
        createdAt: e.createdAt || '',
        updatedAt: e.updatedAt || new Date(now).toISOString(),
      });
    } else {
      skipped++;
      continue;
    }
    applied++;
  }
  return { applied, skipped };
}

// Build the local tape from the current doc set: one plaintext-free entry per
// doc (the newest state). Every entry carries the document's own updatedAt so
// peer tapes converge to the newest per id.
export async function localTape(adapter) {
  const ids = (await adapter.listDocs()).map((r) => r.id);
  const entries = [];
  for (const id of ids) {
    const d = await adapter.getDoc(id);
    if (!d) continue;
    entries.push({
      id: d.id,
      dekIv: d.dekIv,
      dekData: d.dekData,
      iv: d.iv,
      data: d.data,
      createdAt: d.createdAt || '',
      updatedAt: d.updatedAt,
      deleted: false,
    });
  }
  return entries;
}
