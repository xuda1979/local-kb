// docmodel.test.mjs — block model + markdown converter sanity.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBlock, createDoc, serializeDoc, deserializeDoc } from '../src/docmodel.js';
import { textToBlocks, blocksToMarkdown } from '../src/factory.js';

test('block model round trips', () => {
  const doc = createDoc('Title', [
    createBlock('heading', 'H1'),
    createBlock('paragraph', 'body'),
    createBlock('code', 'let x = 1'),
    createBlock('quote', 'cited'),
    createBlock('table', 'a|b'),
  ]);
  const json = serializeDoc(doc);
  const back = deserializeDoc(json);
  assert.equal(back.title, 'Title');
  assert.equal(back.blocks.length, 5);
  assert.deepEqual(back.blocks[0], { type: 'heading', text: 'H1' });
});

test('unknown block type rejected', () => {
  assert.throws(() => createBlock('marquee', 'x'));
});

test('textToBlocks preserves headings/code/quote', () => {
  const text = `# Big heading\n\n> a quote\n\n\`\`\`\ncode here\n\`\`\`\n\njust paragraph`;
  const blocks = textToBlocks(text);
  assert.equal(blocks[0].type, 'heading');
  assert.equal(blocks[1].type, 'quote');
  assert.equal(blocks[2].type, 'code');
  assert.equal(blocks[3].type, 'paragraph');
  assert.equal(blocksToMarkdown(blocks).split('\n')[0], '# Big heading');
});

test('blocksToMarkdown reconstructs markdown from blocks', () => {
  const markdown = blocksToMarkdown([
    { type: 'heading', text: 'Title' },
    { type: 'paragraph', text: 'One two' },
    { type: 'quote', text: 'quoted' },
  ]);
  assert.match(markdown, /^# Title\n/);
  assert.match(markdown, /^> quoted/m);
});
