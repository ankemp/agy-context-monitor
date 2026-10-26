// Backend engine for the Antigravity Context Monitor side panel extension.
//
// Antigravity starts this process with Node.js (see sidecar.json) and sets
// ANTIGRAVITY_SIDECAR_WEB_PORT; SidecarApp listens on it automatically. The
// 'sidecar_sdk' import is resolved natively by Antigravity at runtime.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Response, SidecarApp } from 'sidecar_sdk';
import { getDatabase, getSessionData, getGlobalData, getConversationFingerprint, clearQueryCache } from './query.mjs';
import { createSseHub, attachSseRoute } from './sse.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const app = new SidecarApp();

// Static file routes
const readLocal = (name) => readFileSync(join(HERE, name), 'utf8');

app.page('/', () => readLocal('index.html'));
app.page('/app.js', () => readLocal('app.js'));
app.api('/styles.css', () => new Response(readLocal('styles.css'), { contentType: 'text/css' }), 'GET');

// API routes: resolve DB handle on demand to recover gracefully if DB initializes after boot
app.api('/api/session', (data) => getSessionData(getDatabase(), data?.cid, data?.inspect_cid), 'GET');
app.api('/api/global', () => getGlobalData(getDatabase()), 'GET');

const sseHub = createSseHub({
  getDb: getDatabase,
  getFingerprint: getConversationFingerprint,
  onCacheInvalidate: clearQueryCache,
});

const server = await app.run();
attachSseRoute(server, sseHub);
