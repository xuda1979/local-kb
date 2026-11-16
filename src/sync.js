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

// Apply a tape to a local adapter. The newest entry per id wins on the
// non-secret updatedAt timestamp: a local doc newer than the incoming entry is
// left untouched, a write matching the local version is skipped (idempotence),
// and a deleted:true entry whose timestamp dominates the local doc's applies
// the delete (a tombstone at the same updatedAt is the delete of exactly that
// version — it must not be treated as a duplicate). Returns { applied, skipped }.
export async function applyTape(tape, adapter) {
  const now = Date.now();
  let applied = 0;
  let skipped = 0;
  for (const e of tape) {
    if (!e || typeof e.id !== 'string' || typeof e.updatedAt !== 'string') {
      skipped++;
      continue;
    }
    const existing = await adapter.getDoc(e.id);
    const existingTs = existing ? existing.updatedAt || '' : '';
    if (e.deleted) {
      // LWW on updatedAt: a tombstone younger than the local doc deletes it;
      // a tombstone older than the local doc is stale and must not resurrect
      // either way (guards below).
      if (existing && existingTs > e.updatedAt) {
        skipped++; // local doc is newer than the tombstone — don't delete it
        continue;
      }
      if (existing) {
        await adapter.deleteDoc(e.id);
        if (adapter.ftsDelete) await adapter.ftsDelete(e.id);
      } else {
        // Doc already absent; just adopt the tombstone so it continues to
        // propagate (prevents an old tape from resurrecting the doc here).
        const ts = typeof adapter.listTombstones === 'function' ? await adapter.listTombstones() : [];
        if ((ts || []).some((t) => t && t.id === e.id && t.updatedAt === e.updatedAt)) {
          skipped++; // this exact tombstone already recorded
          continue;
        }
      }
      if (typeof adapter.markDeleted === 'function') {
        await adapter.markDeleted(e.id, e.updatedAt);
      }
      applied++;
      continue;
    }
    // live write entry
    if (existingTs === e.updatedAt) {
      skipped++; // already have this exact version
      continue;
    }
    if (existing && existingTs > e.updatedAt) {
      skipped++; // local copy is newer — don't let a stale tape clobber it
      continue;
    }
    if (!e.dekIv || !e.dekData || !e.iv || !e.data) {
      skipped++;
      continue;
    }
    await adapter.putDoc({
      id: e.id,
      dekIv: e.dekIv,
      dekData: e.dekData,
      iv: e.iv,
      data: e.data,
      createdAt: e.createdAt || '',
      updatedAt: e.updatedAt || new Date(now).toISOString(),
    });
    applied++;
  }
  return { applied, skipped };
}

// Build the local tape from the current doc set: one plaintext-free entry per
// doc (the newest state) plus one deleted:true entry per tombstoned doc, so
// deletes propagate to peers and never resurrect. Every entry carries the
// document's own updatedAt, so peer tapes converge to the newest per id.
export async function localTape(adapter) {
  const ids = (await adapter.listDocs()).map((r) => r.id);
  const live = new Set(ids);
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
  // Tombstoned docs that are no longer live become deleted:true entries. The
  // LWW guard in applyTape keys on updatedAt only, so a tombstone must never
  // be emitted while the doc is still live: if a doc was deleted and then
  // re-created under the same id, the live entry above already carries its
  // newer state, and a stale tombstone (with an earlier timestamp) could win a
  // timestamp tie on a peer and wrongly delete the re-created doc.
  if (typeof adapter.listTombstones === 'function') {
    const tombstones = await adapter.listTombstones();
    for (const t of tombstones || []) {
      if (!t || t.updatedAt == null) continue;
      if (live.has(t.id)) continue; // doc is live again — drop the stale tombstone
      entries.push({
        id: t.id,
        updatedAt: t.updatedAt,
        deleted: true,
      });
    }
  }
  return entries;
}
