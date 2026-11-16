// main.js — offline single-page demo UI.
// Zero-knowledge: documents are encrypted client-side; only ciphertext is stored.

import { IndexedDBBackend } from './idb.js';
import { openKb, createKb } from '../src/factory.js';
import { exportKB, serializeKB, deserializeKB, importKB } from '../src/bak.js';
import { buildTape, serializeTape, parseTape, applyTapeFile, assertSameWorkspace } from '../src/syncfile.js';
import { textToBlocks, blocksToMarkdown } from '../src/factory.js';
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
  a.download = 'local-kb-backup.kb';
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
  a.download = 'local-kb-sync.kbsync';
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

// ---------- boot ----------

async function enterApp(store, params) {
  state.store = store;
  state.params = params;
  setView({ title: 'Local KB · unlocked', target: '#view-app' });
  await refreshDocList(store);
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
  const pw = $('#pw2').value;
  if (!pw || pw.length < 8) return log('Use at least 8 characters.', true);
  const db = new IndexedDBBackend();
  const { store, workspace } = await createKb(db, pw);
  sessionStorage.setItem('local-kb-pw', pw);
  const params = await db.getParams();
  await enterApp(store, params);
}

function bind() {
  $('#btn-unlock').addEventListener('click', unlock);
  $('#btn-create').addEventListener('click', createVault);
  $('#btn-save').addEventListener('click', saveDoc);
  $('#btn-new').addEventListener('click', newDoc);
  $('#btn-delete').addEventListener('click', removeDoc);
  $('#btn-search').addEventListener('click', doSearch);
  $('#btn-export').addEventListener('click', exportKBFile);
  $('#btn-sync-out').addEventListener('click', syncOutFile);
  $('#btn-preview').addEventListener('click', togglePreview);
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
}

setView({ title: 'Local KB · lock', target: '#view-unlock' });
document.addEventListener('DOMContentLoaded', bind);
