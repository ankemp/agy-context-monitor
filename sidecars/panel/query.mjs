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

export function getQuotaSummary(db, modelName = 'Gemini 3.7 Flash', sessionCost = 0, costMap = null) {
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
    const resetCountdown = formatResetCountdown(resetTime);
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
        reset_countdown: formatResetCountdown(resetTime),
        calibrated_capacity: cap,
        session_impact_pct: sessionImpactPct,
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
    session_impact_pct: 0
  };
  const active5h = buckets[fiveHKey] || {
    remaining_pct: 100.0,
    reset_time: '',
    reset_countdown: '',
    calibrated_capacity: prefix === '3p' ? 70.0 : 236.7,
    session_impact_pct: 0
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

export function createDefaultSession(cid = null, title = 'No conversation records', modelName = 'Gemini 3.7 Flash', db = null) {
  const maxTokens = getModelContextLimit(modelName);
  const quotaSummary = getQuotaSummary(db, modelName, 0);
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
    quota: quotaSummary
  };
}

export function getSessionData(db, cid) {
  if (!db) {
    return createDefaultSession(cid, 'No analytics database', 'Unknown', null);
  }

  let conv = null;
  try {
    if (cid) {
      conv = db.prepare(`
        SELECT *
        FROM conversations
        WHERE id = ?
      `).get(cid);

      // If a specific conversation was requested but is not found yet in SQLite,
      // do NOT fall back to another conversation (prevents cross-session data leakage).
      if (!conv) {
        return createDefaultSession(cid, 'Active Session (indexing...)', 'Gemini 3.7 Flash', db);
      }
    } else {
      // No specific cid passed; fetch latest active conversation
      conv = db.prepare(`
        SELECT *
        FROM conversations
        ORDER BY updated_at DESC
        LIMIT 1
      `).get();

      if (!conv) {
        return createDefaultSession(null, 'No conversation records', 'Gemini 3.7 Flash', db);
      }
    }
  } catch {
    // conversations table might not exist yet
    return createDefaultSession(cid, 'No analytics database', 'Gemini 3.7 Flash', db);
  }

  const resolvedCid = conv.id;
  const modelName = conv.model_name || 'Gemini 3.7 Flash';
  const billedCost = conv.billed_cost || 0.0;

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

  const activeTokens = rangeRow?.active_tokens ?? conv.total_tokens ?? 0;
  const maxTokens = getModelContextLimit(modelName);
  const pct = maxTokens ? (activeTokens / maxTokens) * 100 : 0;

  const quotaSummary = getQuotaSummary(db, modelName, billedCost);
  const cap = quotaSummary.calibrated_weekly_capacity || DEFAULT_CAPACITIES['gemini-weekly'];

  return {
    cid: resolvedCid,
    title: conv.title || 'Active Session',
    project: conv.project,
    model_name: modelName,
    max_tokens: maxTokens,
    active_tokens: activeTokens,
    pct: Math.round(pct * 10) / 10,
    total_tokens: conv.total_tokens || activeTokens,
    total_steps: conv.total_steps || (rangeRow?.step_count ?? 0),
    billed_cost: billedCost,
    compaction_count: conv.compaction_count || 0,
    pruned_tokens: conv.pruned_tokens || 0,
    compaction_detected: (conv.compaction_count || 0) > 0,
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
    quota: quotaSummary
  };
}

export function getGlobalData(db) {
  if (!db) {
    const quotaSummary = getQuotaSummary(null, 'Gemini 3.7 Flash', 0);
    return {
      total_conversations: 0,
      total_tokens: 0,
      total_cost: 0,
      total_compactions: 0,
      total_pruned_tokens: 0,
      weekly_quota_pct: 0,
      quota: quotaSummary
    };
  }

  let totalConversations = 0;
  let totalTokens = 0;
  let totalCost = 0.0;
  let totalCompactions = 0;
  let totalPrunedTokens = 0;
  let costGemini = 0.0;
  let cost3p = 0.0;

  try {
    const baseRow = db.prepare(`
      SELECT COUNT(*) as total_conversations,
             COALESCE(SUM(total_tokens), 0) as total_tokens,
             COALESCE(SUM(billed_cost), 0.0) as total_cost
      FROM conversations
    `).get();

    if (baseRow) {
      totalConversations = Number(baseRow.total_conversations) || 0;
      totalTokens = Number(baseRow.total_tokens) || 0;
      totalCost = Number(baseRow.total_cost) || 0.0;
    }
  } catch {
    // conversations table missing or empty
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
  const quotaSummary = getQuotaSummary(db, 'Gemini 3.7 Flash', weeklyCost, costMap);

  return {
    total_conversations: totalConversations,
    total_tokens: totalTokens,
    total_cost: totalCost,
    weekly_cost: weeklyCost,
    total_compactions: totalCompactions,
    total_pruned_tokens: totalPrunedTokens,
    weekly_quota_pct: quotaSummary.session_weekly_pct,
    quota: quotaSummary
  };
}

