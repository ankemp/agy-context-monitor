// Server-Sent Events (SSE) stream hub for the Antigravity Context Monitor sidecar.
//
// Invariant: Zero polling.
// Clients subscribe to /api/events?cid=<cid>&inspect_cid=<inspectCid>.
// The hub watches SQLite WAL file stats only while clients are active,
// invalidates query caches on change, and pushes 'update' events strictly
// to clients whose subscribed conversation or swarm fleet actually changed.

import { statSync } from 'node:fs';
import { getDefaultDbPath, getDatabase, getConversationFingerprint, clearQueryCache } from './query.mjs';

function getFileStats(filePath) {
  if (!filePath) return null;
  try {
    const fileStat = statSync(filePath, { throwIfNoEntry: false });
    return fileStat ? { mtimeMs: fileStat.mtimeMs, size: fileStat.size } : null;
  } catch {
    return null;
  }
}

class SseClient {
  constructor(id, req, res, cid, inspectCid, initialFingerprint) {
    this.id = id;
    this.req = req;
    this.res = res;
    this.cid = cid;
    this.inspectCid = inspectCid;
    this.lastFingerprint = initialFingerprint;
  }

  sendComment(comment) {
    if (!this.res.writableEnded && this.res.writable) {
      try {
        this.res.write(`: ${comment}\n\n`);
      } catch {}
    }
  }

  sendEvent(eventName, payload) {
    if (!this.res.writableEnded && this.res.writable) {
      try {
        const dataStr = typeof payload === 'string' ? payload : JSON.stringify(payload);
        this.res.write(`event: ${eventName}\ndata: ${dataStr}\n\n`);
      } catch {}
    }
  }

  close() {
    try {
      if (!this.res.writableEnded) {
        this.res.end();
      }
    } catch {}
  }
}

export function createSseHub(options = {}) {
  const {
    getDb = getDatabase,
    getFingerprint = getConversationFingerprint,
    onCacheInvalidate = clearQueryCache,
    checkIntervalMs = 500,
    settleWindowMs = 300,
    heartbeatMs = 25000,
    dbPath = getDefaultDbPath(),
  } = options;

  const walPath = dbPath ? `${dbPath}-wal` : null;
  const clients = new Set();

  let pollTimer = null;
  let settleTimer = null;
  let heartbeatTimer = null;

  let lastDbStats = getFileStats(dbPath);
  let lastWalStats = getFileStats(walPath);

  function startWatcher() {
    if (pollTimer) return;
    lastDbStats = getFileStats(dbPath);
    lastWalStats = getFileStats(walPath);

    pollTimer = setInterval(pollFileStats, checkIntervalMs);
    heartbeatTimer = setInterval(sendHeartbeat, heartbeatMs);
  }

  function stopWatcher() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (settleTimer) {
      clearTimeout(settleTimer);
      settleTimer = null;
    }
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function sendHeartbeat() {
    for (const client of clients) {
      client.sendComment('ping');
    }
  }

  function pollFileStats() {
    const curDbStats = getFileStats(dbPath);
    const curWalStats = getFileStats(walPath);

    const dbChanged = !lastDbStats !== !curDbStats || (curDbStats && (curDbStats.mtimeMs !== lastDbStats?.mtimeMs || curDbStats.size !== lastDbStats?.size));
    const walChanged = !lastWalStats !== !curWalStats || (curWalStats && (curWalStats.mtimeMs !== lastWalStats?.mtimeMs || curWalStats.size !== lastWalStats?.size));

    if (dbChanged || walChanged) {
      lastDbStats = curDbStats;
      lastWalStats = curWalStats;

      if (settleTimer) clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        settleTimer = null;
        checkChanges();
      }, settleWindowMs);
    }
  }

  async function checkChanges() {
    try {
      if (onCacheInvalidate) {
        onCacheInvalidate();
      }

      if (clients.size === 0) return;

      const db = getDb?.();
      if (!db) return;

      // Group clients by unique (cid, inspectCid) pair to avoid redundant fingerprint computations
      const groupMap = new Map();
      for (const client of clients) {
        const key = `${client.cid || ''}::${client.inspectCid || ''}`;
        if (!groupMap.has(key)) {
          groupMap.set(key, []);
        }
        groupMap.get(key).push(client);
      }

      for (const [key, groupClients] of groupMap.entries()) {
        const sample = groupClients[0];
        let currentFp;
        try {
          currentFp = getFingerprint(db, sample.cid, sample.inspectCid);
        } catch (err) {
          console.error(`[sse] Error evaluating fingerprint for ${key}:`, err.message);
          continue;
        }

        for (const client of groupClients) {
          if (client.lastFingerprint !== currentFp) {
            client.lastFingerprint = currentFp;
            client.sendEvent('update', {
              cid: client.cid,
              inspect_cid: client.inspectCid,
              timestamp: new Date().toISOString(),
            });
          }
        }
      }
    } catch (err) {
      console.error('[sse] Error during checkChanges:', err);
    }
  }

  function handleRequest(req, res, url) {
    if (req.method !== 'GET') {
      res.writeHead(405, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Method Not Allowed' }));
      return;
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write(': connected\n\n');

    const cid = url.searchParams.get('cid') || null;
    const inspectCid = url.searchParams.get('inspect_cid') || null;

    let initialFp = '';
    const db = getDb?.();
    if (db) {
      try {
        initialFp = getFingerprint(db, cid, inspectCid);
      } catch (err) {
        console.error(`[sse] Error evaluating initial fingerprint for cid=${cid}:`, err.message);
      }
    }

    const client = new SseClient(
      Math.random().toString(36).slice(2),
      req,
      res,
      cid,
      inspectCid,
      initialFp,
    );

    clients.add(client);

    if (clients.size === 1) {
      startWatcher();
    }

    const cleanup = () => {
      if (!clients.has(client)) return;
      clients.delete(client);
      if (clients.size === 0) {
        stopWatcher();
      }
    };

    req.on('close', cleanup);
    res.on('close', cleanup);
    res.on('error', cleanup);
  }

  function close() {
    stopWatcher();
    for (const client of clients) {
      client.close();
    }
    clients.clear();
  }

  return {
    handleRequest,
    checkChanges,
    close,
    get activeClientCount() {
      return clients.size;
    },
    get isWatcherRunning() {
      return pollTimer !== null;
    },
  };
}

export function attachSseRoute(server, hub) {
  const existingListeners = server.listeners('request').slice();
  server.removeAllListeners('request');
  server.on('request', (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/api/events') {
      hub.handleRequest(req, res, url);
      return;
    }
    for (const listener of existingListeners) {
      listener.call(server, req, res);
    }
  });
}
