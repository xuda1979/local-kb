// util.js — small shared helpers + a config singleton.

export const stillStore = {
  iterations: 310000,
};

export function uuid() {
  return globalThis.crypto.randomUUID();
}

export function nowISO() {
  return new Date().toISOString();
}

// Non-cryptographic hash used only for FTS rowid bookkeeping, never security.
export function ftsRowId(docId, salt) {
  let h = 0;
  const s = docId + ':' + salt;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h >>> 0;
}
