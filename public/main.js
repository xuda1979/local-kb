// main.js — offline single-page demo UI.
// Zero-knowledge: documents are encrypted client-side; only ciphertext is stored.

import { IndexedDBBackend } from './idb.js';
import { openKb, createKb } from '../src/factory.js';
import { exportKB, serializeKB, deserializeKB, importKB } from '../src/bak.js';
import { buildTape, serializeTape, parseTape, applyTapeFile, assertSameWorkspace } from '../src/syncfile.js';
import { textToBlocks, blocksToMarkdown } from '../src/factory.js';
import { changeMasterPassword } from '../src/rekey.js';
import { renderMarkdown } from './markdown.js';
import { uuid } from '../src/util.js';

const $ = (sel) => document.querySelector(sel);

let state = {
  store: null,
  params: null,
  current: null, // { id, doc }
  docs: [],
  dirty: false,
  preview: false,
};

// Mark the editor dirty unless the caller explicitly clears (save / open /
// new / delete all serialize the textarea back into state.current first).
function markDirty() {
  if (!state.current) return;
  state.dirty = true;
  $('#doc-meta').textContent = 'edited · unsaved changes';
  window.onbeforeunload = (e) => {
    e.preventDefault();
    e.returnValue = '';
  };
}

function clearDirty() {
  state.dirty = false;
  window.onbeforeunload = null;
}

function setView(v) {
  document.title = v.title;
  ['#view-unlock', '#view-app'].forEach((sel) => {
    $(sel).style.display = 'none';
  });
  $(v.target).style.display = 'block';
}

function log(msg, error) {
  const el = $('#log');
  el.textContent = msg;
  el.classList.toggle('error', !!error);
}

// ---------- unlock ----------

async function refreshDocList(store) {
  const docs = await store.list();
  docs.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
  state.docs = docs;
  renderDocList();
}

function renderDocList() {
  const list = $('#doc-list');
  list.innerHTML = '';
  if (!state.docs.length) {
    list.innerHTML = '<p class="muted">No documents yet. Real encrypt first.</p>';
  }
  for (const d of state.docs) {
    const li = document.createElement('div');
    li.className = 'doc-row' + (state.current && state.current.id === d.id ? ' active' : '');
    li.textContent = d.title || '(untitled)';
    li.addEventListener('click', () => openDoc(d.id));
    list.appendChild(li);
  }
}

async function openDoc(id) {
  if (state.dirty && state.current && state.current.id !== id) {
    if (!confirm('Discard unsaved changes to ' + (state.current.title || 'this doc') + '?')) return;
  }
  const doc = await state.store.get(id);
  if (!doc) return;
  state.current = { id, ...doc };
  state.preview = false;
  clearDirty();
  renderEditor();
  $('#doc-meta').textContent = 'updated ' + (doc.updatedAt || '').slice(0, 16);
  renderDocList();
}

// ---------- editor / preview ----------

function editorValue() {
  return $('#editor-area').value;
}

function renderEditor() {
  const title = (state.current && state.current.title) || '';
  $('#editor-title').value = title;
  $('#editor-area').value = state.current ? blocksToMarkdown(state.current.blocks) : '';
  $('#preview').style.display = 'none';
  $('#editor-area').style.display = 'block';
  $('#btn-preview').textContent = 'Preview';
  state.preview = false;
}

function togglePreview() {
  if (!state.current) return;
  state.preview = !state.preview;
  const prev = $('#preview');
  const ta = $('#editor-area');
  if (state.preview) {
    prev.innerHTML = renderMarkdown(editorValue());
    prev.style.display = 'block';
    ta.style.display = 'none';
    $('#btn-preview').textContent = 'Edit';
  } else {
    prev.style.display = 'none';
    ta.style.display = 'block';
    $('#btn-preview').textContent = 'Preview';
  }
}

// ---------- actions ----------

async function saveDoc() {
  if (!state.current || !state.store) return;
  const title = $('#editor-title').value.trim();
  const blocks = textToBlocks($('#editor-area').value);
  const updated = await state.store.edit(state.current.id, title || '(untitled)', blocks);
  state.current = { ...state.current, ...updated };
  clearDirty();
  $('#doc-meta').textContent = 'updated ' + (updated.updatedAt || '').slice(0, 16);
  await refreshDocList(state.store);
  log('Saved (encrypted at rest).');
}

async function newDoc() {
  // 用户手册 3.3: "未保存的修改在切换文档或离开页面时会收到提醒" — starting a
  // fresh doc discards the current one, so dirty edits must not go silently.
  if (state.dirty && !confirm('Discard unsaved changes to ' + (state.current.title || 'this doc') + ' before creating a new one?')) return;
  const id = uuid();
  const title = 'Untitled ' + new Date().toLocaleTimeString();
  const blocks = [{ type: 'paragraph', text: '' }];
  const created = await state.store.create(id, title, blocks);
  state.current = { id, ...created };
  clearDirty();
  await refreshDocList(state.store);
  $('#editor-title').value = title;
  $('#editor-area').value = '';
  $('#btn-preview').textContent = 'Preview';
  state.preview = false;
  $('#preview').style.display = 'none';
  $('#editor-area').style.display = 'block';
  log('New doc created.');
}

async function removeDoc() {
  if (!state.current || !state.store) return;
  if (!confirm('Delete this document? This cannot be undone.')) return;
  await state.store.delete(state.current.id);
  state.current = null;
  clearDirty();
  $('#editor-title').value = '';
  $('#editor-area').value = '';
  $('#btn-preview').textContent = 'Preview';
  state.preview = false;
  $('#preview').style.display = 'none';
  $('#editor-area').style.display = 'block';
  await refreshDocList(state.store);
  log('Document deleted.');
}

async function doSearch() {
  const q = $('#search-term').value.trim();
  if (!q) return refreshDocList(state.store);
  const hits = await state.store.search(q);
  const list = $('#doc-list');
  list.innerHTML = '';
  if (!hits.length) {
    list.innerHTML = '<p class="muted">No matches.</p>';
    return;
  }
  for (const h of hits) {
    const li = document.createElement('div');
    li.className = 'doc-row';
    li.textContent = h.title || '(untitled)';
    li.addEventListener('click', () => openDoc(h.id));
    list.appendChild(li);
  }
}

// ---------- export / import (.kb) ----------

// Download name embeds the export/sync time so restored backups are
// distinguishable (用户手册 3.6: "文件名含导出时间"). Format uses only
// filesystem-safe characters: local-kb-backup-2026-11-16-12-34-56.kb
function stampedName(base, ext) {
  const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
  return base + '-' + ts + '.' + ext;
}

async function exportKBFile() {
  if (!state.store) return;
  const all = await state.store.db.listDocs();
  const rows = [];
  for (const r of all) {
    const d = await state.store.db.getDoc(r.id);
    rows.push(d);
  }
  const kb = exportKB(state.params, rows);
  const blob = new Blob([serializeKB(kb)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = stampedName('local-kb-backup', 'kb');
  a.click();
  URL.revokeObjectURL(a.href);
  log('Exported .kb backup (ciphertext only).');
}

async function importKBFile(file) {
  const kb = deserializeKB(await file.arrayBuffer());
  if (!confirm('Import ' + kb.documents.length + ' docs? Existing workspace data merges.')) return;
  const params = await importKB(state.store.db, kb);
  // re-open with current password to rebuild store handles
  const pw = sessionStorage.getItem('local-kb-pw');
  const res = await openKb(state.store.db, pw);
  if (res.ok) {
    state.store = res.store;
    state.params = res.params;
    await refreshDocList(state.store);
    log('Imported ' + kb.documents.length + ' docs.');
  } else {
    log('Import done, but unlock failed.', true);
  }
}

// ---------- sync tape (.kbsync) ----------

async function syncOutFile() {
  if (!state.store) return;
  if (state.dirty && !confirm('Save the current document before syncing out? The tape carries ' +
      'only what is already persisted.')) return;
  const tape = await buildTape(state.params, state.store.db);
  const blob = new Blob([serializeTape(tape)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = stampedName('local-kb-sync', 'kbsync');
  a.click();
  URL.revokeObjectURL(a.href);
  log('Wrote sync tape (' + tape.entries.length + ' entries, ciphertext only). Carry it to another machine and "Sync in".');
}

async function syncInFile(file) {
  let tape;
  try {
    tape = parseTape(new Uint8Array(await file.arrayBuffer()));
  } catch (err) {
    log('Sync refused: ' + err.message, true);
    return;
  }
  try {
    assertSameWorkspace(state.params, tape);
  } catch (err) {
    log('Sync refused: ' + err.message, true);
    return;
  }
  const res = await applyTapeFile(state.store.db, tape);
  // Re-open with the current password so store handles + FTS index reflect
  // everything the tape merged in (new/deleted docs, tombstones).
  const pw = sessionStorage.getItem('local-kb-pw');
  const reopened = await openKb(state.store.db, pw);
  if (reopened.ok) {
    state.store = reopened.store;
    state.params = reopened.params;
    await refreshDocList(state.store);
  } else {
    log('Sync done, but re-unlock failed.', true);
    return;
  }
  log(res.applied + ' docs synced in, ' + res.skipped + ' skipped.');
}

// ---------- change master password (re-key) ----------

async function changePassword() {
  if (!state.store) return;
  const current = $('#rekey-current').value;
  const next = $('#rekey-new').value;
  const confirmPw = $('#rekey-confirm').value;
  if (!current) return log('Enter the current master password.', true);
  if (next.length < 8) return log('New password must be at least 8 characters.', true);
  if (next !== confirmPw) return log('New passwords do not match.', true);
  if (next === current) return log('New password is the same as the current one.', true);
  if (state.dirty && !confirm('The current document has unsaved changes. Re-key first?')) return;

  const res = await changeMasterPassword(state.store.db, current, next);
  if (!res.ok) {
    if (res.error === 'wrong-master-password') return log('Current password is wrong — nothing changed.', true);
    return log('Re-key failed: ' + res.error, true);
  }
  // The workspace key changed; re-open with the new password so store handles
  // and the FTS index are derived from the current key.
  sessionStorage.setItem('local-kb-pw', next);
  const reopened = await openKb(state.store.db, next);
  if (!reopened.ok) return log('Re-key done, but re-unlock with the new password failed.', true);
  state.store = reopened.store;
  state.params = reopened.params;
  state.current = null;
  await refreshDocList(state.store);
  $('#editor-title').value = '';
  $('#editor-area').value = '';
  $('#rekey-current').value = '';
  $('#rekey-new').value = '';
  $('#rekey-confirm').value = '';
  log('Master password changed: ' + res.rewrapped + ' document(s) re-wrapped. Old password is now inert.');
}

// ---------- boot ----------

async function enterApp(store, params) {
  state.store = store;
  state.params = params;
  $('#ws-label').textContent = '· ' + params.name;
  setView({ title: 'Local KB · unlocked', target: '#view-app' });
  await refreshDocList(store);
  armIdleLock();
}

// ---------- idle auto-lock ----------
// 用户手册 3.2: "每次打开页面（或默认一段时间后），需用主密码解锁" — the vault
// must re-lock itself after a period of inactivity, not only on the Lock button.
// Default 5 minutes; a page-global override (window.LOCAL_KB_IDLE_MS) lets the
// browser E2E shrink the window to real seconds.

const IDLE_LOCK_MS = 5 * 60 * 1000;
let idleTimer = null;

function idleWindowMs() {
  const n = Number(window.LOCAL_KB_IDLE_MS);
  return Number.isFinite(n) && n > 0 ? n : IDLE_LOCK_MS;
}

function armIdleLock() {
  clearIdleLock();
  if (!state.store) return; // locked view — no timer
  idleTimer = setTimeout(idleLock, idleWindowMs());
}

function clearIdleLock() {
  if (idleTimer != null) {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
}

// The shared lock body. The manual Lock button guards unsaved changes with a
// confirm; the idle path must not prompt (a modal on an empty desk is useless),
// so it persists any pending edits first — in a zero-knowledge vault nothing
// can be recovered after the key drops, so losing a dirty editor silently
// would be unrecoverable.
function doLock() {
  sessionStorage.removeItem('local-kb-pw');
  state.store = null;
  state.params = null;
  state.current = null;
  state.docs = [];
  state.dirty = false;
  state.preview = false;
  clearDirty();
  clearIdleLock();
  $('#editor-title').value = '';
  $('#editor-area').value = '';
  $('#search-term').value = '';
  $('#doc-list').innerHTML = '';
  $('#preview').innerHTML = '';
  $('#editor-area').style.display = 'block';
  $('#preview').style.display = 'none';
  $('#btn-preview').textContent = 'Preview';
  setView({ title: 'Local KB · lock', target: '#view-unlock' });
}

function lock() {
  if (state.dirty && !confirm('Discard unsaved changes and lock?')) return;
  $('#log').textContent = '';
  doLock();
}

async function idleLock() {
  if (state.store && state.current && state.dirty) {
    await saveDoc(); // persist before dropping the key, never lose edits
  }
  doLock();
  log('Auto-locked after inactivity — the session password was dropped.');
}

async function unlock() {
  const pw = $('#pw').value;
  if (!pw) return log('Enter a master password.', true);
  const db = new IndexedDBBackend();
  const res = await openKb(db, pw);
  if (!res.ok) {
    log('Wrong master password (or no vault).', true);
    return;
  }
  sessionStorage.setItem('local-kb-pw', pw);
  await enterApp(res.store, res.params);
}

async function createVault() {
  const name = $('#wsname').value.trim();
  if (!name) return log('Give the workspace a name (it participates in key derivation).', true);
  const pw = $('#pw2').value;
  if (!pw || pw.length < 8) return log('Use at least 8 characters.', true);
  const confirmPw = $('#pw2b').value;
  if (pw !== confirmPw) return log('Passwords do not match — the master password can never be recovered, so please re-enter it carefully.', true);
  const db = new IndexedDBBackend();
  // Never create over a live vault: saveParams is an id=1 upsert, so a new
  // workspace would replace the params and orphan every existing document's
  // ciphertext (unrecoverable without the old workspace key). The .kb import
  // path already refuses cross-workspace overwrites; creation must too.
  const vault = await db.getParams();
  if (vault) return log('A vault already exists in this browser — unlock with its master password instead.', true);
  const { store, workspace } = await createKb(db, pw, name);
  sessionStorage.setItem('local-kb-pw', pw);
  const params = await db.getParams();
  await enterApp(store, params);
}

function bind() {
  $('#btn-unlock').addEventListener('click', unlock);
  $('#btn-create').addEventListener('click', createVault);
  $('#btn-save').addEventListener('click', saveDoc);
  $('#btn-new').addEventListener('click', newDoc);
  $('#btn-lock').addEventListener('click', lock);
  $('#btn-delete').addEventListener('click', removeDoc);
  $('#btn-search').addEventListener('click', doSearch);
  $('#btn-export').addEventListener('click', exportKBFile);
  $('#btn-sync-out').addEventListener('click', syncOutFile);
  $('#btn-preview').addEventListener('click', togglePreview);
  $('#btn-rekey').addEventListener('click', changePassword);
  $('#import-file').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) importKBFile(f);
  });
  $('#sync-file').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) syncInFile(f);
  });
  $('#editor-title').addEventListener('input', markDirty);
  $('#editor-area').addEventListener('input', markDirty);
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveDoc();
    }
  });
  // Any real user activity postpones the idle auto-lock (manual 3.2). Keydown
  // also fires for hotkeys, which is fine — a typing user is not idle.
  for (const evt of ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart']) {
    document.addEventListener(evt, () => armIdleLock());
  }
}

setView({ title: 'Local KB · lock', target: '#view-unlock' });
document.addEventListener('DOMContentLoaded', bind);
