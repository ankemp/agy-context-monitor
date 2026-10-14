// Frontend client logic for Antigravity Context Monitor side panel.
//
// Communicates with the sidecar backend via /api/session or /api/global,
// and dispatches action prompts to the active Antigravity conversation.

const sidecar = window.sidecar;
const $ = (id) => document.getElementById(id);

let isInitialized = false;
let pollTimer = null;
let activeQuota = null;
let activeQuotaFamily = 'gemini';
let userSelectedQuotaFamily = false;
let lastCid = undefined;
let isGlobalMode = false;

function formatTokens(n) {
  const num = Number(n) || 0;
  if (num >= 1000000) return (num / 1000000).toFixed(2) + 'M';
  if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
  return String(Math.round(num));
}

function formatCost(n) {
  const num = Number(n) || 0;
  return `$${num.toFixed(2)}`;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3000);
}

function getCategoryColor(category) {
  const cat = (category || '').toLowerCase();
  if (cat.includes('reasoning') || cat.includes('planner')) return { cssClass: 'reasoning', color: '#1a73e8' };
  if (cat.includes('file') || cat.includes('tool')) return { cssClass: 'tools', color: '#12b5cb' };
  if (cat.includes('summary') || cat.includes('checkpoint')) return { cssClass: 'summary', color: '#a142f4' };
  if (cat.includes('user')) return { cssClass: 'user', color: '#34a853' };
  if (cat.includes('system') || cat.includes('ephemeral')) return { cssClass: 'system', color: '#f9ab00' };
  return { cssClass: 'other', color: '#9aa0a6' };
}

async function callApi(path) {
  const fetchFn = (sidecar && typeof sidecar.fetch === 'function')
    ? sidecar.fetch.bind(sidecar)
    : fetch;

  const res = await fetchFn(path);
  if (!res.ok) {
    throw new Error(`API returned HTTP ${res.status}`);
  }
  return res.json();
}

function renderMultiSegmentBar(container, categories, maxTokens, totalPct) {
  if (!container) return;
  container.innerHTML = '';

  if (!categories || categories.length === 0 || !maxTokens) {
    const singleSegment = document.createElement('div');
    singleSegment.className = 'bar-segment other';
    singleSegment.style.width = `${Math.min(100, Math.max(0, totalPct || 0))}%`;
    container.appendChild(singleSegment);
    return;
  }

  // Multi-segment rendering proportional to maxTokens
  for (const cat of categories) {
    const segWidth = (cat.tokens / maxTokens) * 100;
    if (segWidth <= 0.05) continue;
    const { cssClass, color } = getCategoryColor(cat.category);
    const seg = document.createElement('div');
    seg.className = `bar-segment ${cssClass}`;
    seg.style.width = `${segWidth}%`;
    seg.style.backgroundColor = color;
    seg.title = `${cat.category}: ~${formatTokens(cat.tokens)} (${cat.share.toFixed(1)}%)`;
    container.appendChild(seg);
  }
}

function renderQuota(quotaData, sessionImpactPct) {
  if (!quotaData) return;
  activeQuota = quotaData;

  // Auto-switch to model's prefix if user hasn't explicitly clicked a tab
  if (!userSelectedQuotaFamily && quotaData.prefix) {
    activeQuotaFamily = quotaData.prefix;
  }

  const tabGemini = $('quota-tab-gemini');
  const tab3p = $('quota-tab-3p');
  if (tabGemini) {
    tabGemini.classList.toggle('active', activeQuotaFamily === 'gemini');
    tabGemini.setAttribute('aria-selected', String(activeQuotaFamily === 'gemini'));
  }
  if (tab3p) {
    tab3p.classList.toggle('active', activeQuotaFamily === '3p');
    tab3p.setAttribute('aria-selected', String(activeQuotaFamily === '3p'));
  }

  const weeklyBucket = quotaData.buckets ? quotaData.buckets[`${activeQuotaFamily}-weekly`] : null;
  const fiveHBucket = quotaData.buckets ? quotaData.buckets[`${activeQuotaFamily}-5h`] : null;

  const rawRemWk = weeklyBucket?.remaining_pct ?? quotaData.weekly_remaining_pct ?? 100.0;
  const remWk = Number.isFinite(rawRemWk) ? rawRemWk : 100.0;
  const resetWk = weeklyBucket?.reset_countdown ?? quotaData.weekly_reset_countdown ?? '';
  const capWk = weeklyBucket?.calibrated_capacity ?? quotaData.calibrated_weekly_capacity ?? (activeQuotaFamily === 'gemini' ? 672.0 : 200.0);

  const rawRem5h = fiveHBucket?.remaining_pct ?? quotaData.five_hour_remaining_pct ?? 100.0;
  const rem5h = Number.isFinite(rawRem5h) ? rawRem5h : 100.0;
  const reset5h = fiveHBucket?.reset_countdown ?? quotaData.five_hour_reset_countdown ?? '';
  const cap5h = fiveHBucket?.calibrated_capacity ?? quotaData.calibrated_5h_capacity ?? (activeQuotaFamily === 'gemini' ? 236.7 : 70.0);

  const sessionImpact = weeklyBucket?.session_impact_pct ?? sessionImpactPct ?? quotaData.session_weekly_pct ?? 0.0;

  // Session impact badge & header badge
  const impactEl = $('quota-session-impact');
  if (impactEl) {
    const label = isGlobalMode ? 'fleet total' : 'session share';
    impactEl.textContent = `${Number(sessionImpact).toFixed(2)}% ${label}`;
    impactEl.className = `badge ${sessionImpact >= 10 ? 'error' : sessionImpact >= 5 ? 'warning' : 'ok'}`;
  }
  const quotaBadge = $('quota-badge');
  if (quotaBadge) {
    const headerPct = isGlobalMode ? sessionImpact : (quotaData.session_weekly_pct ?? sessionImpact);
    const suffix = isGlobalMode ? 'fleet' : 'wk';
    quotaBadge.textContent = `${Number(headerPct).toFixed(2)}% ${suffix}`;
    quotaBadge.className = `badge ${headerPct >= 10 ? 'error' : headerPct >= 5 ? 'warning' : 'secondary'}`;
  }

  // Weekly bucket
  const wkPctEl = $('quota-weekly-pct');
  if (wkPctEl) {
    wkPctEl.textContent = `${Number(remWk).toFixed(1)}% remaining`;
    wkPctEl.className = `quota-val ${remWk <= 10 ? 'error' : remWk <= 25 ? 'warning' : 'ok'}`;
  }
  const wkResetEl = $('quota-weekly-reset');
  if (wkResetEl) {
    wkResetEl.textContent = resetWk ? `resets in ${resetWk}` : '';
  }
  const wkSubEl = $('quota-weekly-sub');
  if (wkSubEl) {
    wkSubEl.textContent = `Calibrated capacity: $${Number(capWk).toFixed(2)}`;
  }
  const wkBarEl = $('quota-weekly-bar');
  if (wkBarEl) {
    wkBarEl.style.width = `${Math.min(100, Math.max(0, remWk))}%`;
    wkBarEl.className = `quota-bar ${remWk <= 10 ? 'error' : remWk <= 25 ? 'warning' : 'ok'}`;
  }

  // 5-Hour bucket
  const fhPctEl = $('quota-5h-pct');
  if (fhPctEl) {
    fhPctEl.textContent = `${Number(rem5h).toFixed(1)}% remaining`;
    fhPctEl.className = `quota-val ${rem5h <= 10 ? 'error' : rem5h <= 25 ? 'warning' : 'ok'}`;
  }
  const fhResetEl = $('quota-5h-reset');
  if (fhResetEl) {
    fhResetEl.textContent = reset5h ? `resets in ${reset5h}` : '';
  }
  const fhSubEl = $('quota-5h-sub');
  if (fhSubEl) {
    fhSubEl.textContent = `Calibrated capacity: $${Number(cap5h).toFixed(2)}`;
  }
  const fhBarEl = $('quota-5h-bar');
  if (fhBarEl) {
    fhBarEl.style.width = `${Math.min(100, Math.max(0, rem5h))}%`;
    fhBarEl.className = `quota-bar ${rem5h <= 10 ? 'error' : rem5h <= 25 ? 'warning' : 'ok'}`;
  }
}

function renderCategories(container, categories) {
  if (!container) return;
  if (!categories || categories.length === 0) {
    container.innerHTML = '<div class="empty-state small muted">No category data</div>';
    return;
  }

  container.innerHTML = categories.map(c => {
    const { color } = getCategoryColor(c.category);
    return `
      <div class="category-item">
        <span class="cat-label">
          <span class="cat-dot" style="background-color: ${color}"></span>
          <span title="${escapeHtml(c.category)}">${escapeHtml(c.category)}</span>
        </span>
        <span class="cat-val small muted">~${formatTokens(c.tokens)} (${c.share.toFixed(1)}%)</span>
      </div>
    `;
  }).join('');
}

function renderTopSteps(container, topSteps, rangeSpan, minStep, maxStep) {
  if (rangeSpan) {
    rangeSpan.textContent = `Steps #${minStep}–#${maxStep}`;
  }
  if (!container) return;
  if (!topSteps || topSteps.length === 0) {
    container.innerHTML = '<div class="empty-state small muted">No steps recorded</div>';
    return;
  }

  container.innerHTML = topSteps.map(s => {
    const quotaStr = (s.weekly_quota_pct !== undefined && s.weekly_quota_pct !== null && s.weekly_quota_pct > 0)
      ? ` · ${Number(s.weekly_quota_pct).toFixed(2)}% wk`
      : '';
    return `
      <div class="step-item">
        <div class="step-header">
          <span class="step-source">#${s.step_index} · ${escapeHtml(s.activity || s.tool_or_source)}</span>
          <span class="step-tokens">~${formatTokens(s.est_tokens)} tok${quotaStr}</span>
        </div>
        <div class="step-detail small" title="${escapeHtml(s.detail || '')}">${escapeHtml(s.detail || 'Step details')}</div>
      </div>
    `;
  }).join('');
}

async function refreshTelemetry() {
  // Respect visibility state: pause work when side panel or tab is hidden
  if (document.hidden) return;

  const cid = sidecar ? sidecar.conversationId : null;
  if (lastCid !== cid) {
    lastCid = cid;
    userSelectedQuotaFamily = false;
  }
  isGlobalMode = !cid;

  const statusBadge = $('status-badge');
  const globalCard = $('global-card');
  const capacityCard = $('capacity-card');
  const topStepsCard = $('top-steps-card');
  const banner = $('compaction-banner');
  const actionsCard = $('actions-card');
  const quotaCard = $('quota-card');

  try {
    if (!cid) {
      // Global Aggregate Overview
      const globalData = await callApi('/api/global');
      if (statusBadge) {
        statusBadge.textContent = 'Live';
        statusBadge.className = 'badge ok';
      }
      $('model-badge').textContent = 'Global Overview';
      $('cost-badge').textContent = formatCost(globalData.total_cost);

      if (globalCard) globalCard.hidden = false;
      if (capacityCard) capacityCard.hidden = true;
      if (topStepsCard) topStepsCard.hidden = true;
      if (banner) banner.hidden = true;
      if (actionsCard) actionsCard.hidden = true;
      if (quotaCard) quotaCard.hidden = false;

      $('global-convs').textContent = `${globalData.total_conversations} Sessions`;
      $('global-tokens').textContent = formatTokens(globalData.total_tokens);
      $('global-cost').textContent = formatCost(globalData.total_cost);
      const globalQuotaEl = $('global-quota');
      if (globalQuotaEl) {
        globalQuotaEl.textContent = `${(globalData.weekly_quota_pct || 0).toFixed(2)}% wk`;
      }
      $('global-compactions').textContent = `${globalData.total_compactions} events`;
      $('global-pruned').textContent = `~${formatTokens(globalData.total_pruned_tokens)} tokens`;

      if (globalData.quota) {
        renderQuota(globalData.quota, globalData.weekly_quota_pct);
      }
      return;
    }

    // Active Conversation Telemetry
    if (globalCard) globalCard.hidden = true;
    if (capacityCard) capacityCard.hidden = false;
    if (topStepsCard) topStepsCard.hidden = false;
    if (actionsCard) actionsCard.hidden = false;
    if (quotaCard) quotaCard.hidden = false;

    const sessionData = await callApi(`/api/session?cid=${encodeURIComponent(cid)}`);

    if (statusBadge) {
      statusBadge.textContent = 'Live';
      statusBadge.className = 'badge ok';
    }
    $('model-badge').textContent = sessionData.model_name || 'Gemini 3.7 Flash';
    $('cost-badge').textContent = formatCost(sessionData.billed_cost);

    $('capacity-pct').textContent = `${(sessionData.pct || 0).toFixed(1)}%`;
    $('capacity-sub').textContent = `${formatTokens(sessionData.active_tokens)} / ${formatTokens(sessionData.max_tokens)} tokens`;

    // Multi-segment capacity bar
    renderMultiSegmentBar($('capacity-bar'), sessionData.categories, sessionData.max_tokens, sessionData.pct);

    // Subscription Quota rendering
    if (sessionData.quota) {
      renderQuota(sessionData.quota, sessionData.weekly_quota_pct);
    }

    // Compaction banner
    if (banner) {
      if (sessionData.compaction_detected) {
        banner.hidden = false;
        $('compaction-count').textContent = `${sessionData.compaction_count} ${sessionData.compaction_count === 1 ? 'event' : 'events'}`;
        $('compaction-pruned').textContent = `~${formatTokens(sessionData.pruned_tokens)} freed`;
        $('compaction-window').textContent = `Steps #${sessionData.min_step}–#${sessionData.max_step} active`;
      } else {
        banner.hidden = true;
      }
    }

    // Category breakdown
    renderCategories($('category-list'), sessionData.categories);

    // Top steps
    renderTopSteps($('top-steps-list'), sessionData.top_steps, $('steps-range'), sessionData.min_step, sessionData.max_step);

  } catch (err) {
    if (statusBadge) {
      statusBadge.textContent = 'Offline';
      statusBadge.className = 'badge error';
    }
    console.error('Context Monitor poll failed:', err);
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function sendPrompt(promptText, actionName) {
  if (!sidecar || !sidecar.agent || !sidecar.conversationId) {
    toast('No active conversation connected', true);
    return;
  }
  try {
    await sidecar.agent.sendMessage(promptText);
    toast(`${actionName} sent to agent`);
  } catch (err) {
    toast(`Failed to send prompt: ${err.message}`, true);
  }
}

function init() {
  if (isInitialized) return;
  isInitialized = true;

  const btnAudit = $('btn-audit');
  if (btnAudit) {
    btnAudit.addEventListener('click', () => {
      sendPrompt(
        'Audit recent file reads, search outputs, and tool executions in this conversation. Identify any redundant operations and estimate token savings.',
        'Audit request'
      );
    });
  }

  const btnPlan = $('btn-plan');
  if (btnPlan) {
    btnPlan.addEventListener('click', () => {
      sendPrompt(
        'Review current context window utilization and breakdown. Provide recommendations to optimize context efficiency.',
        'Optimization request'
      );
    });
  }

  const btnCompact = $('btn-compact');
  if (btnCompact) {
    btnCompact.addEventListener('click', () => {
      sendPrompt(
        'Summarize recent context compaction events, how many tokens were pruned, and explain the current active memory boundary.',
        'Compaction review'
      );
    });
  }

  const tabGemini = $('quota-tab-gemini');
  if (tabGemini) {
    tabGemini.addEventListener('click', () => {
      userSelectedQuotaFamily = true;
      activeQuotaFamily = 'gemini';
      if (activeQuota) renderQuota(activeQuota);
    });
  }

  const tab3p = $('quota-tab-3p');
  if (tab3p) {
    tab3p.addEventListener('click', () => {
      userSelectedQuotaFamily = true;
      activeQuotaFamily = '3p';
      if (activeQuota) renderQuota(activeQuota);
    });
  }

  // Handle visibility changes: halt polling when hidden, resume when visible
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshTelemetry();
    }
  });

  // Initial fetch and lifecycle-managed polling interval
  refreshTelemetry();
  pollTimer = setInterval(() => {
    if (!document.hidden) {
      refreshTelemetry();
    }
  }, 4000);
}

document.addEventListener('DOMContentLoaded', init);
if (document.readyState === 'interactive' || document.readyState === 'complete') {
  init();
}
