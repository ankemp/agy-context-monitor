import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const MODEL_LIMIT_RULES = [
  { match: ['pro'], limit: 2000000 },
  { match: ['flash', 'gemini'], limit: 1000000 },
  { match: ['claude'], limit: 200000 },
  { match: ['gpt', 'o1', 'o3'], limit: 128000 }
];

export function getModelContextLimit(modelName) {
  const lower = (modelName || '').toLowerCase();
  for (const rule of MODEL_LIMIT_RULES) {
    if (rule.match.some(m => lower.includes(m))) {
      return rule.limit;
    }
  }
  return 1000000;
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

export function createDefaultSession(cid = null, title = 'No conversation records', modelName = 'Gemini 3.7 Flash') {
  const maxTokens = getModelContextLimit(modelName);
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
    top_steps: []
  };
}

export function getSessionData(db, cid) {
  if (!db) {
    return createDefaultSession(cid, 'No analytics database', 'Unknown');
  }

  let conv = null;
  if (cid) {
    conv = db.prepare(`
      SELECT id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
             duration_seconds, billed_cost, compaction_count, pruned_tokens
      FROM conversations
      WHERE id = ?
    `).get(cid);

    // If a specific conversation was requested but is not found yet in SQLite,
    // do NOT fall back to another conversation (prevents cross-session data leakage).
    if (!conv) {
      return createDefaultSession(cid, 'Active Session (indexing...)', 'Gemini 3.7 Flash');
    }
  } else {
    // No specific cid passed; fetch latest active conversation
    conv = db.prepare(`
      SELECT id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
             duration_seconds, billed_cost, compaction_count, pruned_tokens
      FROM conversations
      ORDER BY updated_at DESC
      LIMIT 1
    `).get();

    if (!conv) {
      return createDefaultSession(null, 'No conversation records', 'Gemini 3.7 Flash');
    }
  }

  const resolvedCid = conv.id;

  // Determine active context start from checkpoint step if present
  const cpRow = db.prepare(`
    SELECT MAX(step_index) as cp_step
    FROM steps
    WHERE conversation_id = ? AND step_type = 'CHECKPOINT'
  `).get(resolvedCid);

  const sinceStep = (cpRow && cpRow.cp_step !== null && cpRow.cp_step !== undefined)
    ? cpRow.cp_step
    : 0;

  const rangeRow = db.prepare(`
    SELECT MIN(step_index) as min_step, MAX(step_index) as max_step, COUNT(*) as step_count, SUM(est_tokens) as active_tokens
    FROM steps
    WHERE conversation_id = ? AND step_index >= ?
  `).get(resolvedCid, sinceStep);

  const activeTokens = rangeRow?.active_tokens ?? conv.total_tokens ?? 0;
  const maxTokens = getModelContextLimit(conv.model_name);
  const pct = maxTokens ? (activeTokens / maxTokens) * 100 : 0;

  const categories = db.prepare(`
    SELECT category, SUM(est_tokens) as tokens
    FROM steps
    WHERE conversation_id = ? AND step_index >= ?
    GROUP BY category
    ORDER BY tokens DESC
  `).all(resolvedCid, sinceStep);

  const topSteps = db.prepare(`
    SELECT step_index, activity, tool_or_source, est_tokens, detail
    FROM steps
    WHERE conversation_id = ? AND step_index >= ?
    ORDER BY est_tokens DESC
    LIMIT 3
  `).all(resolvedCid, sinceStep);

  return {
    cid: resolvedCid,
    title: conv.title || 'Active Session',
    project: conv.project,
    model_name: conv.model_name || 'Gemini 3.7 Flash',
    max_tokens: maxTokens,
    active_tokens: activeTokens,
    pct: Math.round(pct * 10) / 10,
    total_tokens: conv.total_tokens || activeTokens,
    total_steps: conv.total_steps || (rangeRow?.step_count ?? 0),
    billed_cost: conv.billed_cost || 0.0,
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
    top_steps: topSteps.map(s => ({
      step_index: s.step_index,
      activity: s.activity,
      tool_or_source: s.tool_or_source,
      est_tokens: s.est_tokens,
      detail: s.detail
    }))
  };
}

export function getGlobalData(db) {
  if (!db) {
    return {
      total_conversations: 0,
      total_tokens: 0,
      total_cost: 0,
      total_compactions: 0,
      total_pruned_tokens: 0
    };
  }

  const row = db.prepare(`
    SELECT COUNT(*) as total_conversations,
           SUM(total_tokens) as total_tokens,
           SUM(billed_cost) as total_cost,
           SUM(compaction_count) as total_compactions,
           SUM(pruned_tokens) as total_pruned_tokens
    FROM conversations
  `).get();

  return {
    total_conversations: row?.total_conversations || 0,
    total_tokens: row?.total_tokens || 0,
    total_cost: row?.total_cost || 0.0,
    total_compactions: row?.total_compactions || 0,
    total_pruned_tokens: row?.total_pruned_tokens || 0
  };
}
