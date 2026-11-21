// Language Server quota synchronization helper for the Context Monitor sidecar.
//
// Antigravity injects ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN into the
// sidecar process environment. This helper syncs RetrieveUserQuotaSummary into
// SQLite (quota_state) on startup, on DB write activity, or on API requests,
// with single-flight locking, 60s TTL, 30s error backoff, and write suppression.

import { existsSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { DEFAULT_CAPACITIES, getDefaultDbPath } from './query.mjs';

export function resolveLsCredentials(env = process.env) {
  const csrfToken = (env.AGY_CSRF_TOKEN || env.ANTIGRAVITY_CSRF_TOKEN || '').trim();
  if (!csrfToken) {
    return null;
  }

  let baseUrl = '';
  const envPort = (env.AGY_HTTP_PORT || '').trim();
  if (envPort && /^\d+$/.test(envPort)) {
    const portNum = Number(envPort);
    if (portNum >= 1 && portNum <= 65535) {
      baseUrl = `http://127.0.0.1:${portNum}`;
    }
  }

  if (!baseUrl) {
    const rawAddr = (env.ANTIGRAVITY_LS_ADDRESS || '').trim();
    if (rawAddr) {
      try {
        const parsed = new URL(rawAddr.includes('://') ? rawAddr : `http://${rawAddr}`);
        if (parsed.port) {
          baseUrl = `${parsed.protocol}//${parsed.host}`;
        }
      } catch {
        baseUrl = '';
      }
    }
  }

  if (!baseUrl) {
    return null;
  }
  return { baseUrl, csrfToken };
}

export function parseQuotaPayload(rawPayload, existingByBucket = new Map(), nowIso = new Date().toISOString()) {
  if (!rawPayload || typeof rawPayload !== 'object') {
    return [];
  }
  const responseData = (rawPayload.response && typeof rawPayload.response === 'object')
    ? rawPayload.response
    : rawPayload;
  const groups = Array.isArray(responseData.groups) ? responseData.groups : [];
  const rows = [];

  for (const grp of groups) {
    if (!grp || typeof grp !== 'object') continue;
    const groupName = String(grp.displayName || grp.group_name || '');
    const buckets = Array.isArray(grp.buckets) ? grp.buckets : [];
    for (const b of buckets) {
      if (!b || typeof b !== 'object') continue;
      const bucketId = String(b.bucketId || b.bucket_id || '').trim();
      if (!bucketId) continue;

      const rawRem = b.remainingFraction !== undefined && b.remainingFraction !== null
        ? b.remainingFraction
        : (b.remaining_fraction !== undefined && b.remaining_fraction !== null ? b.remaining_fraction : 1.0);
      const parsedRem = Number(rawRem);
      const remainingFraction = Number.isFinite(parsedRem) ? parsedRem : 1.0;

      const resetTime = String(b.resetTime || b.reset_time || '');
      const existing = existingByBucket.get(bucketId);
      const existingCap = existing && Number(existing.calibrated_capacity) > 0
        ? Number(existing.calibrated_capacity)
        : 0;
      const calibratedCapacity = existingCap || DEFAULT_CAPACITIES[bucketId] || (bucketId.includes('3p') ? 200.0 : 672.0);

      rows.push({
        bucket_id: bucketId,
        group_name: groupName,
        window: String(b.window || ''),
        remaining_fraction: remainingFraction,
        reset_time: resetTime,
        calibrated_capacity: calibratedCapacity,
        fetched_at: nowIso,
      });
    }
  }

  return rows;
}

export function needsQuotaSync(existingRows, ttlMs = 60_000, nowMs = Date.now()) {
  if (!Array.isArray(existingRows) || existingRows.length === 0) {
    return true;
  }
  for (const row of existingRows) {
    const fetchedAtStr = row?.fetched_at || row?.updated_at;
    if (!fetchedAtStr) {
      return true;
    }
    const fetchedMs = Date.parse(String(fetchedAtStr));
    if (!Number.isFinite(fetchedMs) || (nowMs - fetchedMs) > ttlMs) {
      return true;
    }
    const resetStr = row?.reset_time || row?.resetTime;
    if (resetStr) {
      const resetMs = Date.parse(String(resetStr));
      if (Number.isFinite(resetMs) && resetMs <= nowMs) {
        return true;
      }
    }
  }
  return false;
}

export function hasQuotaRowsChanged(newRows, existingByBucket) {
  if (!Array.isArray(newRows) || newRows.length === 0) {
    return false;
  }
  if (newRows.length !== existingByBucket.size) {
    return true;
  }
  for (const row of newRows) {
    const oldRow = existingByBucket.get(row.bucket_id);
    if (!oldRow) {
      return true;
    }
    if (Math.abs(Number(row.remaining_fraction) - Number(oldRow.remaining_fraction)) > 1e-4) {
      return true;
    }
    if (String(row.reset_time || '').trim() !== String(oldRow.reset_time || '').trim()) {
      return true;
    }
  }
  return false;
}

function readExistingQuotaRows(targetDbPath) {
  if (!targetDbPath || !existsSync(targetDbPath)) {
    return null;
  }
  let db = null;
  try {
    db = new DatabaseSync(targetDbPath, { readOnly: true });
    return db.prepare(
      'SELECT bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at FROM quota_state'
    ).all();
  } catch {
    return null;
  } finally {
    try { db?.close(); } catch {}
  }
}

function writeQuotaRows(targetDbPath, rows) {
  if (!targetDbPath || !existsSync(targetDbPath) || !rows.length) {
    return false;
  }
  let db = null;
  try {
    db = new DatabaseSync(targetDbPath);
    db.exec('PRAGMA busy_timeout = 5000;');
    const stmt = db.prepare(`
      INSERT INTO quota_state (
        bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(bucket_id) DO UPDATE SET
        group_name = excluded.group_name,
        window = excluded.window,
        remaining_fraction = excluded.remaining_fraction,
        reset_time = excluded.reset_time,
        calibrated_capacity = excluded.calibrated_capacity,
        fetched_at = excluded.fetched_at
    `);
    db.exec('BEGIN IMMEDIATE;');
    try {
      for (const r of rows) {
        stmt.run(
          r.bucket_id,
          r.group_name,
          r.window,
          r.remaining_fraction,
          r.reset_time,
          r.calibrated_capacity,
          r.fetched_at
        );
      }
      db.exec('COMMIT;');
      return true;
    } catch (err) {
      try { db.exec('ROLLBACK;'); } catch {}
      throw err;
    }
  } catch {
    return false;
  } finally {
    try { db?.close(); } catch {}
  }
}

export function createLsQuotaSyncer(options = {}) {
  const {
    dbPath,
    ttlMs = 60_000,
    errorBackoffMs = 30_000,
    timeoutMs = 1500,
    fetchImpl = globalThis.fetch,
    env = process.env,
    onWrite,
  } = options;

  let inFlight = null;
  let lastAttemptMs = 0;
  let lastHadError = false;

  async function syncIfNeeded({ force = false } = {}) {
    if (typeof fetchImpl !== 'function') {
      return false;
    }
    const creds = resolveLsCredentials(env);
    if (!creds) {
      return false;
    }

    if (inFlight) {
      return inFlight;
    }

    const nowMs = Date.now();
    const minWaitMs = lastHadError ? errorBackoffMs : ttlMs;
    if (!force && lastAttemptMs > 0 && (nowMs - lastAttemptMs) < minWaitMs) {
      return false;
    }

    inFlight = (async () => {
      const targetDbPath = dbPath || getDefaultDbPath();
      const existingRows = readExistingQuotaRows(targetDbPath);
      if (existingRows === null) {
        return false;
      }
      if (!force && !needsQuotaSync(existingRows, ttlMs, Date.now())) {
        lastAttemptMs = Date.now();
        lastHadError = false;
        return false;
      }

      lastAttemptMs = Date.now();
      const existingByBucket = new Map(existingRows.map((r) => [r.bucket_id, r]));

      try {
        const res = await fetchImpl(
          `${creds.baseUrl}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'x-codeium-csrf-token': creds.csrfToken,
            },
            body: '{}',
            signal: typeof AbortSignal?.timeout === 'function' ? AbortSignal.timeout(timeoutMs) : undefined,
          }
        );

        if (!res || !res.ok) {
          lastHadError = true;
          return false;
        }

        const payload = await res.json();
        const rows = parseQuotaPayload(payload, existingByBucket, new Date().toISOString());
        if (!rows.length) {
          lastHadError = true;
          return false;
        }

        lastHadError = false;
        if (!hasQuotaRowsChanged(rows, existingByBucket)) {
          return false;
        }

        const wrote = writeQuotaRows(targetDbPath, rows);
        if (wrote && typeof onWrite === 'function') {
          onWrite();
        }
        return wrote;
      } catch {
        lastHadError = true;
        return false;
      }
    })();

    try {
      return await inFlight;
    } finally {
      inFlight = null;
    }
  }

  return { syncIfNeeded };
}
