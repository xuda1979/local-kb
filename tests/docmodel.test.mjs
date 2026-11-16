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
  assert.equal(blocks[2].text, '```\ncode here\n```', 'fenced body must be kept verbatim inside the code block');
  assert.equal(blocks[3].type, 'paragraph');
  assert.equal(blocksToMarkdown(blocks).split('\n')[0], '# Big heading');
});

test('fenced code round-trips losslessly (lang tag, both fences)', () => {
  // note: blank separator lines between blocks are collapsed by the block
  // model (paragraphs/quotes too); the code *region* itself is preserved
  // verbatim, fences, lang tag and body included.
  const text = '```js\nlet x = 1\nlet y = 2\n```\n~~~bash\necho hi\n~~~';
  const blocks = textToBlocks(text);
  assert.equal(blocks.length, 2, 'one code block per fenced region');
  assert.deepEqual(blocks[0], { type: 'code', text: '```js\nlet x = 1\nlet y = 2\n```' });
  assert.deepEqual(blocks[1], { type: 'code', text: '~~~bash\necho hi\n~~~' });
  // the editor round-trip (renderEditor -> saveDoc -> renderEditor) is lossless
  assert.equal(blocksToMarkdown(blocks), text);
  // blank lines *inside* a region are preserved too
  const blank = textToBlocks('```\na\n\nb\n```');
  assert.equal(blank.length, 1);
  assert.equal(blank[0].text, '```\na\n\nb\n```');
});

test('empty and unclosed fenced regions behave', () => {
  // immediately-closed fence → one empty code block
  const empty = textToBlocks('```\n```');
  assert.equal(empty.length, 1);
  assert.equal(empty[0].type, 'code');
  assert.equal(empty[0].text, '```\n```');
  // an unclosed fence runs to the end of the document (like the renderer)
  const unclosed = textToBlocks('before\n\n```python\nx = 1\ny = 2');
  assert.equal(unclosed[unclosed.length - 1].type, 'code');
  assert.equal(unclosed[unclosed.length - 1].text, '```python\nx = 1\ny = 2');
});

test('code regions are stable under re-emit + re-parse (no drift)', () => {
  const md = '# T\npara\n\n```js\nfunction f() {\n  return 1\n}\n```\n\n> q';
  const blocks = textToBlocks(md);
  assert.deepEqual(blocks[2], { type: 'code', text: '```js\nfunction f() {\n  return 1\n}\n```' });
  // re-emit then re-parse gives identical blocks — no garbage, no lost lines
  assert.deepEqual(textToBlocks(blocksToMarkdown(blocks)), blocks);
});

test('heading levels #..###### round-trip losslessly (user manual 3.5)', () => {
  const text = [
    '# One',
    '## Two',
    '### Three',
    '#### Four',
    '##### Five',
    '###### Six',
  ].join('\n');
  const blocks = textToBlocks(text);
  assert.equal(blocks.length, 6);
  for (let i = 0; i < 6; i++) {
    const hashes = '#'.repeat(i + 1);
    assert.equal(blocks[i].type, 'heading');
    assert.deepEqual(blocks[i], { type: 'heading', text: hashes + ' ' + ['One', 'Two', 'Three', 'Four', 'Five', 'Six'][i] }, 'level ' + (i + 1) + ' keeps its marker');
  }
  // save -> reload keeps every level, not just #
  assert.equal(blocksToMarkdown(blocks), text);
  assert.deepEqual(textToBlocks(blocksToMarkdown(blocks)), blocks);
});

test('nested quotes (>>) keep their depth on round-trip', () => {
  const blocks = textToBlocks('> outer\n>> nested\n> back');
  assert.equal(blocks.length, 3);
  assert.deepEqual(blocks[0], { type: 'quote', text: '> outer' });
  assert.deepEqual(blocks[1], { type: 'quote', text: '>> nested' });
  assert.deepEqual(blocks[2], { type: 'quote', text: '> back' });
  assert.equal(blocksToMarkdown(blocks), '> outer\n>> nested\n> back');
});

test('legacy marker-less heading/quote blocks still re-emit their marker', () => {
  // docs saved before the marker was stored verbatim carry bare text
  const md = blocksToMarkdown([
    { type: 'heading', text: 'Old Title' },
    { type: 'quote', text: 'old cite' },
  ]);
  assert.equal(md, '# Old Title\n> old cite');
  // already-markered text is never double-marked
  const again = blocksToMarkdown([
    { type: 'heading', text: '## Keeps Level' },
    { type: 'quote', text: '>> deep' },
  ]);
  assert.equal(again, '## Keeps Level\n>> deep');
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

test('fenced code body stays searchable (user manual 3.4: 代码全文匹配)', async () => {
  const { createKb, openKb } = await import('../src/factory.js');
  const { SQLiteBackend } = await import('../src/db.js');
  const { tempDbPath } = await import('./helpers.mjs');
  const b = new SQLiteBackend(tempDbPath());
  await createKb(b, 'pw');
  const r = await openKb(b, 'pw');
  // round-trips through the editor path: markdown -> blocks -> store
  const blocks = textToBlocks('```python\ndef encrypt(data):\n    return "x9k2"\n```');
  assert.equal(blocks.length, 1);
  await r.store.create('c1', 'Crypto Snippet', blocks);
  assert.ok((await r.store.search('x9k2')).some((h) => h.id === 'c1'), 'code body token must be searchable');
  assert.ok((await r.store.search('encrypt')).some((h) => h.id === 'c1'), 'code identifier must be searchable');
  b.close();
});
