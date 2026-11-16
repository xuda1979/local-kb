// markdown.test.mjs — offline markdown renderer (public/markdown.js).
// Structural output + XSS safety (escape-first, javascript: URL rejection).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderMarkdown, renderInline } from '../public/markdown.js';

test('headings render with escaped content', () => {
  assert.equal(renderMarkdown('# Hello'), '<h1>Hello</h1>');
  assert.equal(renderMarkdown('## A & B'), '<h2>A &amp; B</h2>');
  assert.equal(renderMarkdown('###### six'), '<h6>six</h6>');
});

test('paragraphs split on blank lines', () => {
  assert.equal(renderMarkdown('one\n\ntwo'), '<p>one</p><p>two</p>');
  assert.equal(renderMarkdown('one'), '<p>one</p>');
});

test('fenced code preserves trailing newline and lang class', () => {
  const out = renderMarkdown('```js\nlet x = 1 \n```');
  assert.equal(out, '<pre class="lang-js"><code>let x = 1 </code></pre>');
});

test('blockquote renders', () => {
  assert.equal(renderMarkdown('> cited'), '<blockquote>cited</blockquote>');
});

test('lists render as ul/ol', () => {
  assert.equal(renderMarkdown('- a\n- b\n- c'), '<ul><li>a</li><li>b</li><li>c</li></ul>');
  assert.equal(renderMarkdown('1. first\n2. second'), '<ol><li>first</li><li>second</li></ol>');
});

test('horizontal rule', () => {
  assert.equal(renderMarkdown('---'), '<hr>');
  assert.equal(renderMarkdown('***'), '<hr>');
});

test('inline formatting', () => {
  assert.equal(renderInline('**bold** and *it* and `code`'),
    '<strong>bold</strong> and <em>it</em> and <code>code</code>');
});

test('links escape and reject dangerous schemes', () => {
  assert.equal(renderInline('[ok](https://example.com/a?b=1&c=2)'),
    '<a href="https://example.com/a?b=1&amp;c=2">ok</a>');
  // javascript: URL must not survive as an href
  const out = renderInline('[bad](javascript:alert(1))');
  assert.equal(out, '<em>bad</em>');
  assert.ok(!out.includes('href'));
  // raw HTML is inert
  const injected = renderMarkdown('<script>alert(1)</script>');
  assert.ok(!injected.includes('<script>'));
  assert.ok(injected.includes('&lt;script&gt;'));
});

test('table renders header and rows', () => {
  const md = '| a | b |\n| --- | --- |\n| 1 | 2 |';
  const out = renderMarkdown(md);
  assert.ok(out.includes('<table>'));
  assert.ok(out.includes('<th>a</th>'));
  assert.ok(out.includes('<td>1</td>'));
  assert.ok(out.includes('</table>'));
});

test('backslash-escaped pipe stays in one cell and renders literal', () => {
  // 用户手册 5.1 故障 7: a cell's literal pipe is written \| and must not
  // split the cell or leave the backslash behind.
  const md = '| a \\| b | c |\n| --- | --- |\n| 1 | 2 |';
  const out = renderMarkdown(md);
  assert.ok(out.includes('<th>a | b</th>'), 'cell keeps \'a | b\' in one header cell');
  assert.ok(out.includes('<th>c</th>'), 'escaping consumed, next cell intact');
  assert.ok(out.includes('<td>1</td>') && out.includes('<td>2</td>'));
  assert.ok(!out.includes('\\'), 'no backslash survives the render');
  assert.equal((out.match(/<th>/g) || []).length, 2, 'exactly two header cells');
});

test('backslash-pair before a pipe is a separator, not an escape', () => {
  // `\\` collapses to one literal backslash; that pipe then splits cells.
  const out = renderMarkdown('| a \\\\| b |\n| --- | --- |');
  assert.ok(out.includes('<th>a \\</th>'), 'one literal backslash stays');
  assert.ok(out.includes('<th>b</th>'), 'trailing pipe still splits');
});

test('escaped pipe at row end does not create an empty last cell', () => {
  const out = renderMarkdown('| a | b \\|');
  assert.equal((out.match(/<th>/g) || []).length, 2, 'two cells, not three');
  assert.ok(out.includes('<th>a</th>') && out.includes('<th>b |</th>'));
});

test('escaped pipe stays XSS-safe inside a table cell', () => {
  const out = renderMarkdown('| <script>x\\|y</script> | unsafe |');
  assert.ok(!out.includes('<script>'), 'raw HTML remains inert');
  assert.ok(out.includes('&lt;script&gt;x|y&lt;/script&gt;'));
});

test('markdown of all block types stays balanced', () => {
  const md = '# T\n\npara with **bold**\n\n> quote\n\n```\ncode\n```\n\n- li1\n- li2\n\n---\n\ntable:\n\n| x |\n|---|\n| y |';
  const out = renderMarkdown(md);
  for (const tag of ['h1', 'p', 'blockquote', 'pre', 'code', 'ul', 'li', 'table', 'th', 'td']) {
    const re = new RegExp(`<${tag}>`, 'g');
    const opens = (out.match(re) || []).length;
    const closer = new RegExp(`</${tag}>`, 'g');
    const closes = (out.match(closer) || []).length;
    assert.equal(opens, closes, `unbalanced <${tag}>`);
  }
  assert.equal((out.match(/<hr>/g) || []).length, 1, 'one <hr> emitted');
});

test('empty input renders empty', () => {
  assert.equal(renderMarkdown(''), '');
  assert.equal(renderMarkdown('   '), '');
});
