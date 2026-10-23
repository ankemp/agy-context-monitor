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
let inspectedCid = null;
let activeFleet = null;
let activeSessionImpact = null;

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

function shortenModelName(name) {
  if (!name) return 'Gemini';
  let clean = String(name).trim();
  const lower = clean.toLowerCase();
  if (lower.startsWith('gemini ')) {
    clean = clean.substring(7).trim();
  } else if (lower.startsWith('gemini-')) {
    clean = clean.substring(7).trim();
  } else if (lower.startsWith('claude ')) {
    clean = clean.substring(7).trim();
  } else if (lower.startsWith('claude-')) {
    clean = clean.substring(7).trim();
  }
  clean = clean.replace(/\(Medium\)/i, '(Med)');
  if (clean.length > 20) {
    clean = clean.slice(0, 19) + '…';
  }
  return clean;
}

function updateCostHeaderBadge(localCost, fleet, isGlobal = isGlobalMode) {
  const badge = $('cost-badge');
  if (!badge) return;
  if (typeof fleet === 'boolean') {
    isGlobal = fleet;
    fleet = arguments[2] || null;
  }
  if (!isGlobal && fleet?.is_swarm) {
    badge.textContent = `${formatCost(localCost)} · ${formatCost(fleet.total_cost)} fleet`;
    badge.title = 'Session Cost · Fleet Cost';
  } else {
    badge.textContent = formatCost(localCost);
    badge.title = isGlobal ? 'Total Fleet Cost' : 'Estimated Value';
  }
}

function updateQuotaHeaderBadge(localPct, isGlobal = isGlobalMode, fleet = null) {
  const badge = $('quota-badge');
  if (!badge) return;
  if (typeof isGlobal === 'object' && isGlobal !== null) {
    fleet = isGlobal;
    isGlobal = isGlobalMode;
  }
  const localNum = Number(localPct) || 0;
  if (!isGlobal && fleet?.is_swarm) {
    const fleetNum = Number(fleet.weekly_quota_pct) || 0;
    const thresholdNum = Math.max(localNum, fleetNum);
    const cssClass = thresholdNum >= 10 ? 'error' : (thresholdNum >= 5 ? 'warning' : 'ok');
    badge.textContent = `${localNum.toFixed(2)}% · ${fleetNum.toFixed(2)}% fleet wk`;
    badge.title = 'Session Quota · Fleet Quota Impact';
    badge.className = `badge ${cssClass}`;
    return;
  }
  const num = localNum;
  const suffix = isGlobal ? 'fleet' : 'wk';
  const title = isGlobal ? 'Fleet Quota Impact' : 'Session Quota Impact';
  const cssClass = num >= 10 ? 'error' : (num >= 5 ? 'warning' : 'ok');
  badge.textContent = `${num.toFixed(2)}% ${suffix}`;
  badge.title = title;
  badge.className = `badge ${cssClass}`;
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

async function fetchSessionData(cid, inspectCid = null) {
  let url = `/api/session?cid=${encodeURIComponent(cid)}`;
  if (inspectCid) {
    url += `&inspect_cid=${encodeURIComponent(inspectCid)}`;
  }
  return callApi(url);
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

function renderQuota(quotaData, sessionImpactPct, fleet = null) {
  if (!quotaData) return;
  activeQuota = quotaData;
  if (sessionImpactPct !== undefined) activeSessionImpact = sessionImpactPct;
  if (fleet !== undefined) activeFleet = fleet;
  const currentFleet = fleet || activeFleet;

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

  // Session impact badge
  const impactEl = $('quota-session-impact');
  if (impactEl) {
    const sNum = Number(sessionImpact) || 0.0;
    if (!isGlobalMode && currentFleet?.is_swarm) {
      const fNum = Number(currentFleet.weekly_quota_pct) || 0.0;
      impactEl.textContent = `${sNum.toFixed(2)}% session · ${fNum.toFixed(2)}% fleet`;
      const maxImpact = Math.max(sNum, fNum);
      impactEl.className = `badge ${maxImpact >= 10 ? 'error' : maxImpact >= 5 ? 'warning' : 'ok'}`;
    } else {
      const label = isGlobalMode ? 'fleet total' : 'session share';
      impactEl.textContent = `${sNum.toFixed(2)}% ${label}`;
      impactEl.className = `badge ${sNum >= 10 ? 'error' : sNum >= 5 ? 'warning' : 'ok'}`;
    }
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

  // Quota burn rate tok/hr
  const qWeeklyTph = $('quotaWeeklyTph');
  if (qWeeklyTph) {
    const tphVal = weeklyBucket?.billed_tph ?? (quotaData?.weekly_billed_tph ?? 0);
    qWeeklyTph.textContent = `${formatTokens(tphVal)} tok/hr`;
  }
  const q5hTph = $('quota5hTph');
  if (q5hTph) {
    const tphVal = fiveHBucket?.billed_tph ?? (quotaData?.five_hour_billed_tph ?? 0);
    q5hTph.textContent = `${formatTokens(tphVal)} tok/hr`;
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

function renderGlobal(globalData) {
  const statusBadge = $('status-badge');
  const globalCard = $('global-card');
  const capacityCard = $('capacity-card');
  const topStepsCard = $('top-steps-card');
  const banner = $('compaction-banner');
  const actionsCard = $('actions-card');
  const quotaCard = $('quota-card');
  const fleetCard = $('fleet-card');
  const inspectBanner = $('inspect-banner');

  if (statusBadge) {
    statusBadge.textContent = 'Live';
    statusBadge.className = 'badge ok';
  }
  const modelBadge = $('model-badge');
  if (modelBadge) modelBadge.textContent = 'Global Overview';
  updateCostHeaderBadge(globalData.total_cost, null, true);
  const globalQuotaPct = globalData.weekly_quota_pct ?? (globalData.quota?.session_weekly_pct ?? 0);
  updateQuotaHeaderBadge(globalQuotaPct, true);

  if (globalCard) globalCard.hidden = false;
  if (capacityCard) capacityCard.hidden = true;
  if (topStepsCard) topStepsCard.hidden = true;
  if (banner) banner.hidden = true;
  if (actionsCard) actionsCard.hidden = true;
  if (quotaCard) quotaCard.hidden = false;
  if (fleetCard) fleetCard.hidden = true;
  if (inspectBanner) inspectBanner.hidden = true;

  const convsEl = $('global-convs');
  if (convsEl) {
    const subCount = globalData.subagent_conversations || 0;
    const rootCount = globalData.root_conversations ?? globalData.total_conversations;
    if (subCount > 0) {
      convsEl.textContent = `${rootCount} Campaigns · ${subCount} Sub-Agents`;
    } else {
      convsEl.textContent = `${globalData.total_conversations} Sessions`;
    }
  }

  const tokensEl = $('global-tokens');
  if (tokensEl) tokensEl.textContent = formatTokens(globalData.total_tokens);
  const costEl = $('global-cost');
  if (costEl) costEl.textContent = formatCost(globalData.total_cost);
  const globalQuotaEl = $('global-quota');
  if (globalQuotaEl) {
    globalQuotaEl.textContent = `${(globalData.weekly_quota_pct || 0).toFixed(2)}% wk`;
  }
  const compactionsEl = $('global-compactions');
  if (compactionsEl) compactionsEl.textContent = `${globalData.total_compactions} events`;
  const prunedEl = $('global-pruned');
  if (prunedEl) prunedEl.textContent = `~${formatTokens(globalData.total_pruned_tokens)} tokens`;

  const gActiveTpm = $('globalActiveTpm');
  if (gActiveTpm) {
    const val = Number(globalData.activeBilledTpm) || 0;
    gActiveTpm.textContent = `${formatTokens(val)} TPM`;
  }
  const gPairingTph = $('globalPairingTph');
  if (gPairingTph) {
    const val = Number(globalData.pairingBilledTph) || 0;
    gPairingTph.textContent = `${formatTokens(val)} TPH`;
  }

  if (globalData.quota) {
    renderQuota(globalData.quota, globalData.weekly_quota_pct, null);
  }
}

function renderFleetCard(fleet) {
  const card = $('fleet-card');
  const banner = $('inspect-banner');
  const list = $('fleet-node-list');

  const fleetVelBlock = $('fleetVelocityBlock');
  if (!fleet || !fleet.is_swarm) {
    if (card) card.hidden = true;
    if (banner) banner.hidden = true;
    if (fleetVelBlock) fleetVelBlock.hidden = true;
    if (list) list.innerHTML = '';
    inspectedCid = null;
    return;
  }

  if (card) card.hidden = false;
  if (fleetVelBlock) fleetVelBlock.hidden = false;

  const countBadge = $('fleet-count-badge');
  if (countBadge) {
    countBadge.textContent = `${fleet.agent_count} Agents`;
  }

  if (banner) {
    if (fleet.is_inspecting_other) {
      banner.hidden = false;
      const label = $('inspect-banner-label');
      if (label) {
        label.textContent = `Viewing: ${fleet.inspected_role || 'Sub-Agent'}`;
      }
    } else {
      banner.hidden = true;
    }
  }

  const costEl = $('fleet-total-cost');
  if (costEl) costEl.textContent = formatCost(fleet.total_cost);

  const tokensEl = $('fleet-total-tokens');
  if (tokensEl) tokensEl.textContent = formatTokens(fleet.total_tokens);

  const shieldEl = $('fleet-shielding');
  if (shieldEl) {
    const rawRatio = Number(fleet.shielding_ratio);
    const ratioStr = (!isNaN(rawRatio) && rawRatio > 0) ? rawRatio.toFixed(1) : '1.0';
    shieldEl.textContent = `${ratioStr}x`;
  }

  const fleetVel = fleet.velocity || {};
  const fleetTpmEl = $('fleetVelocityTpm');
  if (fleetTpmEl) {
    const fTpm = Number(fleetVel.rolling5mBilledTpm) || 0;
    fleetTpmEl.textContent = `${formatTokens(fTpm)} TPM`;
  }
  const fleetBarEl = $('fleetVelocityBar');
  if (fleetBarEl) {
    const fPct = Math.min(100, Math.max(0, Number(fleetVel.arcPct) || 0));
    fleetBarEl.style.width = `${fPct}%`;
  }

  if (!list) return;
  list.innerHTML = '';

  const cid = (sidecar && sidecar.conversationId) || null;
  const currentInspectedId = inspectedCid || cid;

  const nodes = fleet.nodes || [];
  for (const node of nodes) {
    const isInspected = node.id === currentInspectedId;
    const item = document.createElement('div');
    item.className = `fleet-node-item${isInspected ? ' inspected' : ''}`;
    item.setAttribute('data-node-id', node.id);
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.setAttribute('aria-pressed', String(isInspected));

    const isRoot = Boolean(node.is_root);
    const isActiveSession = Boolean(node.is_active || node.is_active_session);
    let statusBadgeHtml = '';
    if (isRoot) {
      statusBadgeHtml = '<span class="badge root">Root</span>';
    } else if (isActiveSession) {
      statusBadgeHtml = '<span class="badge active">Active</span>';
    }

    const roleName = node.role || node.subagent_role || 'Worker';
    const modelShort = shortenModelName(node.model_name);
    const costText = `${formatCost(node.billed_cost)} · ${Number(node.weekly_quota_pct || 0).toFixed(2)}% wk`;
    const pct = Math.min(100, Math.max(0, Number(node.context_pct) || 0));
    const pctClass = pct >= 80 ? 'error' : (pct >= 50 ? 'warning' : 'ok');
    const activeTok = (node.active_tokens !== undefined && node.active_tokens !== null)
      ? Number(node.active_tokens)
      : (Number(node.total_tokens) || 0);
    const maxTok = Number(node.max_tokens) || 1000000;
    const tokenLabel = `~${formatTokens(activeTok)} / ${formatTokens(maxTok)} (${pct.toFixed(1)}%)`;

    item.innerHTML = `
      <div class="fleet-node-header">
        <div class="fleet-node-meta">
          <span class="fleet-role-pill">${escapeHtml(roleName)}</span>
          ${statusBadgeHtml}
          <span class="badge secondary model">${escapeHtml(modelShort)}</span>
        </div>
        <span class="fleet-node-cost">${costText}</span>
      </div>
      <div class="fleet-node-bar-row">
        <div class="fleet-mini-track">
          <div class="fleet-mini-bar ${pctClass}" style="width: ${pct}%;"></div>
        </div>
        <span class="fleet-node-tokens muted small">${tokenLabel}</span>
      </div>
    `;

    const selectNode = () => selectFleetNode(node.id);

    item.addEventListener('click', selectNode);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectNode();
      }
    });

    list.appendChild(item);
  }
}

function selectFleetNode(nodeId) {
  const activeCid = (sidecar && sidecar.conversationId) || lastCid;
  inspectedCid = (nodeId === activeCid ? null : nodeId);
  refreshTelemetry();
}

function resetInspectedNode() {
  inspectedCid = null;
  refreshTelemetry();
}

if (typeof window !== 'undefined') {
  window.selectFleetNode = selectFleetNode;
  window.resetInspectedNode = resetInspectedNode;
}

async function refreshTelemetry() {
  // Respect visibility state: pause work when side panel or tab is hidden
  if (document.hidden) return;

  const cid = sidecar ? sidecar.conversationId : null;
  if (lastCid !== cid) {
    lastCid = cid;
    userSelectedQuotaFamily = false;
    inspectedCid = null;
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
      renderGlobal(globalData);
      return;
    }

    // Active Conversation Telemetry
    if (globalCard) globalCard.hidden = true;
    if (capacityCard) capacityCard.hidden = false;
    if (topStepsCard) topStepsCard.hidden = false;
    if (actionsCard) actionsCard.hidden = false;
    if (quotaCard) quotaCard.hidden = false;

    let sessionData;
    try {
      sessionData = await fetchSessionData(cid, inspectedCid);
    } catch (fetchErr) {
      if (inspectedCid) {
        console.warn(`Inspected CID ${inspectedCid} failed to load, falling back to active CID ${cid}:`, fetchErr);
        inspectedCid = null;
        sessionData = await fetchSessionData(cid, null);
      } else {
        throw fetchErr;
      }
    }

    if (statusBadge) {
      statusBadge.textContent = 'Live';
      statusBadge.className = 'badge ok';
    }
    const modelBadge = $('model-badge');
    if (modelBadge) modelBadge.textContent = sessionData.model_name || 'Gemini 3.7 Flash';
    updateCostHeaderBadge(sessionData.billed_cost, sessionData.fleet, false);
    const sessionQuotaPct = sessionData.weekly_quota_pct ?? (sessionData.quota?.session_weekly_pct ?? 0);
    updateQuotaHeaderBadge(sessionQuotaPct, false, sessionData.fleet);

    const capPctEl = $('capacity-pct');
    if (capPctEl) capPctEl.textContent = `${(sessionData.pct || 0).toFixed(1)}%`;
    const capSubEl = $('capacity-sub');
    if (capSubEl) capSubEl.textContent = `${formatTokens(sessionData.active_tokens)} / ${formatTokens(sessionData.max_tokens)} tokens`;

    // Multi-segment capacity bar
    renderMultiSegmentBar($('capacity-bar'), sessionData.categories, sessionData.max_tokens, sessionData.pct);

    // Velocity Speedometer arc & Context fill bullet bar
    const vel = sessionData.velocity || {};
    const arcPct = Math.min(100, Math.max(0, Number(vel.arcPct) || 0));
    const dashOffset = 157 * (1 - (arcPct / 100));

    const arcFill = $('velocityArcFill');
    if (arcFill) {
      arcFill.setAttribute('stroke-dashoffset', String(dashOffset.toFixed(1)));
      const arcClass = arcPct >= 80 ? 'error' : (arcPct >= 50 ? 'warning' : 'ok');
      arcFill.setAttribute('class', `arc-fill ${arcClass}`);
    }

    const liveTpm = $('velocityLiveTpm');
    if (liveTpm) {
      const tpmVal = Number(vel.rolling5mBilledTpm) || 0;
      liveTpm.textContent = `${formatTokens(tpmVal)} TPM`;
    }

    const liveTph = $('velocityLiveTph');
    if (liveTph) {
      const tphVal = Number(vel.rolling60mBilledTph) || 0;
      liveTph.textContent = `${formatTokens(tphVal)} TPH`;
    }

    const activeTpm = $('velocityActiveTpm');
    if (activeTpm) {
      const actVal = Number(vel.activeBilledTpm) || 0;
      activeTpm.textContent = `${formatTokens(actVal)} active`;
    }

    const ctxFill = $('contextVelocityFill');
    if (ctxFill) {
      const fillPct = Math.min(100, Math.max(0, Number(vel.contextFillPct) || 0));
      ctxFill.style.width = `${fillPct}%`;
      const ctxClass = fillPct >= 80 ? 'error' : (fillPct >= 50 ? 'warning' : 'ok');
      ctxFill.className = `bullet-bar ${ctxClass}`;
    }

    const ctxTpm = $('contextVelocityTpm');
    if (ctxTpm) {
      const ctxVal = Number(vel.rolling5mContextTpm) || 0;
      ctxTpm.textContent = `${formatTokens(ctxVal)} tok/min`;
    }

    // Subscription Quota rendering
    if (sessionData.quota) {
      renderQuota(sessionData.quota, sessionData.weekly_quota_pct, sessionData.fleet);
    }

    // Swarm Fleet card
    renderFleetCard(sessionData.fleet);

    // Compaction banner
    if (banner) {
      if (sessionData.compaction_detected) {
        banner.hidden = false;
        const compCountEl = $('compaction-count');
        if (compCountEl) compCountEl.textContent = `${sessionData.compaction_count} ${sessionData.compaction_count === 1 ? 'event' : 'events'}`;
        const compPrunedEl = $('compaction-pruned');
        if (compPrunedEl) compPrunedEl.textContent = `~${formatTokens(sessionData.pruned_tokens)} freed`;
        const compWindowEl = $('compaction-window');
        if (compWindowEl) compWindowEl.textContent = `Steps #${sessionData.min_step}–#${sessionData.max_step} active`;
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
  if (str === undefined || str === null) return '';
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
      if (activeQuota) renderQuota(activeQuota, activeSessionImpact, activeFleet);
    });
  }

  const tab3p = $('quota-tab-3p');
  if (tab3p) {
    tab3p.addEventListener('click', () => {
      userSelectedQuotaFamily = true;
      activeQuotaFamily = '3p';
      if (activeQuota) renderQuota(activeQuota, activeSessionImpact, activeFleet);
    });
  }

  const btnResetInspect = $('btn-reset-inspect');
  if (btnResetInspect) {
    btnResetInspect.addEventListener('click', resetInspectedNode);
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
