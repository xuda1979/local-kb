// docmodel.js — document model. Similar to Notion blocks, rendered as markdown
// in the UI. Documents are stored as JSON text (in memory) and encrypted at rest.

import { nowISO } from './util.js';

export const BLOCK_TYPES = ['title', 'paragraph', 'heading', 'code', 'quote', 'table'];

export function createBlock(type, text = '') {
  if (!BLOCK_TYPES.includes(type)) throw new Error('unknown block type: ' + type);
  return { type, text };
}

export function createDoc(title = '', blocks = []) {
  if (!Array.isArray(blocks)) blocks = [];
  const ts = nowISO();
  return {
    title,
    blocks,
    createdAt: ts,
    updatedAt: ts,
  };
}

// Serialize whole doc (INCLUDING internal metadata) to a JSON string.
// This is what gets encrypted with the document DEK.
export function serializeDoc(doc) {
  return JSON.stringify(doc);
}

export function deserializeDoc(json) {
  let obj;
  try {
    obj = JSON.parse(json);
  } catch {
    throw new Error('corrupt document JSON');
  }
  if (!obj || typeof obj !== 'object') throw new Error('corrupt document JSON');
  return {
    title: typeof obj.title === 'string' ? obj.title : '',
    blocks: Array.isArray(obj.blocks) ? obj.blocks : [],
    createdAt: obj.createdAt || nowISO(),
    updatedAt: obj.updatedAt || nowISO(),
  };
}
