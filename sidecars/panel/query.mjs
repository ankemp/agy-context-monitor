import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const MODEL_LIMIT_RULES = [
  { match: ['pro'], limit: 2000000 },
  { match: ['argon', 'flash', 'gemini'], limit: 1000000 },
  { match: ['claude', 'sonnet', 'opus'], limit: 200000 },
  { match: ['gpt', 'o1', 'o3'], limit: 128000 }
];

export const DEFAULT_CAPACITIES = {
  'gemini-weekly': 672.0,
  'gemini-5h': 236.7,
  '3p-weekly': 200.0,
  '3p-5h': 70.0
};

export const DEFAULT_PEAK_BILLED_TPM = 100000.0;
export const DEFAULT_PEAK_CONTEXT_TPM = 10000.0;
export const DEFAULT_PEAK_BILLED_TPH = 2500000.0;
export const DEFAULT_PEAK_CONTEXT_TPH = 250000.0;

export function resolveFleetIds(db, rootId) {
  if (!db || !rootId) return [rootId].filter(Boolean);
  try {
    const rows = db.prepare(`
      WITH RECURSIVE fleet(id) AS (
        SELECT id FROM conversations WHERE id = ? OR root_id = ? OR parent_id = ?
        UNION
        SELECT c.id FROM conversations c JOIN fleet f ON c.parent_id = f.id OR c.root_id = f.id
      )
      SELECT DISTINCT id FROM fleet
    `).all(rootId, rootId, rootId);
    if (rows && rows.length > 0) {
      return rows.map(r => String(r.id)).filter(Boolean);
    }
  } catch {
    // Recursive CTE or columns might not be supported in older schemas
  }
  try {
    const rows = db.prepare('SELECT DISTINCT id FROM conversations WHERE root_id = ? OR id = ?').all(rootId, rootId);
    if (rows && rows.length > 0) {
      return rows.map(r => String(r.id)).filter(Boolean);
    }
  } catch {
    // fallback
  }
  return [String(rootId)];
}

function calcP95(vals) {
  if (!vals || vals.length === 0) return 0.0;
  const sorted = [...vals].sort((a, b) => a - b);
  const k = (sorted.length - 1) * 0.95;
  const f = Math.floor(k);
  const c = Math.ceil(k);
  if (f === c) return Number(sorted[f]);
  const d0 = sorted[f] * (c - k);
  const d1 = sorted[c] * (k - f);
  return Number(d0 + d1);
}

export function queryVelocityMetrics(db, options = {}) {
  if (!db) {
    return {
      rolling5mContextTpm: 0.0,
      rolling5mBilledTpm: 0.0,
      rolling60mContextTph: 0.0,
      rolling60mBilledTph: 0.0,
      activeContextTpm: 0.0,
      activeBilledTpm: 0.0,
      pairingBilledTph: 0.0,
      peakBilledTpm: DEFAULT_PEAK_BILLED_TPM,
      peakContextTpm: DEFAULT_PEAK_CONTEXT_TPM,
      arcPct: 0.0,
      contextFillPct: 0.0
    };
  }

  const opts = (typeof options === 'string') ? { conversationId: options } : (options || {});
  const conversationId = opts.conversationId || opts.conversation_id || null;
  const rootId = opts.rootId || opts.root_id || null;
  const rawNow = opts.now || Date.now();
  let nowDt = (rawNow instanceof Date) ? rawNow : new Date(rawNow);
  if (isNaN(nowDt.getTime())) {
    nowDt = new Date();
  }
  const isoNow = nowDt.toISOString().replace(/\.\d{3}Z$/, 'Z').slice(0, 19);
  const t5m = new Date(nowDt.getTime() - 5 * 60 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z').slice(0, 19);
  const t60m = new Date(nowDt.getTime() - 60 * 60 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z').slice(0, 19);
  const nowHour = isoNow.slice(0, 13);

  // 1. Resolve scope
  let scopeStepsSql = '';
  let scopeStepsParams = [];
  let scopeConvSql = '';
  let scopeConvParams = [];

  if (conversationId) {
    scopeStepsSql = ' AND conversation_id = ?';
    scopeStepsParams = [conversationId];
    scopeConvSql = ' WHERE id = ?';
    scopeConvParams = [conversationId];
  } else if (rootId) {
    const fleetIds = resolveFleetIds(db, rootId);
    const placeholders = fleetIds.map(() => '?').join(',');
    scopeStepsSql = ` AND conversation_id IN (${placeholders})`;
    scopeStepsParams = fleetIds;
    scopeConvSql = ` WHERE id IN (${placeholders})`;
    scopeConvParams = fleetIds;
  }

  // 2. Live rolling windows from steps
  let rolling5mContext = 0;
  let rolling5mBilled = 0;
  let rolling60mContext = 0;
  let rolling60mBilled = 0;

  try {
    const rollingSql = `
      SELECT
        COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN est_tokens ELSE 0 END), 0) AS c_5m,
        COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN (COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) ELSE 0 END), 0) AS b_5m,
        COALESCE(SUM(est_tokens), 0) AS c_60m,
        COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS b_60m
      FROM steps
      WHERE created_at IS NOT NULL AND created_at != ''
        AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
        AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
        ${scopeStepsSql}
    `;
    const row = db.prepare(rollingSql).get(t5m, t5m, t60m, isoNow, ...scopeStepsParams);
    if (row) {
      rolling5mContext = Number(row.c_5m) || 0;
      rolling5mBilled = Number(row.b_5m) || 0;
      rolling60mContext = Number(row.c_60m) || 0;
      rolling60mBilled = Number(row.b_60m) || 0;
    }
  } catch {
    // If billed_output_tokens column is missing, try fallback with only billed_input_tokens
    try {
      const fallbackSql = `
        SELECT
          COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN est_tokens ELSE 0 END), 0) AS c_5m,
          COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN COALESCE(billed_input_tokens, 0) ELSE 0 END), 0) AS b_5m,
          COALESCE(SUM(est_tokens), 0) AS c_60m,
          COALESCE(SUM(COALESCE(billed_input_tokens, 0)), 0) AS b_60m
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
          AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
          AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
          ${scopeStepsSql}
      `;
      const row = db.prepare(fallbackSql).get(t5m, t5m, t60m, isoNow, ...scopeStepsParams);
      if (row) {
        rolling5mContext = Number(row.c_5m) || 0;
        rolling5mBilled = Number(row.b_5m) || 0;
        rolling60mContext = Number(row.c_60m) || 0;
        rolling60mBilled = Number(row.b_60m) || 0;
      }
    } catch {
      try {
        const minimalSql = `
          SELECT
            COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN est_tokens ELSE 0 END), 0) AS c_5m,
            COALESCE(SUM(est_tokens), 0) AS c_60m
          FROM steps
          WHERE created_at IS NOT NULL AND created_at != ''
            AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
            AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
            ${scopeStepsSql}
        `;
        const row = db.prepare(minimalSql).get(t5m, t60m, isoNow, ...scopeStepsParams);
        if (row) {
          rolling5mContext = Number(row.c_5m) || 0;
          rolling60mContext = Number(row.c_60m) || 0;
        }
      } catch {
        // steps table might not exist
      }
    }
  }

  const rolling5mContextTpm = Math.round((rolling5mContext / 5.0) * 100) / 100;
  const rolling5mBilledTpm = Math.round((rolling5mBilled / 5.0) * 100) / 100;
  const rolling60mContextTph = Math.round(Number(rolling60mContext) * 100) / 100;
  const rolling60mBilledTph = Math.round(Number(rolling60mBilled) * 100) / 100;

  // 3. Active-time velocity
  let totalEstTokens = 0;
  let totalBilledTokens = 0;
  let activeAgentSec = 0.0;
  let activePairingSec = 0.0;

  try {
    const convRow = db.prepare(`
      SELECT
        COALESCE(SUM(active_agent_seconds), 0.0) as active_agent_seconds,
        COALESCE(SUM(active_pairing_seconds), 0.0) as active_pairing_seconds,
        COALESCE(SUM(total_tokens), 0) as total_tokens,
        COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) as billed_tokens
      FROM conversations
      ${scopeConvSql}
    `).get(...scopeConvParams);

    if (convRow) {
      totalEstTokens = Number(convRow.total_tokens) || 0;
      totalBilledTokens = Number(convRow.billed_tokens) || 0;
      activeAgentSec = Number(convRow.active_agent_seconds) || 0.0;
      activePairingSec = Number(convRow.active_pairing_seconds) || 0.0;
    }
  } catch {
    try {
      const convRow = db.prepare(`
        SELECT
          COALESCE(SUM(active_agent_seconds), 0.0) as active_agent_seconds,
          COALESCE(SUM(total_tokens), 0) as total_tokens
        FROM conversations
        ${scopeConvSql}
      `).get(...scopeConvParams);
      if (convRow) {
        totalEstTokens = Number(convRow.total_tokens) || 0;
        activeAgentSec = Number(convRow.active_agent_seconds) || 0.0;
      }
    } catch {
      // conversations table missing or columns missing
    }
  }

  // Fallback to steps if conversations has 0 tokens but steps has them (independent fallbacks)
  if (totalEstTokens === 0 || totalBilledTokens === 0) {
    try {
      const stepRow = db.prepare(`
        SELECT
          COALESCE(SUM(est_tokens), 0) as est_tok,
          COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) as bld_tok
        FROM steps
        WHERE 1=1 ${scopeStepsSql}
      `).get(...scopeStepsParams);
      if (stepRow) {
        if (totalEstTokens === 0) totalEstTokens = Number(stepRow.est_tok) || 0;
        if (totalBilledTokens === 0) totalBilledTokens = Number(stepRow.bld_tok) || 0;
      }
    } catch {
      try {
        const stepRow = db.prepare(`
          SELECT
            COALESCE(SUM(est_tokens), 0) as est_tok,
            COALESCE(SUM(COALESCE(billed_input_tokens, 0)), 0) as bld_tok
          FROM steps
          WHERE 1=1 ${scopeStepsSql}
        `).get(...scopeStepsParams);
        if (stepRow) {
          if (totalEstTokens === 0) totalEstTokens = Number(stepRow.est_tok) || 0;
          if (totalBilledTokens === 0) totalBilledTokens = Number(stepRow.bld_tok) || 0;
        }
      } catch {
        // fallback
      }
    }
  }

  function calcRate(tokens, seconds, multiplier) {
    if (!tokens || tokens <= 0 || !seconds || isNaN(seconds) || seconds <= 0) {
      return 0.0;
    }
    const effSec = Math.max(Number(seconds), 60.0);
    return Math.round((tokens / effSec) * multiplier * 100) / 100;
  }

  const activeContextTpm = calcRate(totalEstTokens, activeAgentSec, 60.0);
  const activeBilledTpm = calcRate(totalBilledTokens, activeAgentSec, 60.0);
  const pairingBilledTph = calcRate(totalBilledTokens, activePairingSec, 3600.0);

  // 4. Peak calibration from hourly_step_rollups / steps
  let distinctHours = [];
  try {
    const rows = db.prepare(`
      SELECT
        SUM(est_tokens) AS est_tok,
        SUM(billed_tokens) AS bld_tok
      FROM hourly_step_rollups
      WHERE hour_key IS NOT NULL AND hour_key != ''
        AND hour_key <= ?
      GROUP BY hour_key
    `).all(nowHour);
    distinctHours = rows.map(r => ({ est: Number(r.est_tok) || 0, bld: Number(r.bld_tok) || 0 }));
  } catch {
    distinctHours = [];
  }

  if (distinctHours.length < 3) {
    try {
      const rows = db.prepare(`
        SELECT
          COALESCE(SUM(est_tokens), 0) AS est_tok,
          COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS bld_tok
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
          AND substr(replace(created_at, ' ', 'T'), 1, 13) <= ?
        GROUP BY substr(replace(created_at, ' ', 'T'), 1, 13)
      `).all(nowHour);
      distinctHours = rows.map(r => ({ est: Number(r.est_tok) || 0, bld: Number(r.bld_tok) || 0 }));
    } catch {
      try {
        const rows = db.prepare(`
          SELECT
            COALESCE(SUM(est_tokens), 0) AS est_tok,
            COALESCE(SUM(COALESCE(billed_input_tokens, 0)), 0) AS bld_tok
          FROM steps
          WHERE created_at IS NOT NULL AND created_at != ''
            AND substr(replace(created_at, ' ', 'T'), 1, 13) <= ?
          GROUP BY substr(replace(created_at, ' ', 'T'), 1, 13)
        `).all(nowHour);
        distinctHours = rows.map(r => ({ est: Number(r.est_tok) || 0, bld: Number(r.bld_tok) || 0 }));
      } catch {
        distinctHours = [];
      }
    }
  }

  let peakBilledTpm = DEFAULT_PEAK_BILLED_TPM;
  let peakContextTpm = DEFAULT_PEAK_CONTEXT_TPM;
  let peakBilledTph = DEFAULT_PEAK_BILLED_TPH;
  let peakContextTph = DEFAULT_PEAK_CONTEXT_TPH;

  if (distinctHours.length >= 3) {
    const bldHourly = distinctHours.map(d => d.bld);
    const ctxHourly = distinctHours.map(d => d.est);
    const p95Bld = calcP95(bldHourly);
    const p95Ctx = calcP95(ctxHourly);

    peakBilledTph = p95Bld > 0 ? Math.round(p95Bld * 100) / 100 : DEFAULT_PEAK_BILLED_TPH;
    peakBilledTpm = p95Bld > 0 ? Math.round((peakBilledTph / 60.0) * 100) / 100 : DEFAULT_PEAK_BILLED_TPM;
    peakContextTph = p95Ctx > 0 ? Math.round(p95Ctx * 100) / 100 : DEFAULT_PEAK_CONTEXT_TPH;
    peakContextTpm = p95Ctx > 0 ? Math.round((peakContextTph / 60.0) * 100) / 100 : DEFAULT_PEAK_CONTEXT_TPM;
  }

  const arcPct = peakBilledTpm > 0
    ? Math.round(Math.max(0.0, Math.min(100.0, (rolling5mBilledTpm / peakBilledTpm) * 100.0)) * 100) / 100
    : 0.0;

  const contextFillPct = peakContextTpm > 0
    ? Math.round(Math.max(0.0, Math.min(100.0, (rolling5mContextTpm / peakContextTpm) * 100.0)) * 100) / 100
    : 0.0;

  return {
    rolling5mContextTpm,
    rolling5mBilledTpm,
    rolling60mContextTph,
    rolling60mBilledTph,
    activeContextTpm,
    activeBilledTpm,
    pairingBilledTph,
    peakBilledTpm,
    peakContextTpm,
    arcPct,
    contextFillPct
  };
}

export function getModelContextLimit(modelName) {
  const lower = (modelName || '').toLowerCase();
  for (const rule of MODEL_LIMIT_RULES) {
    if (rule.match.some(m => lower.includes(m))) {
      return rule.limit;
    }
  }
  return 1000000;
}

export function getModelQuotaPrefix(modelName) {
  const lower = (modelName || '').toLowerCase();
  const is3p = lower.includes('claude')
    || lower.includes('sonnet')
    || lower.includes('opus')
    || lower.includes('gpt')
    || lower.includes('o1')
    || lower.includes('o3');
  return is3p ? '3p' : 'gemini';
}

export function formatResetCountdown(resetTimeIso, now = new Date()) {
  if (!resetTimeIso) return '';
  try {
    let str = String(resetTimeIso).trim();
    if (!str.endsWith('Z') && !/[+-]\d{2}(:?\d{2})?$/.test(str)) {
      str = str.replace(' ', 'T') + 'Z';
    }
    const resetDate = new Date(str);
    const nowDate = (now instanceof Date) ? now : new Date(now);
    const diffMs = resetDate.getTime() - nowDate.getTime();
    if (isNaN(diffMs)) return '';
    const diffSec = Math.floor(diffMs / 1000);
    if (diffSec <= 0) return '0m';

    const days = Math.floor(diffSec / 86400);
    const hours = Math.floor((diffSec % 86400) / 3600);
    const minutes = Math.floor((diffSec % 3600) / 60);

    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${Math.max(1, minutes)}m`;
  } catch {
    return '';
  }
}

export function getQuotaSummary(db, modelName = 'Gemini 3.7 Flash', sessionCost = 0, costMap = null, now = new Date()) {
  let dbRows = [];
  if (db) {
    try {
      dbRows = db.prepare(`
        SELECT bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at
        FROM quota_state
      `).all();
    } catch {
      // quota_state table might not exist yet or query failed
      dbRows = [];
    }
  }

  const rowMap = new Map();
  for (const row of dbRows) {
    if (row && row.bucket_id) {
      rowMap.set(row.bucket_id, row);
    }
  }

  const prefix = getModelQuotaPrefix(modelName);
  const costNum = Math.max(0, Number(sessionCost) || 0);

  const standardBuckets = [
    { bucket_id: 'gemini-weekly', group_name: 'Gemini Models', window: 'weekly' },
    { bucket_id: 'gemini-5h', group_name: 'Gemini Models', window: '5h' },
    { bucket_id: '3p-weekly', group_name: 'Claude and GPT models', window: 'weekly' },
    { bucket_id: '3p-5h', group_name: 'Claude and GPT models', window: '5h' }
  ];

  const buckets = {};
  for (const def of standardBuckets) {
    const r = rowMap.get(def.bucket_id);
    const remFrac = (r && r.remaining_fraction !== null && r.remaining_fraction !== undefined)
      ? Number(r.remaining_fraction)
      : 1.0;
    const remPct = Math.round(remFrac * 1000) / 10;
    const usedPct = Math.round((1 - remFrac) * 1000) / 10;
    const resetTime = r?.reset_time || '';
    const resetCountdown = formatResetCountdown(resetTime, now);
    const cap = (r && r.calibrated_capacity && Number(r.calibrated_capacity) > 0)
      ? Number(r.calibrated_capacity)
      : (DEFAULT_CAPACITIES[def.bucket_id] || (def.bucket_id.startsWith('3p') ? 200.0 : 672.0));

    // Determine applicable cost for this bucket:
    // If costMap is provided (e.g. from getGlobalData):
    //   gemini buckets use costMap.gemini, 3p buckets use costMap['3p']
    // Otherwise in session mode:
    //   only buckets matching the model prefix attribute the session cost;
    //   cross-model family buckets have 0 session impact.
    let applicableCost = 0;
    if (costMap && typeof costMap === 'object') {
      applicableCost = def.bucket_id.startsWith('3p')
        ? (Number(costMap['3p']) || 0)
        : (Number(costMap.gemini) || 0);
    } else {
      applicableCost = def.bucket_id.startsWith(prefix) ? costNum : 0;
    }

    const sessionImpactPct = cap > 0 ? Number(((applicableCost / cap) * 100).toFixed(4)) : 0;

    let bucketTph = 0;
    if (db && resetTime) {
      try {
        const is3p = def.bucket_id.startsWith('3p');
        const is5h = def.bucket_id.includes('5h');
        const windowHours = is5h ? 5.0 : 168.0;
        let str = String(resetTime).trim();
        if (!str.endsWith('Z') && !/[+-]\d{2}(:?\d{2})?$/.test(str)) {
          str = str.replace(' ', 'T') + 'Z';
        }
        const resetDate = new Date(str);
        let nowDate = (now instanceof Date) ? now : new Date(now || Date.now());
        if (isNaN(nowDate.getTime())) {
          nowDate = new Date();
        }
        if (!isNaN(resetDate.getTime())) {
          const diffSec = Math.max(0, Math.floor((resetDate.getTime() - nowDate.getTime()) / 1000));
          const remainingHours = diffSec / 3600.0;
          const elapsedHours = Math.max(0, Math.min(windowHours, windowHours - remainingHours));
          const startWin = new Date(resetDate.getTime() - windowHours * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z').slice(0, 19);
          const endWin = nowDate.toISOString().replace(/\.\d{3}Z$/, 'Z').slice(0, 19);

          const filterClause = is3p
            ? `AND (model_name IS NOT NULL AND (LOWER(model_name) LIKE '%claude%' OR LOWER(model_name) LIKE '%sonnet%' OR LOWER(model_name) LIKE '%opus%' OR LOWER(model_name) LIKE '%gpt%' OR LOWER(model_name) LIKE '%o1%' OR LOWER(model_name) LIKE '%o3%'))`
            : `AND (model_name IS NULL OR NOT (LOWER(model_name) LIKE '%claude%' OR LOWER(model_name) LIKE '%sonnet%' OR LOWER(model_name) LIKE '%opus%' OR LOWER(model_name) LIKE '%gpt%' OR LOWER(model_name) LIKE '%o1%' OR LOWER(model_name) LIKE '%o3%'))`;

          let tokRow = null;
          try {
            tokRow = db.prepare(`
              SELECT COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) as b_tok
              FROM steps
              WHERE created_at IS NOT NULL AND created_at != ''
                AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
                AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
                ${filterClause}
            `).get(startWin, endWin);
          } catch {
            try {
              tokRow = db.prepare(`
                SELECT COALESCE(SUM(COALESCE(billed_input_tokens, 0)), 0) as b_tok
                FROM steps
                WHERE created_at IS NOT NULL AND created_at != ''
                  AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
                  AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
                  ${filterClause}
              `).get(startWin, endWin);
            } catch {
              tokRow = null;
            }
          }

          const bTok = Number(tokRow?.b_tok) || 0;
          if (elapsedHours > 0 && bTok > 0) {
            const effHours = Math.max(elapsedHours, 1.0 / 60.0);
            bucketTph = Math.round((bTok / effHours) * 100) / 100;
          }
        }
      } catch {
        // fallback
      }
    }

    buckets[def.bucket_id] = {
      bucket_id: def.bucket_id,
      group_name: r?.group_name || def.group_name,
      window: r?.window || def.window,
      remaining_fraction: remFrac,
      remaining_pct: remPct,
      used_pct: Math.max(0, usedPct),
      reset_time: resetTime,
      reset_countdown: resetCountdown,
      calibrated_capacity: cap,
      session_impact_pct: sessionImpactPct,
      billed_tph: bucketTph,
      fetched_at: r?.fetched_at || ''
    };
  }

  // Also include any extra custom buckets from quota_state
  for (const [bid, r] of rowMap.entries()) {
    if (!buckets[bid]) {
      const remFrac = (r && r.remaining_fraction !== null && r.remaining_fraction !== undefined)
        ? Number(r.remaining_fraction)
        : 1.0;
      const remPct = Math.round(remFrac * 1000) / 10;
      const usedPct = Math.round((1 - remFrac) * 1000) / 10;
      const resetTime = r?.reset_time || '';
      const cap = (r && r.calibrated_capacity && Number(r.calibrated_capacity) > 0)
        ? Number(r.calibrated_capacity)
        : (DEFAULT_CAPACITIES[bid] || 672.0);
      const isMatching = bid.startsWith(prefix);
      const applicableCost = isMatching ? costNum : 0;
      const sessionImpactPct = cap > 0 ? Number(((applicableCost / cap) * 100).toFixed(4)) : 0;

      buckets[bid] = {
        bucket_id: bid,
        group_name: r?.group_name || 'Custom Quota',
        window: r?.window || 'custom',
        remaining_fraction: remFrac,
        remaining_pct: remPct,
        used_pct: Math.max(0, usedPct),
        reset_time: resetTime,
        reset_countdown: formatResetCountdown(resetTime, now),
        calibrated_capacity: cap,
        session_impact_pct: sessionImpactPct,
        billed_tph: 0,
        fetched_at: r?.fetched_at || ''
      };
    }
  }

  const weeklyKey = `${prefix}-weekly`;
  const fiveHKey = `${prefix}-5h`;

  const activeWeekly = buckets[weeklyKey] || {
    remaining_pct: 100.0,
    reset_time: '',
    reset_countdown: '',
    calibrated_capacity: prefix === '3p' ? 200.0 : 672.0,
    session_impact_pct: 0,
    billed_tph: 0
  };
  const active5h = buckets[fiveHKey] || {
    remaining_pct: 100.0,
    reset_time: '',
    reset_countdown: '',
    calibrated_capacity: prefix === '3p' ? 70.0 : 236.7,
    session_impact_pct: 0,
    billed_tph: 0
  };

  return {
    prefix,
    is_3p: prefix === '3p',
    model_name: modelName,
    buckets,
    weekly_remaining_pct: activeWeekly.remaining_pct,
    five_hour_remaining_pct: active5h.remaining_pct,
    weekly_reset_time: activeWeekly.reset_time,
    five_hour_reset_time: active5h.reset_time,
    weekly_reset_countdown: activeWeekly.reset_countdown,
    five_hour_reset_countdown: active5h.reset_countdown,
    calibrated_weekly_capacity: activeWeekly.calibrated_capacity,
    calibrated_5h_capacity: active5h.calibrated_capacity,
    session_weekly_pct: activeWeekly.session_impact_pct,
    session_5h_pct: active5h.session_impact_pct,
    weekly_billed_tph: activeWeekly.billed_tph || 0,
    five_hour_billed_tph: active5h.billed_tph || 0,
  };
}

let cachedDb = null;
let cachedDbPath = null;

export function openDatabase(customPath) {
  const dbPath = customPath || process.env.AGY_ANALYTICS_DB || join(homedir(), '.gemini', 'antigravity', 'analytics.db');
  if (!existsSync(dbPath)) {
    return null;
  }
  try {
    return new DatabaseSync(dbPath, { readOnly: true });
  } catch (err) {
    console.error(`Could not open analytics database at ${dbPath}:`, err.message);
    return null;
  }
}

export function getDatabase(customPath) {
  const dbPath = customPath || process.env.AGY_ANALYTICS_DB || join(homedir(), '.gemini', 'antigravity', 'analytics.db');
  if (cachedDb && cachedDbPath === dbPath) {
    return cachedDb;
  }
  cachedDb = openDatabase(dbPath);
  cachedDbPath = dbPath;
  return cachedDb;
}

export function createDefaultSession(cid = null, title = 'No conversation records', modelName = 'Gemini 3.7 Flash', db = null, now = new Date()) {
  const maxTokens = getModelContextLimit(modelName);
  const quotaSummary = getQuotaSummary(db, modelName, 0, null, now);
  const defaultVelocity = {
    rolling5mContextTpm: 0.0,
    rolling5mBilledTpm: 0.0,
    rolling60mContextTph: 0.0,
    rolling60mBilledTph: 0.0,
    activeContextTpm: 0.0,
    activeBilledTpm: 0.0,
    pairingBilledTph: 0.0,
    peakBilledTpm: DEFAULT_PEAK_BILLED_TPM,
    peakContextTpm: DEFAULT_PEAK_CONTEXT_TPM,
    arcPct: 0.0,
    contextFillPct: 0.0
  };
  const defaultFleet = {
    is_swarm: false,
    root_id: cid || null,
    active_cid: cid || null,
    inspected_cid: cid || null,
    is_inspecting_other: false,
    inspected_role: 'Main Session',
    agent_count: 1,
    subagent_count: 0,
    total_cost: 0,
    total_tokens: 0,
    total_disk_tokens: 0,
    total_steps: 0,
    weekly_quota_pct: 0,
    five_hour_quota_pct: 0,
    shielded_tokens: 0,
    shielding_ratio: 1.0,
    velocity: null,
    nodes: []
  };

  return {
    cid,
    title,
    project: null,
    model_name: modelName,
    max_tokens: maxTokens,
    active_tokens: 0,
    pct: 0,
    total_tokens: 0,
    total_steps: 0,
    billed_cost: 0,
    compaction_count: 0,
    pruned_tokens: 0,
    compaction_detected: false,
    min_step: 0,
    max_step: 0,
    categories: [],
    top_steps: [],
    weekly_quota_pct: 0,
    five_hour_quota_pct: 0,
    quota_remaining_weekly_pct: quotaSummary.weekly_remaining_pct,
    quota_remaining_5h_pct: quotaSummary.five_hour_remaining_pct,
    quota_reset_time: quotaSummary.weekly_reset_time,
    quota: quotaSummary,
    velocity: defaultVelocity,
    fleet: defaultFleet
  };
}

function resolveConversationRoot(db, conv) {
  if (!conv) return null;
  if (conv.root_id) return conv.root_id;
  if (!conv.parent_id) return conv.id;
  try {
    let curr = conv;
    for (let d = 0; d < 10 && curr && curr.parent_id; d++) {
      const p = db.prepare('SELECT id, parent_id, root_id FROM conversations WHERE id = ?').get(curr.parent_id);
      if (!p) break;
      if (p.root_id) return p.root_id;
      curr = p;
    }
    return curr?.id || conv.parent_id || conv.id;
  } catch {
    return conv.parent_id || conv.id;
  }
}

export function getSessionData(db, cid, inspectCid = null, now = new Date()) {
  if (!db) {
    return createDefaultSession(inspectCid || cid || null, 'No analytics database', 'Unknown', null, now);
  }

  let activeCid = cid || null;
  let targetCid = inspectCid || cid || null;
  let activeConv = null;
  let targetConv = null;

  try {
    if (activeCid) {
      activeConv = db.prepare(`
        SELECT *
        FROM conversations
        WHERE id = ?
      `).get(activeCid);

      // If a specific conversation was requested but is not found yet in SQLite,
      // do NOT fall back to another conversation (prevents cross-session data leakage).
      if (!activeConv) {
        return createDefaultSession(activeCid, 'Active Session (indexing...)', 'Gemini 3.7 Flash', db, now);
      }
    }

    if (targetCid) {
      if (activeConv && targetCid === activeCid) {
        targetConv = activeConv;
      } else {
        targetConv = db.prepare(`
          SELECT *
          FROM conversations
          WHERE id = ?
        `).get(targetCid);
      }
    } else {
      // No specific cid passed; fetch latest active conversation
      targetConv = db.prepare(`
        SELECT *
        FROM conversations
        ORDER BY updated_at DESC
        LIMIT 1
      `).get();

      if (!targetConv) {
        return createDefaultSession(null, 'No conversation records', 'Gemini 3.7 Flash', db, now);
      }
      targetCid = targetConv.id;
      activeCid = targetConv.id;
      activeConv = targetConv;
    }

    if (!targetConv) {
      if (activeConv) {
        targetConv = activeConv;
        targetCid = activeCid;
      } else {
        return createDefaultSession(targetCid || activeCid, 'Active Session (indexing...)', 'Gemini 3.7 Flash', db, now);
      }
    }

    // If activeCid && activeCid !== targetCid, verify targetConv belongs to the same swarm root_id or fall back to activeCid
    if (activeCid && activeCid !== targetCid && activeConv) {
      const activeRoot = resolveConversationRoot(db, activeConv);
      const targetRoot = resolveConversationRoot(db, targetConv);
      if (activeRoot !== targetRoot) {
        targetConv = activeConv;
        targetCid = activeCid;
      }
    }
  } catch {
    // conversations table might not exist yet
    return createDefaultSession(targetCid || activeCid, 'No analytics database', 'Gemini 3.7 Flash', db, now);
  }

  const resolvedCid = targetConv.id;
  const modelName = targetConv.model_name || 'Gemini 3.7 Flash';
  const billedCost = Number(targetConv.billed_cost) || 0.0;

  // Determine active context start from checkpoint step if present (safely handles missing steps table)
  let cpRow = null;
  let rangeRow = null;
  let categories = [];
  let topSteps = [];

  try {
    cpRow = db.prepare(`
      SELECT MAX(step_index) as cp_step
      FROM steps
      WHERE conversation_id = ? AND step_type = 'CHECKPOINT'
    `).get(resolvedCid);

    const sinceStep = (cpRow && cpRow.cp_step !== null && cpRow.cp_step !== undefined)
      ? cpRow.cp_step
      : 0;

    rangeRow = db.prepare(`
      SELECT MIN(step_index) as min_step, MAX(step_index) as max_step, COUNT(*) as step_count, SUM(est_tokens) as active_tokens
      FROM steps
      WHERE conversation_id = ? AND step_index >= ?
    `).get(resolvedCid, sinceStep);

    categories = db.prepare(`
      SELECT category, SUM(est_tokens) as tokens
      FROM steps
      WHERE conversation_id = ? AND step_index >= ?
      GROUP BY category
      ORDER BY tokens DESC
    `).all(resolvedCid, sinceStep);

    try {
      topSteps = db.prepare(`
        SELECT step_index, activity, tool_or_source, est_tokens, detail, billed_cost
        FROM steps
        WHERE conversation_id = ? AND step_index >= ?
        ORDER BY est_tokens DESC
        LIMIT 3
      `).all(resolvedCid, sinceStep);
    } catch {
      topSteps = db.prepare(`
        SELECT step_index, activity, tool_or_source, est_tokens, detail
        FROM steps
        WHERE conversation_id = ? AND step_index >= ?
        ORDER BY est_tokens DESC
        LIMIT 3
      `).all(resolvedCid, sinceStep);
    }
  } catch {
    // steps table might not exist in minimal test DBs
  }

  const activeTokens = rangeRow?.active_tokens ?? targetConv.total_tokens ?? 0;
  const maxTokens = getModelContextLimit(modelName);
  const pct = maxTokens ? (activeTokens / maxTokens) * 100 : 0;

  const quotaSummary = getQuotaSummary(db, modelName, billedCost, null, now);
  const cap = quotaSummary.calibrated_weekly_capacity || DEFAULT_CAPACITIES['gemini-weekly'];

  // Swarm fleet rollups and topology
  const effectiveRoot = resolveConversationRoot(db, targetConv);
  let swarmRows = [];
  try {
    swarmRows = db.prepare(`
      SELECT id, title, model_name, total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens, parent_id, root_id, depth, subagent_role, subagent_type, compaction_count, pruned_tokens
      FROM conversations
      WHERE root_id = ? OR id = ?
    `).all(effectiveRoot, effectiveRoot);
  } catch {
    try {
      swarmRows = db.prepare(`
        SELECT id, title, model_name, total_steps, total_tokens, billed_cost, parent_id, root_id
        FROM conversations
        WHERE root_id = ? OR id = ?
      `).all(effectiveRoot, effectiveRoot);
    } catch {
      try {
        swarmRows = db.prepare(`
          SELECT id, title, model_name, total_steps, total_tokens, billed_cost
          FROM conversations
          WHERE id = ?
        `).all(effectiveRoot);
      } catch {
        swarmRows = [targetConv];
      }
    }
  }

  if (!swarmRows || swarmRows.length === 0) {
    swarmRows = [targetConv];
  } else if (!swarmRows.some(n => n.id === targetConv.id)) {
    swarmRows.push(targetConv);
  }

  let cpStmt = null;
  let activeTokensStmt = null;
  try {
    cpStmt = db.prepare(`
      SELECT MAX(step_index) as cp_step
      FROM steps
      WHERE conversation_id = ? AND step_type = 'CHECKPOINT'
    `);
    activeTokensStmt = db.prepare(`
      SELECT SUM(est_tokens) as active_tokens
      FROM steps
      WHERE conversation_id = ? AND step_index >= ?
    `);
  } catch {
    // steps table might not exist
  }

  const nodes = swarmRows.map(node => {
    let nodeActiveTokens = null;
    if (node.id === resolvedCid && rangeRow?.active_tokens !== undefined && rangeRow?.active_tokens !== null) {
      nodeActiveTokens = Number(rangeRow.active_tokens);
    } else if (cpStmt && activeTokensStmt) {
      try {
        const cp = cpStmt.get(node.id);
        const since = (cp && cp.cp_step !== null && cp.cp_step !== undefined)
          ? cp.cp_step
          : 0;

        const row = activeTokensStmt.get(node.id, since);
        if (row && row.active_tokens !== null && row.active_tokens !== undefined) {
          nodeActiveTokens = Number(row.active_tokens);
        }
      } catch {
        // query fallback
      }
    }

    if (nodeActiveTokens === null || isNaN(nodeActiveTokens)) {
      nodeActiveTokens = Number(node.total_tokens) || 0;
    }

    const nodeModel = node.model_name || 'Gemini 3.7 Flash';
    const nodeMaxTokens = getModelContextLimit(nodeModel);
    const nodeContextPct = nodeMaxTokens > 0
      ? Math.min(100, Math.round((nodeActiveTokens / nodeMaxTokens) * 1000) / 10)
      : 0;

    const nodePrefix = getModelQuotaPrefix(nodeModel);
    const nodeCost = Number(node.billed_cost) || 0.0;
    const nodeCap = quotaSummary.buckets?.[`${nodePrefix}-weekly`]?.calibrated_capacity
      || DEFAULT_CAPACITIES[`${nodePrefix}-weekly`]
      || (nodePrefix === '3p' ? 200.0 : 672.0);
    const nodeWeeklyQuotaPct = nodeCap > 0 ? Number(((nodeCost / nodeCap) * 100).toFixed(4)) : 0;

    const node5hCap = quotaSummary.buckets?.[`${nodePrefix}-5h`]?.calibrated_capacity
      || DEFAULT_CAPACITIES[`${nodePrefix}-5h`]
      || (nodePrefix === '3p' ? 70.0 : 236.7);
    const node5hQuotaPct = node5hCap > 0 ? Number(((nodeCost / node5hCap) * 100).toFixed(4)) : 0;

    const diskTokens = Number(node.total_tokens) || 0;
    const billedTokens = (Number(node.billed_input_tokens) || 0) + (Number(node.billed_output_tokens) || 0);
    const nodeTotalTokens = billedTokens > 0 ? billedTokens : diskTokens;

    return {
      id: node.id,
      title: node.title || (node.id === effectiveRoot ? 'Root Coordinator' : `Subagent ${node.id.slice(0, 8)}`),
      model_name: nodeModel,
      total_steps: Number(node.total_steps) || 0,
      total_tokens: nodeTotalTokens,
      total_disk_tokens: diskTokens,
      billed_cost: nodeCost,
      billed_input_tokens: Number(node.billed_input_tokens) || 0,
      billed_output_tokens: Number(node.billed_output_tokens) || 0,
      parent_id: node.parent_id || null,
      root_id: node.root_id || null,
      depth: Number(node.depth) || 0,
      role: node.subagent_role || (node.id === effectiveRoot ? 'Root Coordinator' : 'Worker'),
      subagent_role: node.subagent_role || (node.id === effectiveRoot ? 'Root Coordinator' : 'Worker'),
      type: node.subagent_type || 'self',
      subagent_type: node.subagent_type || 'self',
      compaction_count: Number(node.compaction_count) || 0,
      pruned_tokens: Number(node.pruned_tokens) || 0,
      max_tokens: nodeMaxTokens,
      active_tokens: nodeActiveTokens,
      context_pct: nodeContextPct,
      weekly_quota_pct: nodeWeeklyQuotaPct,
      five_hour_quota_pct: node5hQuotaPct,
      is_active: node.id === activeCid,
      is_active_session: node.id === activeCid,
      is_inspected: node.id === targetCid,
      is_root: node.id === effectiveRoot
    };
  });

  nodes.sort((a, b) => {
    const costDiff = (Number(b.billed_cost) || 0) - (Number(a.billed_cost) || 0);
    if (Math.abs(costDiff) > 1e-6) {
      return costDiff;
    }
    return (Number(b.total_tokens) || 0) - (Number(a.total_tokens) || 0);
  });

  const totalFleetCost = Number(nodes.reduce((acc, n) => acc + (Number(n.billed_cost) || 0), 0).toFixed(4));
  const totalFleetTokens = nodes.reduce((acc, n) => acc + (Number(n.total_tokens) || 0), 0);
  const totalFleetDiskTokens = nodes.reduce((acc, n) => acc + (Number(n.total_disk_tokens) || 0), 0);
  const totalFleetSteps = nodes.reduce((acc, n) => acc + (Number(n.total_steps) || 0), 0);

  const rootConv = nodes.find(n => n.id === effectiveRoot) || targetConv;
  const rootModel = rootConv.model_name || targetConv.model_name || 'Gemini 3.7 Flash';
  const fleetPrefix = getModelQuotaPrefix(rootModel);
  const fleetWeeklyCap = quotaSummary.buckets?.[`${fleetPrefix}-weekly`]?.calibrated_capacity
    || DEFAULT_CAPACITIES[`${fleetPrefix}-weekly`]
    || (fleetPrefix === '3p' ? 200.0 : 672.0);
  const fleet5hCap = quotaSummary.buckets?.[`${fleetPrefix}-5h`]?.calibrated_capacity
    || DEFAULT_CAPACITIES[`${fleetPrefix}-5h`]
    || (fleetPrefix === '3p' ? 70.0 : 236.7);
  const fleetCostGemini = nodes.filter(n => getModelQuotaPrefix(n.model_name) === 'gemini').reduce((acc, n) => acc + (Number(n.billed_cost) || 0), 0);
  const fleetCost3p = nodes.filter(n => getModelQuotaPrefix(n.model_name) === '3p').reduce((acc, n) => acc + (Number(n.billed_cost) || 0), 0);
  const applicableFleetCost = fleetPrefix === '3p' ? (fleetCost3p > 0 ? fleetCost3p : totalFleetCost) : (fleetCostGemini > 0 ? fleetCostGemini : totalFleetCost);
  const fleetWeeklyQuotaPct = fleetWeeklyCap > 0 ? Number(((applicableFleetCost / fleetWeeklyCap) * 100).toFixed(4)) : 0;
  const fleet5hQuotaPct = fleet5hCap > 0 ? Number(((applicableFleetCost / fleet5hCap) * 100).toFixed(4)) : 0;

  const rootTokens = rootConv ? (Number(rootConv.total_tokens) || 0) : (Number(targetConv.total_tokens) || 0);
  const shieldedTokens = Math.max(0, totalFleetTokens - rootTokens);
  const shieldingRatio = rootTokens > 0 ? Number((totalFleetTokens / rootTokens).toFixed(1)) : 1.0;

  const fleet = {
    is_swarm: nodes.length > 1,
    root_id: effectiveRoot,
    active_cid: activeCid,
    inspected_cid: targetCid,
    is_inspecting_other: Boolean(activeCid !== targetCid),
    inspected_role: targetConv.subagent_role || 'Main Session',
    agent_count: nodes.length,
    subagent_count: Math.max(0, nodes.length - 1),
    total_cost: totalFleetCost,
    total_tokens: totalFleetTokens,
    total_disk_tokens: totalFleetDiskTokens,
    total_steps: totalFleetSteps,
    weekly_quota_pct: fleetWeeklyQuotaPct,
    five_hour_quota_pct: fleet5hQuotaPct,
    shielded_tokens: shieldedTokens,
    shielding_ratio: shieldingRatio,
    nodes
  };

  return {
    cid: resolvedCid,
    title: targetConv.title || 'Active Session',
    project: targetConv.project,
    model_name: modelName,
    max_tokens: maxTokens,
    active_tokens: activeTokens,
    pct: Math.round(pct * 10) / 10,
    total_tokens: targetConv.total_tokens || activeTokens,
    total_steps: targetConv.total_steps || (rangeRow?.step_count ?? 0),
    billed_cost: billedCost,
    compaction_count: targetConv.compaction_count || 0,
    pruned_tokens: targetConv.pruned_tokens || 0,
    compaction_detected: (targetConv.compaction_count || 0) > 0,
    min_step: rangeRow?.min_step ?? 0,
    max_step: rangeRow?.max_step ?? 0,
    categories: categories.map(c => ({
      category: c.category,
      tokens: c.tokens,
      share: activeTokens ? Math.round((c.tokens / activeTokens) * 1000) / 10 : 0
    })),
    top_steps: topSteps.map(s => {
      const stepCost = Number(s.billed_cost || 0.0);
      return {
        step_index: s.step_index,
        activity: s.activity,
        tool_or_source: s.tool_or_source,
        est_tokens: s.est_tokens,
        billed_cost: stepCost,
        weekly_quota_pct: cap > 0 ? Number(((stepCost / cap) * 100).toFixed(4)) : 0,
        detail: s.detail
      };
    }),
    weekly_quota_pct: quotaSummary.session_weekly_pct,
    five_hour_quota_pct: quotaSummary.session_5h_pct,
    quota_remaining_weekly_pct: quotaSummary.weekly_remaining_pct,
    quota_remaining_5h_pct: quotaSummary.five_hour_remaining_pct,
    quota_reset_time: quotaSummary.weekly_reset_time,
    quota: quotaSummary,
    velocity: queryVelocityMetrics(db, { conversationId: resolvedCid, now }),
    fleet: {
      ...fleet,
      velocity: fleet.is_swarm ? queryVelocityMetrics(db, { rootId: effectiveRoot, now }) : null
    }
  };
}

export const loadSessionById = getSessionData;

export function getGlobalData(db, now = new Date()) {
  if (!db) {
    const quotaSummary = getQuotaSummary(null, 'Gemini 3.7 Flash', 0);
    return {
      total_conversations: 0,
      root_conversations: 0,
      subagent_conversations: 0,
      total_tokens: 0,
      total_cost: 0,
      weekly_cost: 0,
      total_compactions: 0,
      total_pruned_tokens: 0,
      weekly_quota_pct: 0,
      activeContextTpm: 0.0,
      activeBilledTpm: 0.0,
      pairingBilledTph: 0.0,
      rolling60mBilledTph: 0.0,
      velocity: null,
      quota: quotaSummary
    };
  }

  let totalConversations = 0;
  let rootConversations = 0;
  let subagentConversations = 0;
  let totalTokens = 0;
  let totalCost = 0.0;
  let totalCompactions = 0;
  let totalPrunedTokens = 0;
  let costGemini = 0.0;
  let cost3p = 0.0;

  try {
    const baseRow = db.prepare(`
      SELECT
        COUNT(*) as total_conversations,
        COALESCE(SUM(CASE WHEN parent_id IS NULL OR parent_id = '' THEN 1 ELSE 0 END), 0) as root_conversations,
        COALESCE(SUM(CASE WHEN parent_id IS NOT NULL AND parent_id != '' THEN 1 ELSE 0 END), 0) as subagent_conversations,
        COALESCE(SUM(total_tokens), 0) as total_tokens,
        COALESCE(SUM(billed_cost), 0.0) as total_cost
      FROM conversations
    `).get();

    if (baseRow) {
      totalConversations = Number(baseRow.total_conversations) || 0;
      rootConversations = Number(baseRow.root_conversations) || 0;
      subagentConversations = Number(baseRow.subagent_conversations) || 0;
      totalTokens = Number(baseRow.total_tokens) || 0;
      totalCost = Number(baseRow.total_cost) || 0.0;
    }
  } catch {
    // parent_id column might not exist in older/minimal schemas
    try {
      const fallbackRow = db.prepare(`
        SELECT COUNT(*) as total_conversations,
               COALESCE(SUM(total_tokens), 0) as total_tokens,
               COALESCE(SUM(billed_cost), 0.0) as total_cost
        FROM conversations
      `).get();

      if (fallbackRow) {
        totalConversations = Number(fallbackRow.total_conversations) || 0;
        rootConversations = totalConversations;
        subagentConversations = 0;
        totalTokens = Number(fallbackRow.total_tokens) || 0;
        totalCost = Number(fallbackRow.total_cost) || 0.0;
      }
    } catch {
      // conversations table missing or empty
    }
  }

  try {
    const compRow = db.prepare(`
      SELECT COALESCE(SUM(compaction_count), 0) as total_compactions,
             COALESCE(SUM(pruned_tokens), 0) as total_pruned_tokens
      FROM conversations
    `).get();

    if (compRow) {
      totalCompactions = Number(compRow.total_compactions) || 0;
      totalPrunedTokens = Number(compRow.total_pruned_tokens) || 0;
    }
  } catch {
    // compaction columns might not exist
  }

  let geminiWindowStart = null;
  let threePWindowStart = null;
  try {
    const qRows = db.prepare(`
      SELECT bucket_id, reset_time FROM quota_state
      WHERE reset_time IS NOT NULL AND reset_time != ''
        AND (window = 'weekly' OR bucket_id LIKE '%weekly%')
    `).all();
    for (const r of qRows) {
      let str = String(r.reset_time).trim();
      if (!str.endsWith('Z') && !/[+-]\d{2}(:?\d{2})?$/.test(str)) {
        str = str.replace(' ', 'T') + 'Z';
      }
      const resetDate = new Date(str);
      if (!isNaN(resetDate.getTime())) {
        const startDate = new Date(resetDate.getTime() - 7 * 24 * 60 * 60 * 1000);
        const ws = startDate.toISOString().replace(/\.\d{3}Z$/, 'Z');
        if (r.bucket_id && (r.bucket_id.startsWith('3p') || r.bucket_id.startsWith('claude') || r.bucket_id.startsWith('gpt'))) {
          threePWindowStart = ws;
        } else {
          geminiWindowStart = ws;
        }
      }
    }
  } catch {
    // quota_state table missing or query failed
  }

  const defaultWindowStart = geminiWindowStart || threePWindowStart;
  const gStart = geminiWindowStart || defaultWindowStart;
  const pStart = threePWindowStart || defaultWindowStart;

  try {
    let breakdown;
    if (gStart && pStart) {
      breakdown = db.prepare(`
        SELECT
          COALESCE(SUM(CASE
            WHEN (LOWER(model_name) LIKE '%claude%'
              OR LOWER(model_name) LIKE '%sonnet%'
              OR LOWER(model_name) LIKE '%opus%'
              OR LOWER(model_name) LIKE '%gpt%'
              OR LOWER(model_name) LIKE '%o1%'
              OR LOWER(model_name) LIKE '%o3%')
              AND datetime(created_at) >= datetime(?)
            THEN billed_cost ELSE 0 END), 0.0) as cost_3p,
          COALESCE(SUM(CASE
            WHEN NOT (LOWER(model_name) LIKE '%claude%'
              OR LOWER(model_name) LIKE '%sonnet%'
              OR LOWER(model_name) LIKE '%opus%'
              OR LOWER(model_name) LIKE '%gpt%'
              OR LOWER(model_name) LIKE '%o1%'
              OR LOWER(model_name) LIKE '%o3%')
              AND datetime(created_at) >= datetime(?)
            THEN billed_cost ELSE 0 END), 0.0) as cost_gemini
        FROM conversations
      `).get(pStart, gStart);
    } else {
      breakdown = db.prepare(`
        SELECT
          COALESCE(SUM(CASE
            WHEN LOWER(model_name) LIKE '%claude%'
              OR LOWER(model_name) LIKE '%sonnet%'
              OR LOWER(model_name) LIKE '%opus%'
              OR LOWER(model_name) LIKE '%gpt%'
              OR LOWER(model_name) LIKE '%o1%'
              OR LOWER(model_name) LIKE '%o3%'
            THEN billed_cost ELSE 0 END), 0.0) as cost_3p,
          COALESCE(SUM(CASE
            WHEN NOT (LOWER(model_name) LIKE '%claude%'
              OR LOWER(model_name) LIKE '%sonnet%'
              OR LOWER(model_name) LIKE '%opus%'
              OR LOWER(model_name) LIKE '%gpt%'
              OR LOWER(model_name) LIKE '%o1%'
              OR LOWER(model_name) LIKE '%o3%')
            THEN billed_cost ELSE 0 END), 0.0) as cost_gemini
        FROM conversations
      `).get();
    }

    if (breakdown) {
      costGemini = Number(breakdown.cost_gemini) || 0.0;
      cost3p = Number(breakdown.cost_3p) || 0.0;
    }
  } catch {
    costGemini = totalCost;
  }

  const weeklyCost = costGemini + cost3p;
  const costMap = { gemini: costGemini, '3p': cost3p };
  const quotaSummary = getQuotaSummary(db, 'Gemini 3.7 Flash', weeklyCost, costMap, now);

  let globalVelocity = null;
  if (db) {
    try {
      globalVelocity = queryVelocityMetrics(db, { now });
    } catch {
      globalVelocity = null;
    }
  }

  const activeContextTpm = globalVelocity?.activeContextTpm ?? 0.0;
  const activeBilledTpm = globalVelocity?.activeBilledTpm ?? 0.0;
  const pairingBilledTph = globalVelocity?.pairingBilledTph ?? 0.0;
  const rolling60mBilledTph = globalVelocity?.rolling60mBilledTph ?? 0.0;

  return {
    total_conversations: totalConversations,
    root_conversations: rootConversations,
    subagent_conversations: subagentConversations,
    total_tokens: totalTokens,
    total_cost: totalCost,
    weekly_cost: weeklyCost,
    total_compactions: totalCompactions,
    total_pruned_tokens: totalPrunedTokens,
    weekly_quota_pct: quotaSummary.session_weekly_pct,
    activeContextTpm,
    activeBilledTpm,
    pairingBilledTph,
    rolling60mBilledTph,
    velocity: globalVelocity,
    quota: quotaSummary
  };
}

export const queryGlobal = getGlobalData;

