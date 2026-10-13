// Frontend client logic for Antigravity Context Monitor side panel.
//
// Communicates with the sidecar backend via /api/session or /api/global,
// and dispatches action prompts to the active Antigravity conversation.

const sidecar = window.sidecar;
const $ = (id) => document.getElementById(id);

let isInitialized = false;
let pollTimer = null;

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

  container.innerHTML = topSteps.map(s => `
    <div class="step-item">
      <div class="step-header">
        <span class="step-source">#${s.step_index} · ${escapeHtml(s.activity || s.tool_or_source)}</span>
        <span class="step-tokens">~${formatTokens(s.est_tokens)} tok</span>
      </div>
      <div class="step-detail small" title="${escapeHtml(s.detail || '')}">${escapeHtml(s.detail || 'Step details')}</div>
    </div>
  `).join('');
}

async function refreshTelemetry() {
  // Respect visibility state: pause work when side panel or tab is hidden
  if (document.hidden) return;

  const cid = sidecar ? sidecar.conversationId : null;
  const statusBadge = $('status-badge');
  const globalCard = $('global-card');
  const capacityCard = $('capacity-card');
  const topStepsCard = $('top-steps-card');
  const banner = $('compaction-banner');
  const actionsCard = $('actions-card');

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

      $('global-convs').textContent = `${globalData.total_conversations} Sessions`;
      $('global-tokens').textContent = formatTokens(globalData.total_tokens);
      $('global-cost').textContent = formatCost(globalData.total_cost);
      $('global-compactions').textContent = `${globalData.total_compactions} events`;
      $('global-pruned').textContent = `~${formatTokens(globalData.total_pruned_tokens)} tokens`;
      return;
    }

    // Active Conversation Telemetry
    if (globalCard) globalCard.hidden = true;
    if (capacityCard) capacityCard.hidden = false;
    if (topStepsCard) topStepsCard.hidden = false;
    if (actionsCard) actionsCard.hidden = false;

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
    .replace(/"/g, '&quot;');
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
