// demo.mjs — minimal static/offline demo server + a small /api/health endpoint.
// No personal data leaves the browser; everything runs client-side.

import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const ROOT = new URL('../', import.meta.url).pathname; // repo root
const PUBLIC = new URL('../public/', import.meta.url).pathname;
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
};

const server = createServer((req, res) => {
  if (req.url.startsWith('/api/health')) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true, app: 'local-kb', offline: true }));
    return;
  }
  let pathname = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (pathname === '/') pathname = '/index.html';
  // Resolve against repo root, preferring public/ (which hosts index.html,
  // main.js, idb.js) while still serving src/ for the module graph.
  const filePath = normalize(join(ROOT, pathname));
  const inRoot = filePath.startsWith(ROOT);
  const publicPath = normalize(join(PUBLIC, pathname));
  const inPublic = publicPath.startsWith(PUBLIC);
  const chosen = inPublic && existsSync(publicPath) ? publicPath
    : inRoot && existsSync(filePath) ? filePath
    : null;
  if (!chosen) {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found (offline demo)');
    return;
  }
  const mime = MIME[extname(chosen).toLowerCase()] || 'application/octet-stream';
  res.writeHead(200, { 'content-type': mime });
  res.end(readFileSync(chosen));
});

const PORT = process.env.PORT || 4242;
server.listen(PORT, () => console.log(`local-kb demo http://localhost:${PORT}`));
