# 09 — Local-KB

Zero-knowledge, local-first knowledge base (E2EE like 1Password's crypto).
A privacy slice of Notion / 语雀: every document is encrypted before it
touches disk, so neither the backing store nor any server can read your notes.

- **Local-first**: no server required for core usage.
- **Zero-knowledge**: content is ciphertext everywhere except in RAM.
- **Encryption**: AES-256-GCM, per-document DEKs, PBKDF2-SHA256 key derivation
  + HKDF workspace namespacing (reuses the 01-passvault design).
- **Search**: FTS5 over a local in-memory index (plaintext in RAM only).
- **Portable backups**: `.kb` export/import for machine migration.
- **Offline sync tape**: `.kbsync` ciphertext carry — a delete-aware LWW sync
  plane that converges two machines sharing a workspace (P3/M3).
- **Markdown editor + preview**: dependency-free XSS-safe renderer; preview toggle
  and unsaved-changes guard in the demo UI. Pipe tables round-trip as `table`
  blocks through the editor and the encrypted store.
- **Change master password**: re-key the entire vault (every DEK re-wrapped)
  without losing data; the old password stops working atomically at the end.

---

## Quick start

```bash
# headless tests (Node >= 22)
npm test

# offline demo page (static server only)
node server/demo.mjs
# open http://localhost:4242
```

### Try it

1. Choose a master password (>= 8 chars) and **Create vault**.
2. Create/en/edit documents; hit **Save**. Everything is re-encrypted instantly.
3. Close the tab, reopen, unlock — data is still there.
4. **Lock** to drop the master password from the session and clear plaintext
   from memory without closing the tab; unlock again with the same password.
5. **Search** to find docs by keyword (local FTS index).
6. Toggle **Preview** to render the markdown live; **Ctrl/Cmd+S** saves.
7. **Export .kb** to download a ciphertext-only backup; **Import .kb** on
   another machine (or empty profile) to restore — using the same master password.
8. **Sync out** writes a `.kbsync` file (ciphertext-only LWW tape); on a second
   machine sharing the same workspace password, **Sync in** that file to
   converge both doc sets — including deletes (tombstones carried in the tape).
9. **Change master password**: enter current + new password (twice) in the
   app view card — every document is re-wrapped under the new password, then
   the old password stops working.

> The demo is 100% offline-capable: all code ships as static files, the only
> server is a trivial static file server.

---

## Cryptography architecture

### Key hierarchy

```
masterPassword
   └─(PBKDF2-SHA256, 310,000 iters, random 16-byte salt)──▶ unlockKey (32 B)
        └─(HKDF-SHA256, purpose "local-kb/ws:"+workspaceName)──▶ workspaceKey (32 B)
             └─(AES-256-GCM wrap)──▶  per-document Data Encryption Key (DEK)
                  └─(AES-256-GCM encrypt)──▶  document JSON → ciphertext
```

- **Workspace key** is *derived from the master password only*, never stored.
- Each **document** gets a fresh random 32-byte DEK.
- The DEK is wrapped with the workspace key and stored alongside the document.
- Document ciphertext is produced with the DEK.
- Unwrap/needs the workspace key requires the master password; without the
  password nothing on disk is decryptable.

### Structure on disk / in IndexedDB

```
workspace  (row id=1)
  name, version, salt (cleartext — not sensitive),
  iterations, verifier: {iv, data}   (AES-256-GCM of a constant, used as an
                                      unlock gate)

docs  (one row per document)
  id            — UUID
  dekIv, dekData — wrapped DEK (AES-256-GCM, workspace key)
  iv, data       — document ciphertext (AES-256-GCM, DEK)
  created_at, updated_at
```

Nothing in `workspace` or `docs` contains document plaintext. The verifier is a
ciphertext blob; the salt is a public nonce and reveals nothing about password
or content.

### Search (FTS)

Search runs against an **in-memory FTS5 index** built from documents on unlock.
No plaintext is ever written to disk for the search index. In the browser the
same design holds (the IndexedDB adapter keeps the FTS map in memory).

### Threats

| Threat | Mitigation |
| --- | --- |
| Disk / DB leak (file exposure) | Only ciphertext stored |
| Wrong password | Verifier + all DEK unwraps fail with an AEAD auth tag |
| Server/DB admin snooping | Same as disk leak — no plaintext server-visible |
| Brute-force password | PBKDF2-SHA256 310k iterations |
| doc key compromise | Each doc independent (unique DEK per doc) |

Limitations (documented): the web client keeps plaintext in memory while a
workspace is unlocked; a device compromise with access to the open page can
read content. Use **Lock** when stepping away from the machine — it drops the
master password from the session and clears memory held plaintext (the in-memory
FTS index is released with it). The password is the single root of trust — lose
it and the data is unrecoverable by design.

---

## Project layout

```
src/
  crypto.js       WebCrypto primitives (PBKDF2/HKDF/AES-GCM/base64url)
  workspace.js    workspace create/lock/unlock (password gate)
  store.js        document store: DEK wrap/unwrap, CRUD, search glue
  db.js           SQLite backend (node:sqlite) for headless Node
  bak.js          .kb export/import serialization
  rekey.js        master-password rotation (re-key): re-wrap DEKs, rebuild verifier
  factory.js      high-level createKb/openKb wiring + markdown <-> blocks
  docmodel.js     Notion-like block model
  sync.js         ciphertext-only sync plane (P3/M3): tape export/merge/apply
  syncfile.js     .kbsync file transport: build/serialize/parse/apply + workspace gate
  util.js         generators/helpers
tests/
  crypto, workspace, persistence, no-plaintext-on-disk, search,
  backup, dek-wrap, search-title, docmodel, idb.store, import-safety,
  fts-hardening, sync, syncfile, rekey  (Node test runner)
  browser_e2e.py  optional real-Firefox/geckodriver drive test
public/
  index.html, main.js, markdown.js, idb.js   — offline web client
server/demo.mjs                 — static demo server
vendor/fake-indexeddb/          — vendored spec-compliant in-memory IDB
```

## Testing

`npm test` runs 71 tests headlessly via `node --test`:

- **No-plaintext-on-disk**: after writing known strings (title, body text,
  code, master password), greps the raw SQLite bytes and IndexedDB records —
  requires zero matches.
- **Persistence**: create/edit/delete across fresh DB opens.
- **Wrong password**: verifier blocks unlock; tampered verifier/DEK blocked.
- **FTS search**: keyword hits and misses, reindex-on-edit, and **rebuild on
  unlock** (the index is plaintext and never persisted, so it is re-derived
  from the decrypted store every time a workspace opens).
- **FTS hardening**: malformed/operator queries return empty results instead
  of throwing; monotonic index rowids can never alias a second doc.
- **Backup**: `.kb` import on a fresh store restores every doc; importing a
  backup from a *different workspace* over an existing vault is refused (it
  would orphan existing documents); re-import is idempotent.
- **Sync (P3)**: tapes carry only ciphertext; two stores sharing a workspace
  converge after exchanging tapes; deletes propagate as tombstones (a newer
  local doc is never clobbered by a stale tape, a deleted-then-recreated doc
  only ever leaves the live entry behind, and a tape from a *different
  workspace* is refused); re-applying a tape is a no-op.
- **Markdown**: structural output for every block type (headings, lists,
  tables, fenced code…), and XSS safety — escape-first throughout plus
  `javascript:`/`data:` link targets degraded to inert text. Pipe tables
  round-trip the editor ↔ encrypted-store boundary as `table` blocks.
- **Re-key**: a wrong current password is refused with zero mutation; a
  successful rotation re-wraps every DEK, leaves the old password inert and
  the new one working with all documents intact; no plaintext (including the
  passwords) ever touches disk — re-key is also stable across repeated
  rotations.
- The offline browser UI is exercised through the same store layer with
  `fake-indexeddb` (spec-compliant, no browser needed).

Optional real-browser run (if `geckodriver` + firefox available):

```bash
node server/demo.mjs &
.venv/bin/python tests/browser_e2e.py
```

The browser test may need `FIREFOX_BIN` set to a real firefox executable
(on Ubuntu the `/usr/bin/firefox` snap wrapper isn't a binary); the script
falls back to the snap's inner binary automatically.

## Production notes

- The vendor/ directory pins `fake-indexeddb@6.2.8` (MIT) for hermetic tests
  because the sandbox npm reify step is unreliable; no runtime dependency.
- M1 (encryption + editing + storage) and M2 (search + backup) are met; M3's
  ciphertext sync plane ships as `src/sync.js` (`localTape`/`mergeTape`/
  `applyTape`) plus a complete **file transport** (`src/syncfile.js`,
  `.kbsync`): build, serialize, parse, apply, and a workspace-identity gate.
  Hosted/networked sync is not part of this release — the file carry is the
  offline-friendly transport the SPEC allows.

## License

MIT.
