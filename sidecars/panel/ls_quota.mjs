// Language Server quota synchronization helper for the Context Monitor sidecar.
//
// Antigravity injects ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN into the
// sidecar process environment. This helper syncs RetrieveUserQuotaSummary into
// SQLite (quota_state) on startup, on DB write activity, or on API requests,
// with single-flight locking, 60s TTL, 30s error backoff, and write suppression.

import { existsSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { DEFAULT_CAPACITIES, getDefaultDbPath } from './query.mjs';

const IDLE_REMAINING_THRESHOLD = 0.99;
const MIN_REMAINING_JUMP = 0.05;
const FULL_RESET_REMAINING_FLOOR = 0.90;
const SCHEDULED_TOLERANCE_MS = 900_000;
const RESET_TIME_SHIFT_THRESHOLD_MS = 900_000;
const SMALL_POSITIVE_DELTA_THRESHOLD = 0.005;

function parseUtcMs(value) {
  if (value === null || value === undefined) {
    return NaN;
  }
  if (value instanceof Date) {
    return value.getTime();
  }
  let str = String(value).trim();
  if (!str) {
    return NaN;
  }
  str = str.replace(/^(\d{4}-\d{2}-\d{2})\s+/, '$1T');
  if (/[zZ]$/.test(str)) {
    str = str.slice(0, -1) + 'Z';
  } else if (/T\d{2}:\d{2}/.test(str) && !/[+-]\d{2}(?::?\d{2})?$/.test(str)) {
    str += 'Z';
  }
  const ms = Date.parse(str);
  return Number.isFinite(ms) ? ms : NaN;
}

function resolveDetectedIso(detectedAtIso) {
  if (detectedAtIso instanceof Date) {
    const ms = detectedAtIso.getTime();
    if (Number.isFinite(ms)) {
      return { detectedIso: detectedAtIso.toISOString(), detectedMs: ms };
    }
  } else if (detectedAtIso !== null && detectedAtIso !== undefined) {
    const trimmed = String(detectedAtIso).trim();
    if (trimmed) {
      const ms = parseUtcMs(trimmed);
      if (Number.isFinite(ms)) {
        return { detectedIso: trimmed, detectedMs: ms };
      }
    }
  }
  const nowMs = Date.now();
  return { detectedIso: new Date(nowMs).toISOString(), detectedMs: nowMs };
}

export function detectQuotaResetEvent(
  oldRow,
  newRow,
  detectedAtIso = new Date().toISOString(),
  source = 'sidecar',
) {
  if (!oldRow || typeof oldRow !== 'object' || !newRow || typeof newRow !== 'object') {
    return null;
  }

  const bucketId = String(newRow.bucket_id ?? newRow.bucketId ?? oldRow.bucket_id ?? oldRow.bucketId ?? '').trim();
  if (!bucketId) {
    return null;
  }

  const oldResetTime = String(oldRow.reset_time ?? oldRow.resetTime ?? '').trim();
  if (!oldResetTime) {
    return null;
  }

  const rawOldRem = oldRow.remaining_fraction ?? oldRow.remainingFraction;
  const rawNewRem = newRow.remaining_fraction ?? newRow.remainingFraction;
  if (
    rawOldRem === null || rawOldRem === undefined || rawOldRem === '' || typeof rawOldRem === 'boolean'
    || rawNewRem === null || rawNewRem === undefined || rawNewRem === '' || typeof rawNewRem === 'boolean'
  ) {
    return null;
  }

  const oldRem = Number(rawOldRem);
  const newRem = Number(rawNewRem);
  if (!Number.isFinite(oldRem) || !Number.isFinite(newRem)) {
    return null;
  }

  if (oldRem >= IDLE_REMAINING_THRESHOLD) {
    return null;
  }

  const { detectedIso, detectedMs } = resolveDetectedIso(detectedAtIso);
  const newResetTime = String(newRow.reset_time ?? newRow.resetTime ?? '').trim();
  const oldResetMs = parseUtcMs(oldResetTime);
  const newResetMs = parseUtcMs(newResetTime);
  const oldFetchedMs = parseUtcMs(
    oldRow.fetched_at ?? oldRow.fetchedAt ?? oldRow.updated_at ?? oldRow.updatedAt ?? ''
  );

  const delta = newRem - oldRem;
  const bothResetsFinite = Number.isFinite(newResetMs) && Number.isFinite(oldResetMs);
  const resetAdvanced = bothResetsFinite && (newResetMs - oldResetMs) > RESET_TIME_SHIFT_THRESHOLD_MS;
  // 1e-9 tolerance prevents IEEE-754 representation artifacts (e.g. 0.35 - 0.30 === 0.04999999999999999)
  const deltaJump = (delta + 1e-9) >= MIN_REMAINING_JUMP;

  const triggered = (
    deltaJump
    || (resetAdvanced && (oldResetMs <= detectedMs + SCHEDULED_TOLERANCE_MS || delta > SMALL_POSITIVE_DELTA_THRESHOLD))
  );
  if (!triggered) {
    return null;
  }

  let eventType = null;
  let resetAt = detectedIso;

  if (Number.isFinite(oldResetMs) && detectedMs >= (oldResetMs - SCHEDULED_TOLERANCE_MS)) {
    eventType = 'scheduled';
    resetAt = oldResetTime;
  } else {
    const absShiftOverThreshold = bothResetsFinite && Math.abs(newResetMs - oldResetMs) > RESET_TIME_SHIFT_THRESHOLD_MS;
    if (newRem >= FULL_RESET_REMAINING_FLOOR || absShiftOverThreshold) {
      eventType = 'early_reset';
      const windowMs = bucketId.toLowerCase().includes('5h')
        ? 5 * 3600 * 1000
        : 7 * 24 * 3600 * 1000;
      const inferredMs = Number.isFinite(newResetMs) ? (newResetMs - windowMs) : NaN;
      if (
        Number.isFinite(inferredMs)
        && inferredMs <= detectedMs
        && (!Number.isFinite(oldFetchedMs) || inferredMs >= oldFetchedMs)
      ) {
        resetAt = new Date(inferredMs).toISOString();
      } else {
        resetAt = detectedIso;
      }
    } else if (
      deltaJump
      && newRem < FULL_RESET_REMAINING_FLOOR
      && (!bothResetsFinite || Math.abs(newResetMs - oldResetMs) <= RESET_TIME_SHIFT_THRESHOLD_MS)
    ) {
      eventType = 'top_up';
      resetAt = detectedIso;
    } else {
      return null;
    }
  }

  const groupName = String(
    newRow.group_name ?? newRow.groupName ?? newRow.displayName
    ?? oldRow.group_name ?? oldRow.groupName ?? oldRow.displayName ?? ''
  );
  const win = String(newRow.window ?? oldRow.window ?? '');
  const resolvedSource = String(source || 'sidecar').trim() || 'sidecar';

  return {
    bucket_id: bucketId,
    group_name: groupName,
    window: win,
    event_type: eventType,
    reset_at: resetAt,
    detected_at: detectedIso,
    old_remaining_fraction: oldRem,
    new_remaining_fraction: newRem,
    delta_fraction: Number(delta.toFixed(8)),
    old_reset_time: oldResetTime,
    new_reset_time: newResetTime,
    source: resolvedSource,
  };
}

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

function recordQuotaResetEventsInTx(db, rows, fallbackNowIso) {
  const hasResetTable = Boolean(
    db.prepare(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quota_reset_events' LIMIT 1"
    ).get()
  );
  if (!hasResetTable) {
    return;
  }

  // Re-read live quota_state inside BEGIN IMMEDIATE so concurrent writers cannot double-detect resets.
  const liveRows = db.prepare(
    'SELECT bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at FROM quota_state'
  ).all();
  const liveByBucket = new Map(liveRows.map((row) => [row.bucket_id, row]));

  const dedupStmt = db.prepare(`
    SELECT 1 FROM quota_reset_events
    WHERE bucket_id = ?
      AND event_type = ?
      AND new_reset_time = ?
      AND ABS(strftime('%s', detected_at) - strftime('%s', ?)) <= 300
    LIMIT 1
  `);
  const insertEventStmt = db.prepare(`
    INSERT INTO quota_reset_events (
      bucket_id,
      group_name,
      window,
      event_type,
      reset_at,
      detected_at,
      old_remaining_fraction,
      new_remaining_fraction,
      delta_fraction,
      old_reset_time,
      new_reset_time,
      source
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  for (const r of rows) {
    if (!r || typeof r !== 'object' || !r.bucket_id) {
      continue;
    }
    const oldRow = liveByBucket.get(r.bucket_id) || null;
    const event = detectQuotaResetEvent(
      oldRow,
      r,
      r.fetched_at || fallbackNowIso,
      'sidecar',
    );
    if (!event) {
      continue;
    }
    const dup = dedupStmt.get(
      event.bucket_id,
      event.event_type,
      event.new_reset_time,
      event.detected_at,
    );
    if (dup) {
      continue;
    }
    insertEventStmt.run(
      event.bucket_id,
      event.group_name,
      event.window,
      event.event_type,
      event.reset_at,
      event.detected_at,
      event.old_remaining_fraction,
      event.new_remaining_fraction,
      event.delta_fraction,
      event.old_reset_time,
      event.new_reset_time,
      event.source,
    );
  }
}

function upsertQuotaStateInTx(db, rows, fallbackNowIso) {
  const upsertStmt = db.prepare(`
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
  for (const r of rows) {
    if (!r || typeof r !== 'object' || !r.bucket_id) {
      continue;
    }
    upsertStmt.run(
      String(r.bucket_id),
      String(r.group_name ?? ''),
      String(r.window ?? ''),
      Number.isFinite(Number(r.remaining_fraction)) ? Number(r.remaining_fraction) : 1.0,
      String(r.reset_time ?? ''),
      Number.isFinite(Number(r.calibrated_capacity)) ? Number(r.calibrated_capacity) : 0.0,
      String(r.fetched_at || fallbackNowIso),
    );
  }
}

export function writeQuotaRows(targetDbPath, rows) {
  if (!targetDbPath || !existsSync(targetDbPath) || !Array.isArray(rows) || !rows.length) {
    return false;
  }
  let db = null;
  try {
    db = new DatabaseSync(targetDbPath);
    db.exec('PRAGMA busy_timeout = 5000;');
    db.exec('PRAGMA foreign_keys = ON;');
    db.exec('BEGIN IMMEDIATE;');
    try {
      const fallbackNowIso = new Date().toISOString();
      recordQuotaResetEventsInTx(db, rows, fallbackNowIso);
      upsertQuotaStateInTx(db, rows, fallbackNowIso);
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
