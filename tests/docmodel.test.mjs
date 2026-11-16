// docmodel.test.mjs — block model + markdown converter sanity.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBlock, createDoc, serializeDoc, deserializeDoc } from '../src/docmodel.js';
import { textToBlocks, blocksToMarkdown } from '../src/factory.js';
import { renderMarkdown } from '../public/markdown.js';

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

test('textToBlocks groups consecutive pipe rows into one table block', () => {
  const text = '# T\n\n| a | b |\n| --- | --- |\n| 1 | 2 |';
  const blocks = textToBlocks(text);
  assert.equal(blocks.length, 2, 'heading + one table block');
  assert.deepEqual(blocks[1], {
    type: 'table',
    text: '| a | b |\n| --- | --- |\n| 1 | 2 |',
  });
});

test('table block round-trips through markdown and renders', () => {
  const table = '| a | b |\n| --- | --- |\n| 1 | 2 |';
  const blocks = textToBlocks('intro\n\n' + table);
  assert.equal(blocks.filter((b) => b.type === 'table').length, 1);
  const md = blocksToMarkdown(blocks); // rerenders the table verbatim
  assert.ok(md.includes(table));
  const html = renderMarkdown(md);
  assert.ok(html.includes('<table>'));
  assert.ok(html.includes('<th>a</th>'));
  assert.ok(html.includes('<td>2</td>'));
});

test('paragraph between tables splits them into separate blocks', () => {
  const blocks = textToBlocks('| a |\n|---|\n\nnote\n\n| c |\n|---|');
  const tables = blocks.filter((b) => b.type === 'table');
  assert.equal(tables.length, 2);
  assert.equal(blocks.some((b) => b.type === 'paragraph' && b.text === 'note'), true);
});

test('escaped pipe in a cell stays inside the table block', () => {
  const line = '| left | a \\| b |';
  const blocks = textToBlocks(line);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, 'table');
  assert.equal(blocks[0].text, line);
  // pipe-free paragraphs never become tables
  assert.equal(textToBlocks('a | b (not a table opening)').length, 1);
  assert.equal(textToBlocks('no pipes at all')[0].type, 'paragraph');
});

test('table cells are searchable via plaintextForIndex', async () => {
  const { createKb, openKb } = await import('../src/factory.js');
  const { SQLiteBackend } = await import('../src/db.js');
  const { tempDbPath } = await import('./helpers.mjs');
  const b = new SQLiteBackend(tempDbPath());
  await createKb(b, 'pw');
  const r = await openKb(b, 'pw');
  await r.store.create('t1', 'Table Doc', [
    { type: 'table', text: '| name | city |\n| --- | --- |\n| X | Kyoto |' },
  ]);
  assert.ok((await r.store.search('Kyoto')).some((h) => h.id === 't1'));
  b.close();
});
