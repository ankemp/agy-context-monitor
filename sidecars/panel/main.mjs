// Backend engine for the Antigravity Context Monitor side panel extension.
//
// Antigravity starts this process with Node.js (see sidecar.json) and sets
// ANTIGRAVITY_SIDECAR_WEB_PORT; SidecarApp listens on it automatically. The
// 'sidecar_sdk' import is resolved natively by Antigravity at runtime.
// Outside Antigravity (when ANTIGRAVITY_SIDECAR_WEB_PORT is unset), falls back
// to a standalone node:http dev server on 127.0.0.1.

import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getDatabase, getSessionData, getGlobalData, getConversationFingerprint, clearQueryCache } from './query.mjs';
import { createSseHub, attachSseRoute } from './sse.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const isHostRuntime = Boolean(process.env.ANTIGRAVITY_SIDECAR_WEB_PORT);
const dbPath = process.env.CONTEXT_MONITOR_DB || process.env.AGY_ANALYTICS_DB || undefined;

const STATIC_ROUTES = {
  '/': { file: 'index.html', contentType: 'text/html; charset=utf-8' },
  '/app.js': { file: 'app.js', contentType: 'application/javascript; charset=utf-8' },
  '/styles.css': { file: 'styles.css', contentType: 'text/css; charset=utf-8' },
};

const MOCK_PRELOAD_JS = [
  '// Standalone local-dev mock for Antigravity sidecar preload.js',
  'window.sidecar = {',
  "  conversationId: new URLSearchParams(location.search).get('conversationId') || '',",
  '  fetch: (u, o) => fetch(u, o),',
  '  getWorkspaceUris: async () => [],',
  '  onWorkspaceChange: () => () => {},',
  '};',
  '',
].join('\n');

const readLocal = (name) => readFileSync(join(HERE, name), 'utf8');

// Resolve DB handle on demand to recover gracefully if DB initializes after boot.
const resolveDb = () => getDatabase(dbPath);
const getSessionPayload = (cid, inspectCid) => getSessionData(resolveDb(), cid, inspectCid);
const getGlobalPayload = () => getGlobalData(resolveDb());

const sseHub = createSseHub({
  getDb: resolveDb,
  getFingerprint: getConversationFingerprint,
  onCacheInvalidate: clearQueryCache,
  dbPath,
});

function sendText(res, statusCode, contentType, body) {
  res.writeHead(statusCode, {
    'Content-Type': contentType,
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function sendJson(res, statusCode, payload) {
  sendText(res, statusCode, 'application/json; charset=utf-8', JSON.stringify(payload));
}

function resolveStandalonePort() {
  const rawPort = process.env.PORT;
  if (rawPort === undefined || rawPort === '') {
    return 4173;
  }
  const parsed = Number(rawPort);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 65535 ? parsed : 4173;
}

function handleStandaloneRequest(req, res) {
  try {
    if (req.method !== 'GET') {
      sendJson(res, 405, { error: 'Method Not Allowed' });
      return;
    }

    const url = new URL(req.url || '/', 'http://127.0.0.1');

    if (url.pathname === '/preload.js') {
      sendText(res, 200, 'application/javascript; charset=utf-8', MOCK_PRELOAD_JS);
      return;
    }

    const staticRoute = STATIC_ROUTES[url.pathname];
    if (staticRoute) {
      sendText(res, 200, staticRoute.contentType, readLocal(staticRoute.file));
      return;
    }

    if (url.pathname === '/api/session') {
      const cid = url.searchParams.get('cid');
      const inspectCid = url.searchParams.get('inspect_cid');
      sendJson(res, 200, getSessionPayload(cid, inspectCid));
      return;
    }

    if (url.pathname === '/api/global') {
      sendJson(res, 200, getGlobalPayload());
      return;
    }

    sendJson(res, 404, { error: 'Not Found', path: url.pathname });
  } catch (err) {
    if (!res.headersSent) {
      sendJson(res, 500, { error: err?.message || 'Internal Server Error' });
    } else if (!res.writableEnded) {
      res.end();
    }
  }
}

async function startHostRuntime() {
  const { Response, SidecarApp } = await import('sidecar_sdk');
  const app = new SidecarApp();

  app.page('/', () => readLocal('index.html'));
  app.page('/app.js', () => readLocal('app.js'));
  app.page('/styles.css', () => new Response(readLocal('styles.css'), { contentType: 'text/css' }));

  app.api('/api/session', (data) => getSessionPayload(data?.cid, data?.inspect_cid), 'GET');
  app.api('/api/global', () => getGlobalPayload(), 'GET');

  const server = await app.run();
  attachSseRoute(server, sseHub);
}

async function startStandaloneServer() {
  const server = createServer(handleStandaloneRequest);
  // Register SSE wrapper after createServer so /api/events intercepts before handleStandaloneRequest.
  attachSseRoute(server, sseHub);

  const shutdown = () => {
    sseHub.close();
    server.closeAllConnections?.();
    server.close(() => process.exit(0));
  };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);

  const port = resolveStandalonePort();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      server.off('error', reject);
      const addr = server.address();
      const boundPort = typeof addr === 'object' && addr ? addr.port : port;
      console.log(`Context Monitor standalone server listening on http://127.0.0.1:${boundPort}`);
      resolve();
    });
  });
}

if (isHostRuntime) {
  await startHostRuntime();
} else {
  await startStandaloneServer();
}
