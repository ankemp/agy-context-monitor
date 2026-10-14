// Completely disable animations globally to prevent spamming animations on live SSE updates
    if (typeof Chart !== 'undefined') {
      Chart.defaults.animation = false;
      Chart.defaults.animations = false;
      Chart.defaults.transitions = {
        active: { animation: { duration: 0 } },
        resize: { animation: { duration: 0 } }
      };
    }

    let timelineChart = null;
    let modelsChart = null;
    let projectCostChart = null;
    let toolsChart = null;
    let categoryChart = null;
    let phaseChart = null;
    let mcpServerChart = null;
    let activeHeatmapHover = null;

    function trackCanvasPointer(canvas) {
      if (!canvas || canvas._hasPointerTracker) return;
      canvas._hasPointerTracker = true;
      canvas.addEventListener('pointermove', (e) => {
        canvas._lastPointer = {
          clientX: e.clientX,
          clientY: e.clientY
        };
      });
      canvas.addEventListener('pointerleave', () => {
        canvas._lastPointer = null;
      });
    }

    function restoreChartTooltip(chart) {
      const canvas = chart?.canvas;
      if (!chart || !canvas || !canvas._lastPointer) return;
      const pos = canvas._lastPointer;

      // 1. Dispatch synthetic mousemove so Chart.js native event handlers recalculate hover
      canvas.dispatchEvent(new MouseEvent('mousemove', {
        clientX: pos.clientX,
        clientY: pos.clientY,
        bubbles: true
      }));

      // 2. Programmatically ensure active elements & tooltip plugin are synced
      try {
        const rect = canvas.getBoundingClientRect();
        const relativePos = {
          x: pos.clientX - rect.left,
          y: pos.clientY - rect.top
        };
        const mode = chart.options?.interaction?.mode || 'nearest';
        const intersect = chart.options?.interaction?.intersect ?? true;
        const elements = chart.getElementsAtEventForMode(relativePos, mode, { intersect }, false);
        if (elements && elements.length > 0) {
          chart.setActiveElements(elements);
          if (chart.tooltip) {
            chart.tooltip.setActiveElements(elements, relativePos);
          }
          chart.render();
        }
      } catch (e) {
        // Safe fallback for transient geometry calculation during redraws
      }
    }

    function upsertChart(existingChart, canvas, config) {
      if (!canvas) return null;
      trackCanvasPointer(canvas);

      if (existingChart && existingChart.config.type === config.type) {
        existingChart.data.labels = config.data.labels;
        existingChart.data.datasets = config.data.datasets;
        if (config.options) {
          existingChart.options = config.options;
        }
        existingChart.update('none');
        restoreChartTooltip(existingChart);
        return existingChart;
      }

      if (existingChart) {
        try { existingChart.destroy(); } catch (e) {}
      }
      const newChart = new Chart(canvas.getContext('2d'), config);
      restoreChartTooltip(newChart);
      return newChart;
    }

    let allHotFiles = [];
    let allConversations = [];
    let allTimelineData = [];
    let allProjectCostData = null;
    let allToolsData = [];
    let allMcpData = [];
    let currentTimelineDays = 30;
    let currentWeeklyQuotaCapacity = 672.0;

    function updateWeeklyQuotaCapacity(quota) {
      if (!quota) return;
      const buckets = quota.buckets || quota;
      const geminiWeeklyBucket = buckets['gemini-weekly'] || quota['gemini-weekly'] || quota.gemini_weekly;
      const weeklyCalibratedCapacity = geminiWeeklyBucket?.calibrated_capacity || quota.calibrated_weekly_capacity;
      if (Number(weeklyCalibratedCapacity) > 0) {
        const newCap = Number(weeklyCalibratedCapacity);
        if (newCap !== currentWeeklyQuotaCapacity) {
          currentWeeklyQuotaCapacity = newCap;
          if (typeof timelineChart !== 'undefined' && timelineChart) {
            timelineChart.update('none');
          }
        }
      }
    }
    if (typeof STATIC_DATA !== 'undefined' && STATIC_DATA && STATIC_DATA.quota) {
      updateWeeklyQuotaCapacity(STATIC_DATA.quota);
    }
    let currentToolFilter = 'all';
    let currentHotFilesFilter = 'files';
    let cachedActivityMatrix = null;
    let currentHeatmapMode = 'year';
    let localTimezoneAbbr = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
    function getProjectFromUrl() {
      try {
        const params = new URLSearchParams(window.location.search);
        const p = params.get('project');
        if (p && p.trim() && p.trim().toLowerCase() !== 'all') {
          return p.trim();
        }
      } catch (e) {
        console.error('Failed to read project from URL', e);
      }
      return 'all';
    }

    function syncProjectToUrl(val) {
      try {
        const url = new URL(window.location.href);
        if (val && val !== 'all') {
          url.searchParams.set('project', val);
        } else {
          url.searchParams.delete('project');
        }
        const newUrl = url.pathname + (url.search ? url.search : '') + url.hash;
        if ((window.location.pathname + window.location.search + window.location.hash) !== newUrl) {
          window.history.pushState({ project: val }, '', newUrl);
        }
      } catch (e) {
        console.error('Failed to sync project to URL', e);
      }
    }

    function updateProjectFilterUI(val) {
      const filterSelect = document.getElementById('projectFilter');
      if (filterSelect) {
        if (val && val !== 'all') {
          let opt = Array.from(filterSelect.options).find(o => o.value === val);
          if (!opt) {
            opt = document.createElement('option');
            opt.value = val;
            opt.textContent = val;
            filterSelect.appendChild(opt);
          }
          filterSelect.value = val;
          filterSelect.classList.remove('hidden');
        } else {
          filterSelect.value = 'all';
        }
      }
      const modeGroup = document.getElementById('timelineModeGroup');
      const divider = document.getElementById('timelineDivider');
      if (modeGroup) {
        if (val !== 'all') {
          modeGroup.classList.add('hidden');
          if (divider) divider.classList.add('hidden');
        } else {
          modeGroup.classList.remove('hidden');
          if (divider) divider.classList.remove('hidden');
        }
      }
    }

    let currentProject = getProjectFromUrl();
    let currentTimelineViewMode = 'stacked'; // 'stacked' | 'aggregate'

    window.setTimelineViewMode = function(mode) {
      currentTimelineViewMode = mode;
      const btnStacked = document.getElementById('btnTmStacked');
      const btnAggregate = document.getElementById('btnTmAggregate');
      const btnClasses = {
        active: 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow-sm transition text-[11px] whitespace-nowrap outline-none focus:outline-none focus:ring-0',
        inactive: 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition text-[11px] whitespace-nowrap outline-none focus:outline-none focus:ring-0'
      };
      if (btnStacked) btnStacked.className = mode === 'stacked' ? btnClasses.active : btnClasses.inactive;
      if (btnAggregate) btnAggregate.className = mode === 'aggregate' ? btnClasses.active : btnClasses.inactive;
      setTimelineRange(currentTimelineDays);
    };

    let currentConvViewMode = 'hierarchy'; // 'hierarchy' | 'flat'
    let expandedConvs = new Set();

    window.setConvViewMode = async function(mode) {
      currentConvViewMode = mode;
      const btnHierarchy = document.getElementById('btnConvHierarchy');
      const btnFlat = document.getElementById('btnConvFlat');
      const activeCls = 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition text-[11px] whitespace-nowrap inline-flex items-center space-x-1.5 shrink-0';
      const inactiveCls = 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition text-[11px] whitespace-nowrap inline-flex items-center space-x-1.5 shrink-0';
      if (btnHierarchy) btnHierarchy.className = mode === 'hierarchy' ? activeCls : inactiveCls;
      if (btnFlat) btnFlat.className = mode === 'flat' ? activeCls : inactiveCls;

      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        let params = [`view=${encodeURIComponent(mode)}`];
        if (clientTz) params.push(`tz=${encodeURIComponent(clientTz)}`);
        if (currentProject !== 'all') params.push(`project=${encodeURIComponent(currentProject)}`);
        const convs = await fetch(`/api/conversations?${params.join('&')}`).then(r => r.json());
        if (convs) {
          renderConversations(convs);
        }
      } catch (err) {
        console.error('Failed to switch conversation view mode', err);
      }
    };

    window.toggleSubagents = function(convId) {
      if (expandedConvs.has(convId)) {
        expandedConvs.delete(convId);
      } else {
        expandedConvs.add(convId);
      }
      filterConversations();
    };

    window.handleProjectChange = function(val, updateUrl = true) {
      currentProject = val || 'all';
      updateProjectFilterUI(currentProject);
      if (updateUrl) {
        syncProjectToUrl(currentProject);
      }
      fetchDashboardData();
    };

    window.addEventListener('popstate', () => {
      const projFromUrl = getProjectFromUrl();
      if (projFromUrl !== currentProject) {
        window.handleProjectChange(projFromUrl, false);
      }
    });

    let currentProjectMetric = 'cost'; // 'cost' | 'tokens'

    window.setProjectCostMetric = function(metric) {
      currentProjectMetric = metric;
      const btnCost = document.getElementById('projectMetricCost');
      const btnTokens = document.getElementById('projectMetricTokens');
      const activeCls = 'px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition text-[11px]';
      const inactiveCls = 'px-2.5 py-1 rounded text-gray-400 hover:text-white transition text-[11px]';
      if (btnCost) btnCost.className = metric === 'cost' ? activeCls : inactiveCls;
      if (btnTokens) btnTokens.className = metric === 'tokens' ? activeCls : inactiveCls;
      if (allProjectCostData) {
        renderProjectCostSection(allProjectCostData, currentProject);
      }
    };

    const PROJECT_PALETTE = [
      '#6366f1', '#a855f7', '#06b6d4', '#10b981', '#f59e0b',
      '#ec4899', '#3b82f6', '#14b8a6', '#f43f5e', '#8b5cf6'
    ];

    function getProjectColor(projectName, index) {
      if (!projectName || projectName === '(unassigned)') {
        return '#64748b';
      }
      return PROJECT_PALETTE[index % PROJECT_PALETTE.length];
    }

    const COLOR_PALETTE = {
      lvl0: '#151d2f',
      lvl1: '#312e81',
      lvl2: '#4338ca',
      lvl3: '#6366f1',
      lvl4: '#a5b4fc'
    };

    function formatTokens(n) {
      if (!n) return "0";
      if (n >= 1000000000) return (n / 1000000000).toFixed(2) + "B";
      if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
      if (n >= 1000) return (n / 1000).toFixed(1) + "k";
      return n.toLocaleString();
    }

    function formatDuration(sec) {
      if (!sec) return "0m";
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    }

    function shortenModelName(name) {
      if (!name) return 'Gemini';
      let clean = String(name).trim();
      if (clean.toLowerCase().startsWith('gemini ')) {
        clean = clean.substring(7).trim();
      }
      clean = clean.replace(/\(Medium\)/i, '(Med)');
      return clean;
    }

    function getModelTierClass(modelStr) {
      const m = String(modelStr || '').toLowerCase();
      if (m.includes('argon') || m.includes('reasoning') || m.includes('thinking')) {
        return 'bg-cyan-950/70 text-cyan-300 border-cyan-500/40';
      }
      if (m.includes('pro') || m.includes('opus')) {
        return 'bg-indigo-950/70 text-indigo-300 border-indigo-500/40';
      }
      if (m.includes('flash') || m.includes('haiku') || (m.includes('mini') && !m.includes('gemini'))) {
        return 'bg-purple-950/70 text-purple-300 border-purple-500/40';
      }
      if (m.includes('claude') || m.includes('sonnet') || m.includes('gpt')) {
        return 'bg-amber-950/70 text-amber-300 border-amber-500/40';
      }
      return 'bg-zinc-800/80 text-gray-400 border-zinc-700/50';
    }

    function formatModelBadgeHtml(modelStr) {
      if (!modelStr) {
        return `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium bg-zinc-800/80 text-gray-400 border border-zinc-700/50 shrink-0" title="Unknown">Gemini</span>`;
      }

      const str = String(modelStr).trim();
      const parts = str.split(/\s*(?:→|➔|->)\s*/).filter(Boolean);

      if (parts.length === 0) {
        return `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium bg-zinc-800/80 text-gray-400 border border-zinc-700/50 shrink-0" title="${str}">${str}</span>`;
      }

      const badgesHtml = parts.map(part => {
        const short = shortenModelName(part);
        const tierClass = getModelTierClass(part);
        return `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium ${tierClass} border shrink-0" title="${part}">${short}</span>`;
      });

      if (parts.length === 1) {
        return `<div class="flex flex-wrap items-center gap-1 max-w-[220px]">${badgesHtml[0]}</div>`;
      }

      const joinedHtml = badgesHtml.join('<span class="text-gray-500 text-[10px] font-mono select-none">→</span>');
      return `<div class="flex flex-wrap items-center gap-1 max-w-[220px]">${joinedHtml}</div>`;
    }

    async function fetchDashboardData() {
      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        let baseQuery = [];
        if (clientTz) baseQuery.push(`tz=${encodeURIComponent(clientTz)}`);
        if (currentProject !== 'all') baseQuery.push(`project=${encodeURIComponent(currentProject)}`);
        const queryStr = baseQuery.length > 0 ? `?${baseQuery.join('&')}` : '';

        let convQuery = [...baseQuery, `view=${encodeURIComponent(currentConvViewMode)}`];
        const convQueryStr = convQuery.length > 0 ? `?${convQuery.join('&')}` : '';

        const [summaryRes, timelineRes, costProjRes, toolsRes, mcpRes, hotFilesRes, matrixRes, convsRes] = await Promise.all([
          fetch(`/api/summary${queryStr}`).then(r => r.json()),
          fetch(`/api/timeline${queryStr}`).then(r => r.json()),
          fetch(`/api/cost-by-project${queryStr}`).then(r => r.json()),
          fetch(`/api/tools${queryStr}`).then(r => r.json()),
          fetch(`/api/mcp${queryStr}`).then(r => r.json()),
          fetch(`/api/hot-files${queryStr}`).then(r => r.json()),
          fetch(`/api/activity-matrix${queryStr}`).then(r => r.json()),
          fetch(`/api/conversations${convQueryStr}`).then(r => r.json())
        ]);

        allTimelineData = timelineRes || [];
        allProjectCostData = costProjRes || { total_cost: 0, projects: [] };
        allToolsData = toolsRes || [];
        allMcpData = mcpRes || [];
        cachedActivityMatrix = matrixRes;

        if (summaryRes.timezone_label) {
          document.getElementById('tzLabel').textContent = summaryRes.timezone_label;
        } else if (matrixRes.timezone_abbr) {
          document.getElementById('tzLabel').textContent = matrixRes.timezone_abbr;
        }
        if (matrixRes.timezone_abbr) {
          localTimezoneAbbr = matrixRes.timezone_abbr;
        }

        renderKPIs(summaryRes);
        if (summaryRes.quota || summaryRes.quota_summary) {
          renderQuotaSection(summaryRes.quota || summaryRes.quota_summary);
        }
        setTimelineRange(currentTimelineDays);
        renderModelsChart(summaryRes.models);
        renderProjectCostSection(allProjectCostData, currentProject);
        renderToolsChart(allToolsData);
        renderMcpSection(allMcpData);
        renderCategoryChart(summaryRes.categories);
        renderPhaseChart(summaryRes.phases);
        renderHeatmapAllViews(matrixRes);
        renderHotFiles(hotFilesRes);
        if (convsRes && convsRes.length > 0) {
          renderConversations(convsRes);
        }
        
        lucide.createIcons();
      } catch (err) {
        console.error("Failed to load dashboard data", err);
      }
    }

    function renderQuotaSection(quota) {
      if (!quota) return;
      updateWeeklyQuotaCapacity(quota);
      const buckets = quota.buckets || quota;
      const gw = buckets['gemini-weekly'] || quota['gemini-weekly'] || quota.gemini_weekly;
      const g5 = buckets['gemini-5h'] || quota['gemini-5h'] || quota.gemini_5h;
      const pw = buckets['3p-weekly'] || quota['3p-weekly'] || quota.threep_weekly;
      const p5 = buckets['3p-5h'] || quota['3p-5h'] || quota.threep_5h;

      if (gw) {
        const rem = gw.quota_remaining_pct != null ? gw.quota_remaining_pct : (gw.remaining_pct != null ? gw.remaining_pct : (gw.remaining_fraction != null ? gw.remaining_fraction * 100 : 100));
        const cap = gw.calibrated_capacity || quota.calibrated_weekly_capacity || 672.0;
        const cd = gw.reset_countdown || quota.weekly_reset_countdown || '';
        const elRem = document.getElementById('quotaGeminiWeeklyRem');
        const elBar = document.getElementById('quotaGeminiWeeklyBar');
        const elReset = document.getElementById('quotaGeminiWeeklyReset');
        const elCap = document.getElementById('quotaGeminiWeeklyCap');
        if (elRem) elRem.textContent = `${Number(rem).toFixed(1)}% rem`;
        if (elBar) elBar.style.width = `${Math.min(Math.max(0, 100 - rem), 100)}%`;
        if (elReset) elReset.textContent = cd ? `Reset: ${cd}` : 'Reset: --';
        if (elCap) elCap.textContent = `Cap: $${Number(cap).toFixed(2)}`;
      }

      if (g5) {
        const rem = g5.quota_remaining_pct != null ? g5.quota_remaining_pct : (g5.remaining_pct != null ? g5.remaining_pct : (g5.remaining_fraction != null ? g5.remaining_fraction * 100 : 100));
        const cap = g5.calibrated_capacity || quota.calibrated_5h_capacity || 236.7;
        const cd = g5.reset_countdown || quota.five_hour_reset_countdown || '';
        const elRem = document.getElementById('quotaGemini5hRem');
        const elBar = document.getElementById('quotaGemini5hBar');
        const elReset = document.getElementById('quotaGemini5hReset');
        const elCap = document.getElementById('quotaGemini5hCap');
        if (elRem) elRem.textContent = `${Number(rem).toFixed(1)}% rem`;
        if (elBar) elBar.style.width = `${Math.min(Math.max(0, 100 - rem), 100)}%`;
        if (elReset) elReset.textContent = cd ? `Reset: ${cd}` : 'Reset: --';
        if (elCap) elCap.textContent = `Cap: $${Number(cap).toFixed(2)}`;
      }

      if (pw) {
        const rem = pw.quota_remaining_pct != null ? pw.quota_remaining_pct : (pw.remaining_pct != null ? pw.remaining_pct : (pw.remaining_fraction != null ? pw.remaining_fraction * 100 : 100));
        const cap = pw.calibrated_capacity || 200.0;
        const cd = pw.reset_countdown || '';
        const elRem = document.getElementById('quota3pWeeklyRem');
        const elBar = document.getElementById('quota3pWeeklyBar');
        const elReset = document.getElementById('quota3pWeeklyReset');
        const elCap = document.getElementById('quota3pWeeklyCap');
        if (elRem) elRem.textContent = `${Number(rem).toFixed(1)}% rem`;
        if (elBar) elBar.style.width = `${Math.min(Math.max(0, 100 - rem), 100)}%`;
        if (elReset) elReset.textContent = cd ? `Reset: ${cd}` : 'Reset: --';
        if (elCap) elCap.textContent = `Cap: $${Number(cap).toFixed(2)}`;
      }

      if (p5) {
        const rem = p5.quota_remaining_pct != null ? p5.quota_remaining_pct : (p5.remaining_pct != null ? p5.remaining_pct : (p5.remaining_fraction != null ? p5.remaining_fraction * 100 : 100));
        const cap = p5.calibrated_capacity || 70.0;
        const cd = p5.reset_countdown || '';
        const elRem = document.getElementById('quota3p5hRem');
        const elBar = document.getElementById('quota3p5hBar');
        const elReset = document.getElementById('quota3p5hReset');
        const elCap = document.getElementById('quota3p5hCap');
        if (elRem) elRem.textContent = `${Number(rem).toFixed(1)}% rem`;
        if (elBar) elBar.style.width = `${Math.min(Math.max(0, 100 - rem), 100)}%`;
        if (elReset) elReset.textContent = cd ? `Reset: ${cd}` : 'Reset: --';
        if (elCap) elCap.textContent = `Cap: $${Number(cap).toFixed(2)}`;
      }

      const sessWk = quota.session_weekly_pct != null ? quota.session_weekly_pct : (quota.weekly_pct || 0);
      const sess5h = quota.session_5h_pct != null ? quota.session_5h_pct : (quota.five_hour_pct || 0);
      const elSessWk = document.getElementById('sessionQuotaImpactWk');
      const elSess5h = document.getElementById('sessionQuotaImpact5h');
      if (elSessWk) elSessWk.textContent = `${Number(sessWk).toFixed(2)}% weekly`;
      if (elSess5h) elSess5h.textContent = `${Number(sess5h).toFixed(2)}% 5h`;
    }

    function renderKPIs(data) {
      document.getElementById('kpiTokens').textContent = formatTokens(data.total_tokens);
      document.getElementById('kpiChars').textContent = (data.total_chars || 0).toLocaleString() + ' chars';
      document.getElementById('kpiConvs').textContent = (data.total_convs || 0).toLocaleString();
      document.getElementById('kpiSteps').textContent = (data.total_steps || 0).toLocaleString();
      document.getElementById('kpiAvgSteps').textContent = data.avg_steps + ' avg/session';
      document.getElementById('kpiTools').textContent = (data.total_tools || 0).toLocaleString();
      
      const agentWork = data.total_agent_work || 0;
      const pairingWork = data.total_pairing_work || 0;
      const wallSpan = data.total_duration || 0;
      
      document.getElementById('kpiDuration').textContent = formatDuration(agentWork);
      document.getElementById('kpiPairingSub').textContent = `~${formatDuration(pairingWork)} pairing • ${formatDuration(wallSpan)} span`;
      
      const cardEl = document.getElementById('cardActiveTime');
      if (cardEl) {
        cardEl.title = `Pure Agent Execution Time: ${formatDuration(agentWork)} (active LLM reasoning, code generation, tool runs).\\nActive Pairing Time: ${formatDuration(pairingWork)} (continuous interactions <= 5m gaps).\\nTotal Wall-Clock Span: ${formatDuration(wallSpan)} (across all open sessions).`;
      }
      
      document.getElementById('kpiCost').textContent = '$' + (data.total_billed_cost || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
      document.getElementById('kpiBilledTokens').textContent = `~${formatTokens(data.total_billed_input_tokens)} in / ${formatTokens(data.total_billed_output_tokens)} out`;

      // 1b. Developer Velocity Strip
      const elAutonomy = document.getElementById('kpiAutonomy');
      if (elAutonomy) elAutonomy.textContent = `${data.autonomy_multiplier || 1}x`;
      const elAutonomySub = document.getElementById('kpiAutonomySub');
      if (elAutonomySub) elAutonomySub.textContent = `${(data.total_tools || 0).toLocaleString()} ops / ${(data.total_prompts || 0).toLocaleString()} prompts`;
      const cardAutonomy = document.getElementById('cardAutonomy');
      if (cardAutonomy) {
        const tools = (data.total_tools || 0).toLocaleString();
        const prompts = (data.total_prompts || 0).toLocaleString();
        const multiplier = data.autonomy_multiplier || 1;
        cardAutonomy.title = `Autonomous leverage ratio: ${multiplier}x\nAverage of ${tools} automated operations executed across ${prompts} human prompts.\nHigher ratios represent deeper autonomous execution per instruction.`;
      }

      const elThinking = document.getElementById('kpiThinking');
      if (elThinking) elThinking.textContent = formatTokens(data.total_thinking || 0);
      const cardThinking = document.getElementById('cardThinking');
      if (cardThinking) {
        const thinkingTokens = (data.total_thinking || 0).toLocaleString();
        cardThinking.title = `Internal chain-of-thought volume: ${thinkingTokens} thinking tokens.\nGenerated during deep reasoning and deliberative planning phases before producing visible responses.`;
      }

      const elCodeLines = document.getElementById('kpiCodeLines');
      if (elCodeLines) elCodeLines.textContent = (data.total_code_lines || 0).toLocaleString();
      const elCodeSub = document.getElementById('kpiCodeSub');
      if (elCodeSub) elCodeSub.textContent = `+${(data.lines_written || 0).toLocaleString()} / ~${(data.lines_edited || 0).toLocaleString()}`;
      const cardCodeLines = document.getElementById('cardCodeLines');
      if (cardCodeLines) {
        const total = (data.total_code_lines || 0).toLocaleString();
        const written = (data.lines_written || 0).toLocaleString();
        const edited = (data.lines_edited || 0).toLocaleString();
        cardCodeLines.title = `Total code authored: ${total} lines\n• +${written} new lines written to created files (write_to_file)\n• ~${edited} modified lines replaced in existing files (replace_file_content)`;
      }

      const elTimeSaved = document.getElementById('kpiTimeSaved');
      if (elTimeSaved) elTimeSaved.textContent = `~${(data.saved_hours || 0).toLocaleString()}h`;
      const cardTimeSaved = document.getElementById('cardTimeSaved');
      if (cardTimeSaved) {
        if (data.saved_reads != null) {
          const reads = (data.saved_reads || 0).toLocaleString();
          const cmds = (data.saved_cmds || 0).toLocaleString();
          const edits = (data.saved_edits || 0).toLocaleString();
          const hours = (data.saved_hours || 0).toLocaleString();
          cardTimeSaved.title = `Calculated from automated developer actions (~${hours}h saved):\n• ${reads} file reads/searches (30s each)\n• ${cmds} command executions (45s each)\n• ${edits} code edits/writes (120s each)`;
        } else {
          cardTimeSaved.title = `Calculated from automated developer actions: 30s per file read/search (view_file, grep_search, find_by_name, list_dir), 45s per command execution (run_command), and 120s per code write/edit (write_to_file, replace_file_content).`;
        }
      }

      // Context Hygiene Diagnostics
      const elReReadRate = document.getElementById('diagReReadRate');
      if (elReReadRate) elReReadRate.textContent = `${data.re_read_rate || 0}%`;
      const elReReadBar = document.getElementById('diagReReadBar');
      if (elReReadBar) elReReadBar.style.width = `${Math.min(data.re_read_rate || 0, 100)}%`;
      const elReReadCount = document.getElementById('diagReReadCount');
      if (elReReadCount) elReReadCount.textContent = `${(data.total_re_reads || 0).toLocaleString()} repeated views`;
      const elReReadWaste = document.getElementById('diagReReadWaste');
      if (elReReadWaste) elReReadWaste.textContent = `~${formatTokens(data.re_read_waste_tokens || 0)} waste`;
      const cardDiagReRead = document.getElementById('cardDiagReRead');
      if (cardDiagReRead) {
        const rate = data.re_read_rate || 0;
        const count = (data.total_re_reads || 0).toLocaleString();
        const waste = formatTokens(data.re_read_waste_tokens || 0);
        cardDiagReRead.title = `Context cache health: ${rate}% re-read rate\n${count} repeated views of previously inspected files in the same session.\nEstimated context waste: ~${waste} tokens (~750 tok/re-read).`;
      }

      const elResilienceRate = document.getElementById('diagResilienceRate');
      if (elResilienceRate) elResilienceRate.textContent = `${data.resilience_rate || 100}%`;
      const elResilienceBar = document.getElementById('diagResilienceBar');
      if (elResilienceBar) elResilienceBar.style.width = `${Math.min(data.resilience_rate || 100, 100)}%`;
      const elErrorCount = document.getElementById('diagErrorCount');
      if (elErrorCount) elErrorCount.textContent = `${(data.failed_tool_calls || 0).toLocaleString()} errors resolved`;
      const cardDiagResilience = document.getElementById('cardDiagResilience');
      if (cardDiagResilience) {
        const rate = data.resilience_rate || 100;
        const fails = (data.failed_tool_calls || 0).toLocaleString();
        const tools = (data.total_tools || 0).toLocaleString();
        cardDiagResilience.title = `Tool execution resilience: ${rate}% first-attempt success\n${fails} errors recovered across ${tools} total tool calls.\nMeasures autonomous self-correction without human intervention.`;
      }

      // Bloat Table
      const bloatTbody = document.getElementById('bloatTableBody');
      if (bloatTbody && data.bloat_steps) {
        bloatTbody.innerHTML = '';
        const phaseBadges = {
          'discovery': 'bg-amber-500/10 text-amber-400 border-amber-500/20',
          'reasoning': 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
          'execution': 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
          'verification': 'bg-purple-500/10 text-purple-400 border-purple-500/20'
        };
        for (const b of data.bloat_steps) {
          const tr = document.createElement('tr');
          tr.className = 'hover:bg-dark-800/60 transition';
          const p = (b.phase || 'other').toLowerCase();
          const badgeCls = phaseBadges[p] || 'bg-gray-500/10 text-gray-400 border-gray-500/20';
          tr.innerHTML = `
            <td class="py-1.5 px-2 font-mono text-[10px] text-gray-400">#${b.step_index}</td>
            <td class="py-1.5 px-2"><span class="px-1.5 py-0.5 rounded text-[9px] uppercase font-semibold border ${badgeCls}">${b.phase}</span></td>
            <td class="py-1.5 px-2 text-right font-mono font-bold text-rose-400 text-[11px]">${formatTokens(b.est_tokens)}</td>
            <td class="py-1.5 px-2 text-[10px] text-gray-300 max-w-[120px] truncate" title="${b.detail || ''}">${b.detail || '--'}</td>
          `;
          bloatTbody.appendChild(tr);
        }
      }
    }

    function getCommitColor(steps) {
      if (!steps || steps <= 0) return COLOR_PALETTE.lvl0;
      if (steps <= 50) return COLOR_PALETTE.lvl1;
      if (steps <= 250) return COLOR_PALETTE.lvl2;
      if (steps <= 1000) return COLOR_PALETTE.lvl3;
      return COLOR_PALETTE.lvl4;
    }

    function renderHeatmapAllViews(data) {
      if (!data) return;
      const daily = data.daily || {};
      const streak = data.streak || {};

      document.getElementById('streakCurrent').textContent = `${streak.current} days`;
      document.getElementById('streakLongest').textContent = `${streak.longest} days`;
      document.getElementById('activeDaysCount').textContent = `${streak.total_active_days} days`;

      let totalOps = 0;
      for (const k in daily) totalOps += daily[k].steps || 0;
      document.getElementById('heatmapOpsCount').textContent = `${totalOps.toLocaleString()} operations in 2026`;

      // View 1: 52-Week Year Matrix
      renderYearGrid(daily);
      // View 2: 3 Months (14 Weeks) Matrix
      renderQuarterGrid(daily);
      // View 3: Past 30 Days (Hourly Matrix: 24 cols x 30 rows)
      renderMonthHourlyGrid(data.past_30_days_hourly || {});
      // View 4: Past 7 Days (Hourly 24x7 Commit Squares)
      renderPast7HourlyGrid(data.past_7_days_hourly || {});
      // View 5: 24x7 Aggregate (Hourly 24x7 Commit Squares)
      renderHourlyAggregateGrid(data.hourly_aggregate || []);

      if (activeHeatmapHover) {
        if (activeHeatmapHover.type === 'daily') {
          const dayData = daily[activeHeatmapHover.dateStr] || { steps: 0, tokens: 0, sessions: 0 };
          const tooltipMsg = dayData.steps > 0 
            ? `${dayData.steps.toLocaleString()} operations (${formatTokens(dayData.tokens)} tokens across ${dayData.sessions} sessions) on ${activeHeatmapHover.formattedDate}`
            : `No operations on ${activeHeatmapHover.formattedDate}`;
          document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
        } else if (activeHeatmapHover.type === 'month_hourly') {
          const monthHourly = data.past_30_days_hourly || {};
          const hours = monthHourly[activeHeatmapHover.dateStr] || [];
          const val = hours[activeHeatmapHover.hour] || 0;
          const tooltipMsg = `${activeHeatmapHover.dateStr} @ ${activeHeatmapHover.timeFormatted} — ${val.toLocaleString()} operations`;
          document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
        } else if (activeHeatmapHover.type === 'past7_hourly') {
          const past7 = data.past_7_days_hourly || {};
          const hours = past7[activeHeatmapHover.dateStr] || [];
          const val = hours[activeHeatmapHover.hour] || 0;
          const tooltipMsg = `${activeHeatmapHover.dateStr} @ ${activeHeatmapHover.timeFormatted} — ${val.toLocaleString()} operations`;
          document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
        } else if (activeHeatmapHover.type === 'hourly_agg') {
          const agg = data.hourly_aggregate || [];
          const row = agg[activeHeatmapHover.dayIdx] || [];
          const val = row[activeHeatmapHover.hour] || 0;
          const tooltipMsg = `${activeHeatmapHover.dayName} at ${activeHeatmapHover.timeFormatted} (${localTimezoneAbbr}) — ${val.toLocaleString()} operations across all history`;
          document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
        }
      }
    }

    // --- Mode 1: 52-Week Grid ---
    function renderYearGrid(daily) {
      const gridContainer = document.getElementById('ghGrid');
      const monthLabelsContainer = document.getElementById('ghMonthLabels');
      gridContainer.innerHTML = '';
      monthLabelsContainer.innerHTML = '';

      const today = new Date();
      const dayOfWeek = today.getDay();
      const totalDays = 52 * 7 + dayOfWeek + 1;
      
      const startDate = new Date(today);
      startDate.setDate(today.getDate() - totalDays + 1);

      let currentDate = new Date(startDate);
      let monthCols = {};
      let colIdx = 0;

      while (currentDate <= today) {
        const col = document.createElement('div');
        col.className = 'flex flex-col space-y-[3px]';

        for (let row = 0; row < 7; row++) {
          if (currentDate > today) {
            const emptyCell = document.createElement('div');
            emptyCell.className = 'commit-square-sm opacity-0 pointer-events-none';
            col.appendChild(emptyCell);
            continue;
          }

          const dateStr = currentDate.toISOString().substring(0, 10);
          const monthName = currentDate.toLocaleString('default', { month: 'short' });
          const dayNum = currentDate.getDate();

          if (row === 0 && dayNum <= 7 && !monthCols[monthName]) {
            monthCols[monthName] = colIdx;
          }

          const dayData = daily[dateStr] || { steps: 0, tokens: 0, sessions: 0 };
          const cell = document.createElement('div');
          cell.className = 'commit-square-sm';
          cell.style.backgroundColor = getCommitColor(dayData.steps);

          const formattedDate = currentDate.toLocaleDateString('default', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
          const tooltipMsg = dayData.steps > 0 
            ? `${dayData.steps.toLocaleString()} operations (${formatTokens(dayData.tokens)} tokens across ${dayData.sessions} sessions) on ${formattedDate}`
            : `No operations on ${formattedDate}`;

          cell.addEventListener('mouseenter', () => {
            activeHeatmapHover = { type: 'daily', dateStr, formattedDate };
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            activeHeatmapHover = null;
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          col.appendChild(cell);
          currentDate.setDate(currentDate.getDate() + 1);
        }

        gridContainer.appendChild(col);
        colIdx++;
      }

      for (let c = 0; c < colIdx; c++) {
        const span = document.createElement('span');
        span.style.width = '14px';
        span.style.display = 'inline-block';
        for (const [mName, mCol] of Object.entries(monthCols)) {
          if (mCol === c) { span.textContent = mName; break; }
        }
        monthLabelsContainer.appendChild(span);
      }
    }

    // --- Mode 2: 3 Months (14 Weeks) Matrix ---
    function renderQuarterGrid(daily) {
      const container = document.getElementById('ghQuarterGrid');
      const labelContainer = document.getElementById('ghQuarterMonthLabels');
      container.innerHTML = '';
      labelContainer.innerHTML = '';

      const today = new Date();
      const totalDays = 14 * 7;
      const startDate = new Date(today);
      startDate.setDate(today.getDate() - totalDays + (6 - today.getDay()));

      let currentDate = new Date(startDate);
      let colIdx = 0;
      let monthCols = {};

      while (currentDate <= today) {
        const col = document.createElement('div');
        col.className = 'flex flex-col space-y-[3px]';

        for (let row = 0; row < 7; row++) {
          if (currentDate > today) {
            const emptyCell = document.createElement('div');
            emptyCell.className = 'commit-square opacity-0 pointer-events-none';
            col.appendChild(emptyCell);
            continue;
          }

          const dateStr = currentDate.toISOString().substring(0, 10);
          const monthName = currentDate.toLocaleString('default', { month: 'short' });
          if (row === 0 && !monthCols[monthName]) {
            monthCols[monthName] = colIdx;
          }

          const dayData = daily[dateStr] || { steps: 0, tokens: 0, sessions: 0 };
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(dayData.steps);

          const formattedDate = currentDate.toLocaleDateString('default', { weekday: 'short', month: 'short', day: 'numeric' });
          const tooltipMsg = `${dayData.steps.toLocaleString()} ops (${formatTokens(dayData.tokens)} tokens) on ${formattedDate}`;

          cell.addEventListener('mouseenter', () => {
            activeHeatmapHover = { type: 'daily', dateStr, formattedDate };
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            activeHeatmapHover = null;
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          col.appendChild(cell);
          currentDate.setDate(currentDate.getDate() + 1);
        }
        container.appendChild(col);
        colIdx++;
      }

      for (let c = 0; c < colIdx; c++) {
        const span = document.createElement('span');
        span.style.width = '16px';
        span.style.display = 'inline-block';
        for (const [mName, mCol] of Object.entries(monthCols)) {
          if (mCol === c) { span.textContent = mName; break; }
        }
        labelContainer.appendChild(span);
      }
    }

    // --- Mode 3: Past 30 Days (Hourly Matrix: 24 cols x 30 rows) ---
    function renderMonthHourlyGrid(data) {
      const headerContainer = document.getElementById('monthHourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghMonthGrid');
      if (!container) return;
      container.innerHTML = '';

      const sortedDates = Object.keys(data).sort().reverse();

      for (const dateStr of sortedDates) {
        const hours = data[dateStr];
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';

        const dObj = new Date(dateStr + 'T00:00:00');
        const label = document.createElement('div');
        label.className = 'w-16 text-[10px] font-medium text-gray-400 whitespace-nowrap text-right pr-2 select-none';
        label.textContent = dObj.toLocaleDateString('default', { month: 'short', day: 'numeric' });
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        hours.forEach((val, h) => {
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${dateStr} @ ${timeFormatted} — ${val.toLocaleString()} operations`;

          cell.addEventListener('mouseenter', () => {
            activeHeatmapHover = { type: 'month_hourly', dateStr, timeFormatted, hour: h };
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            activeHeatmapHover = null;
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          squaresContainer.appendChild(cell);
        });

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    // --- Mode 4: Past 7 Days (Hourly 24x7 Commit Squares) ---
    function renderPast7HourlyGrid(data) {
      const headerContainer = document.getElementById('past7HourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghPast7Grid');
      if (!container) return;
      container.innerHTML = '';

      for (const [dateStr, hours] of Object.entries(data)) {
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';

        const dObj = new Date(dateStr + 'T00:00:00');
        const label = document.createElement('div');
        label.className = 'w-16 text-[10px] font-medium text-gray-400 whitespace-nowrap text-right pr-2 select-none';
        label.textContent = dObj.toLocaleDateString('default', { weekday: 'short', day: 'numeric' });
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        hours.forEach((val, h) => {
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${dateStr} @ ${timeFormatted} — ${val.toLocaleString()} operations`;

          cell.addEventListener('mouseenter', () => {
            activeHeatmapHover = { type: 'past7_hourly', dateStr, timeFormatted, hour: h };
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            activeHeatmapHover = null;
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          squaresContainer.appendChild(cell);
        });

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    // --- Mode 5: 24x7 Aggregate (Hourly 24x7 Commit Squares) ---
    function renderHourlyAggregateGrid(matrix) {
      const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const headerContainer = document.getElementById('hourlyHourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghHourlyGrid');
      if (!container) return;
      container.innerHTML = '';

      for (let d = 0; d < 7; d++) {
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';
        
        const label = document.createElement('div');
        label.className = 'w-10 text-[10px] font-medium text-gray-400 text-right pr-2 select-none';
        label.textContent = days[d];
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        for (let h = 0; h < 24; h++) {
          const val = (matrix[d] && matrix[d][h]) || 0;
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${days[d]} at ${timeFormatted} (${localTimezoneAbbr}) — ${val.toLocaleString()} operations across all history`;

          cell.addEventListener('mouseenter', () => {
            activeHeatmapHover = { type: 'hourly_agg', dayName: days[d], timeFormatted, dayIdx: d, hour: h };
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            activeHeatmapHover = null;
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });

          squaresContainer.appendChild(cell);
        }

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    function setHeatmapMode(mode) {
      currentHeatmapMode = mode;
      document.getElementById('ghYearView').className = mode === 'year' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghQuarterView').className = mode === 'quarter' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghMonthView').className = mode === 'month' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghPast7View').className = mode === 'past7hourly' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghHourlyView').className = mode === 'hourly' ? 'flex flex-col w-fit' : 'hidden';

      const btnClasses = {
        active: 'px-3 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap',
        inactive: 'px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap'
      };

      document.getElementById('hmBtnYear').className = mode === 'year' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnQuarter').className = mode === 'quarter' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnMonth').className = mode === 'month' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnPast7').className = mode === 'past7hourly' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnHourly').className = mode === 'hourly' ? btnClasses.active : btnClasses.inactive;
    }

    // ==========================================
    // CHARTS & TABLES
    // ==========================================
    function setTimelineRange(days) {
      currentTimelineDays = days;
      const btn7d = document.getElementById('btn7d');
      const btn14d = document.getElementById('btn14d');
      const btn30d = document.getElementById('btn30d');
      const btnAll = document.getElementById('btnAll');

      const btnClasses = {
        active: 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow-sm transition text-[11px] whitespace-nowrap outline-none focus:outline-none focus:ring-0',
        inactive: 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition text-[11px] whitespace-nowrap outline-none focus:outline-none focus:ring-0'
      };

      if (btn7d) btn7d.className = days === 7 ? btnClasses.active : btnClasses.inactive;
      if (btn14d) btn14d.className = days === 14 ? btnClasses.active : btnClasses.inactive;
      if (btn30d) btn30d.className = days === 30 ? btnClasses.active : btnClasses.inactive;
      if (btnAll) btnAll.className = days === null ? btnClasses.active : btnClasses.inactive;
      
      let sliced = allTimelineData;
      if (days) {
        sliced = allTimelineData.slice(-days);
      } else {
        const firstActiveIdx = allTimelineData.findIndex(d => (d.steps > 0 || d.tokens > 0 || d.sessions > 0));
        if (firstActiveIdx > 0 && (allTimelineData.length - firstActiveIdx) >= 1) {
          sliced = allTimelineData.slice(firstActiveIdx);
        } else {
          sliced = allTimelineData;
        }
      }
      renderTimelineChart(sliced);
    }
    window.setTimelineRange = setTimelineRange;

    function renderTimelineChart(data) {
      const canvas = document.getElementById('timelineChart');
      if (!canvas) return;
      const labels = data.map(d => d.day);
      const isStacked = (currentTimelineViewMode === 'stacked' && currentProject === 'all');

      let barDatasets = [];

      if (!isStacked) {
        barDatasets = [
          {
            label: currentProject !== 'all' ? `API Tokens (${currentProject})` : 'API Tokens Consumed',
            data: data.map(d => d.tokens),
            backgroundColor: 'rgba(99, 102, 241, 0.65)',
            borderColor: '#6366f1',
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: 'y',
            order: 3
          }
        ];
      } else {
        // Collect all project token sums across the current slice
        const projectSums = {};
        data.forEach(d => {
          if (d.projects) {
            Object.entries(d.projects).forEach(([proj, tok]) => {
              projectSums[proj] = (projectSums[proj] || 0) + (tok || 0);
            });
          }
        });

        const sortedProjects = Object.entries(projectSums)
          .filter(([_, sum]) => sum > 0)
          .sort((a, b) => b[1] - a[1])
          .map(entry => entry[0]);

        const topProjects = sortedProjects.slice(0, 5);
        const otherProjects = new Set(sortedProjects.slice(5));

        const PROJECT_COLORS = ['#6366f1', '#38bdf8', '#a855f7', '#10b981', '#f59e0b'];
        const OTHER_COLOR = '#64748b';

        barDatasets = topProjects.map((proj, idx) => {
          const color = PROJECT_COLORS[idx % PROJECT_COLORS.length];
          return {
            label: proj,
            type: 'bar',
            stack: 'tokens_stack',
            data: data.map(d => (d.projects ? d.projects[proj] || 0 : 0)),
            backgroundColor: color + 'B3', // ~70% opacity
            borderColor: color,
            borderWidth: 1,
            borderRadius: 2,
            yAxisID: 'y',
            order: 3
          };
        });

        if (otherProjects.size > 0) {
          barDatasets.push({
            label: 'Other Projects',
            type: 'bar',
            stack: 'tokens_stack',
            data: data.map(d => {
              if (!d.projects) return 0;
              let sumOther = 0;
              for (const [p, tok] of Object.entries(d.projects)) {
                if (otherProjects.has(p)) {
                  sumOther += (tok || 0);
                }
              }
              return sumOther;
            }),
            backgroundColor: OTHER_COLOR + 'B3',
            borderColor: OTHER_COLOR,
            borderWidth: 1,
            borderRadius: 2,
            yAxisID: 'y',
            order: 3
          });
        }

        if (barDatasets.length === 0) {
          barDatasets.push({
            label: 'API Tokens Consumed',
            data: data.map(d => d.tokens),
            backgroundColor: 'rgba(99, 102, 241, 0.65)',
            borderColor: '#6366f1',
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: 'y',
            order: 3
          });
        }
      }

      const sessions = data.map(d => d.sessions);
      const costs = data.map(d => d.cost || 0);

      const lineDatasets = [
        {
          label: 'Est. API Value ($)',
          data: costs,
          type: 'line',
          borderColor: '#f43f5e',
          backgroundColor: '#f43f5e',
          borderWidth: 2,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          yAxisID: 'yCost',
          order: 1
        },
        {
          label: 'Sessions',
          data: sessions,
          type: 'line',
          borderColor: '#e0e7ff',
          backgroundColor: '#e0e7ff',
          borderWidth: 2,
          borderDash: [4, 4],
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          yAxisID: 'y1',
          order: 2
        }
      ];

      timelineChart = upsertChart(timelineChart, canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [...barDatasets, ...lineDatasets]
        },
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { 
              labels: { 
                color: '#9ca3af', 
                font: { size: 10 },
                usePointStyle: true,
                boxWidth: 8
              } 
            },
            tooltip: {
              filter: function(tooltipItem) {
                if (isStacked && tooltipItem.dataset.stack === 'tokens_stack') {
                  return (tooltipItem.raw || 0) > 0;
                }
                return true;
              },
              callbacks: {
                label: (ctx) => {
                  if (ctx.dataset.label === 'Est. API Value ($)') {
                    const val = Number(ctx.raw || 0);
                    const costFormatted = val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
                    const pct = currentWeeklyQuotaCapacity > 0 ? ((val / currentWeeklyQuotaCapacity) * 100).toFixed(2) : '0.00';
                    return `Est. API Value: $${costFormatted} (${pct}% of weekly quota)`;
                  }
                  if (ctx.dataset.label === 'Sessions') {
                    return `Sessions: ${ctx.raw}`;
                  }
                  if (isStacked && ctx.dataset.stack === 'tokens_stack') {
                    const dayTotal = data[ctx.dataIndex]?.tokens || 0;
                    const dayCost = data[ctx.dataIndex]?.cost || 0;
                    const pct = dayTotal > 0 ? ((ctx.raw / dayTotal) * 100).toFixed(1) : '0.0';
                    const projCost = dayTotal > 0 ? dayCost * (ctx.raw / dayTotal) : 0;
                    const projQuotaPct = currentWeeklyQuotaCapacity > 0 ? ((projCost / currentWeeklyQuotaCapacity) * 100).toFixed(2) : '0.00';
                    return `${ctx.dataset.label}: ${formatTokens(ctx.raw)} (${pct}% · ${projQuotaPct}% wk quota)`;
                  }
                  const dayCost = data[ctx.dataIndex]?.cost || 0;
                  const dayQuotaPct = currentWeeklyQuotaCapacity > 0 ? ((dayCost / currentWeeklyQuotaCapacity) * 100).toFixed(2) : '0.00';
                  return `${ctx.dataset.label || 'API Tokens'}: ${formatTokens(ctx.raw)} (${dayQuotaPct}% of weekly quota)`;
                },
                footer: (items) => {
                  if (!isStacked) return '';
                  const idx = items[0]?.dataIndex;
                  const dayTokens = data[idx]?.tokens || 0;
                  const dayCost = data[idx]?.cost || 0;
                  const dayQuotaPct = currentWeeklyQuotaCapacity > 0 ? ((dayCost / currentWeeklyQuotaCapacity) * 100).toFixed(2) : '0.00';
                  return `Total API Tokens: ${formatTokens(dayTokens)} (${dayQuotaPct}% of weekly quota)`;
                }
              }
            }
          },
          scales: {
            x: { 
              stacked: isStacked,
              grid: { color: '#1f2937' }, 
              ticks: { color: '#9ca3af', maxTicksLimit: 12, font: { size: 10 } } 
            },
            y: {
              type: 'linear',
              position: 'left',
              stacked: isStacked,
              beginAtZero: true,
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', callback: v => formatTokens(v), font: { size: 10 } },
              title: { display: true, text: 'API Tokens', color: '#6b7280', font: { size: 10 } }
            },
            y1: {
              type: 'linear',
              position: 'right',
              stacked: false,
              beginAtZero: true,
              grid: { drawOnChartArea: false },
              ticks: { color: '#e0e7ff', font: { size: 10 }, precision: 0 },
              title: { display: true, text: 'Sessions', color: '#e0e7ff', font: { size: 10 } }
            },
            yCost: {
              type: 'linear',
              position: 'right',
              stacked: false,
              beginAtZero: true,
              grid: { drawOnChartArea: false },
              ticks: {
                color: '#f43f5e',
                callback: v => {
                  let costStr;
                  if (v >= 1000) {
                    costStr = '$' + (v / 1000).toFixed(1) + 'k';
                  } else if (Math.abs(v - Math.round(v)) < 1e-4) {
                    costStr = '$' + Math.round(v);
                  } else {
                    costStr = '$' + v.toFixed(2);
                  }
                  const pct = currentWeeklyQuotaCapacity > 0 ? ((v / currentWeeklyQuotaCapacity) * 100).toFixed(1) : '0.0';
                  return `${costStr} (${pct}%)`;
                },
                font: { size: 10 }
              },
              title: { display: true, text: 'Est. API Value ($ / % wk)', color: '#f43f5e', font: { size: 10 } }
            }
          }
        }
      });
    }

    function renderModelsChart(models) {
      const canvas = document.getElementById('modelsChart');
      if (!canvas || !models) return;
      const labels = models.map(m => m.model_name);
      const data = models.map(m => m.tokens);
      const colors = ['#6366f1', '#a855f7', '#ec4899', '#3b82f6', '#10b981', '#f59e0b'];

      modelsChart = upsertChart(modelsChart, canvas, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.slice(0, labels.length),
            borderColor: '#111827',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { color: '#9ca3af', boxWidth: 12, font: { size: 10 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} API tokens`
              }
            }
          },
          cutout: '65%'
        }
      });
    }

    function renderProjectCostSection(costData, currentProject) {
      if (!costData) return;
      const projects = costData.projects || [];
      const totalCost = costData.total_cost || 0;

      // Header Subtitle
      const costSub = document.getElementById('costProjectSub');
      const isFiltered = Boolean(currentProject && currentProject !== 'all');

      if (costSub) {
        if (isFiltered) {
          costSub.textContent = `Filtered to ${currentProject} • Non-selected projects de-emphasized in grayscale`;
        } else {
          costSub.textContent = 'Cumulative API cost and resource attribution across workspaces';
        }
      }

      // Total Spend / Tokens KPI Stat Callout
      const totalTokens = projects.reduce((acc, p) => acc + (p.tokens || 0), 0);
      const selProj = projects.find(p => p.project === currentProject);
      const headerValEl = document.getElementById('projectCostHeaderValue');
      const headerLabelEl = document.getElementById('projectCostHeaderLabel');

      if (headerValEl && headerLabelEl) {
        if (currentProjectMetric === 'cost') {
          if (isFiltered) {
            const costVal = selProj ? selProj.cost : (costData.filtered_project_cost || 0);
            headerValEl.textContent = '$' + costVal.toFixed(2);
            headerLabelEl.textContent = `${currentProject} SPEND (${selProj ? selProj.percentage : 0}%)`;
          } else {
            headerValEl.textContent = '$' + totalCost.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            headerLabelEl.textContent = 'TOTAL SPEND';
          }
        } else {
          if (isFiltered) {
            const tokVal = selProj ? selProj.tokens : 0;
            const pctTok = totalTokens > 0 ? (tokVal / totalTokens * 100).toFixed(1) : '0';
            headerValEl.textContent = formatTokens(tokVal);
            headerLabelEl.textContent = `${currentProject} TOKENS (${pctTok}%)`;
          } else {
            headerValEl.textContent = formatTokens(totalTokens);
            headerLabelEl.textContent = 'TOTAL TOKENS';
          }
        }
      }

      const elCountBadge = document.getElementById('projectCountBadge');
      if (elCountBadge) {
        elCountBadge.textContent = `${projects.length} project${projects.length === 1 ? '' : 's'}`;
      }

      // 1. Chart.js Horizontal Bar Chart
      const chartCanvas = document.getElementById('projectCostChart');
      if (chartCanvas) {
        const ctx = chartCanvas.getContext('2d');
        const labels = projects.map(p => p.project);
        const isCost = (currentProjectMetric === 'cost');
        const dataValues = isCost ? projects.map(p => p.cost) : projects.map(p => p.tokens);

        const backgroundColors = [];
        const borderColors = [];
        const borderWidths = [];

        projects.forEach((p, idx) => {
          const baseColor = getProjectColor(p.project, idx);
          const isSelected = isFiltered && (p.project === currentProject);

          if (!isFiltered) {
            // Normal state: all projects vibrant
            backgroundColors.push(baseColor + 'cc');
            borderColors.push(baseColor);
            borderWidths.push(1);
          } else if (isSelected) {
            // Active selected project: intense highlight
            backgroundColors.push(baseColor);
            borderColors.push('#ffffff');
            borderWidths.push(2);
          } else {
            // Other projects: GRAYSCALE
            backgroundColors.push('rgba(75, 85, 99, 0.35)');
            borderColors.push('#4b5563');
            borderWidths.push(1);
          }
        });

        projectCostChart = upsertChart(projectCostChart, chartCanvas, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              data: dataValues,
              backgroundColor: backgroundColors,
              borderColor: borderColors,
              borderWidth: borderWidths,
              borderRadius: 4
            }]
          },
          options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            animations: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: (ctx) => {
                    const p = projects[ctx.dataIndex];
                    if (isCost) {
                      const costStr = '$' + ctx.raw.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                      return ` ${costStr} (${p.percentage}% share • ${p.sessions} sessions • ${formatTokens(p.tokens)} API tokens)`;
                    } else {
                      const tokShare = totalTokens > 0 ? (p.tokens / totalTokens * 100).toFixed(1) : '0';
                      return ` ${formatTokens(ctx.raw)} API tokens (${tokShare}% share • $${p.cost.toFixed(2)} • ${p.sessions} sessions)`;
                    }
                  }
                }
              }
            },
            onClick: (evt, elements) => {
              if (elements && elements.length > 0) {
                const idx = elements[0].index;
                const clickedProject = labels[idx];
                if (currentProject === clickedProject) {
                  window.handleProjectChange('all');
                } else {
                  window.handleProjectChange(clickedProject);
                }
              }
            },
            scales: {
              x: {
                beginAtZero: true,
                grid: { color: '#1f2937' },
                ticks: {
                  color: '#9ca3af',
                  font: { size: 10 },
                  callback: v => isCost ? ('$' + (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(v < 10 ? 2 : 0))) : formatTokens(v)
                }
              },
              y: {
                grid: { display: false },
                ticks: {
                  color: (ctx) => {
                    const label = labels[ctx.index];
                    if (!isFiltered) return '#e5e7eb';
                    return label === currentProject ? '#ffffff' : '#6b7280';
                  },
                  font: (ctx) => {
                    const label = labels[ctx.index];
                    const isSelected = isFiltered && label === currentProject;
                    return {
                      size: 11,
                      weight: isSelected ? 'bold' : 'normal'
                    };
                  }
                }
              }
            }
          }
        });
      }

      // 2. Top Cost Drivers Table
      const subEl = document.getElementById('topCostDriversSub');
      if (subEl) {
        if (currentProject && currentProject !== 'all') {
          subEl.textContent = `Highest-expenditure conversations in ${currentProject}`;
        } else {
          subEl.textContent = 'Highest-expenditure conversations across all projects';
        }
      }

      const scopeBadge = document.getElementById('topCostScopeBadge');
      if (scopeBadge) {
        if (currentProject && currentProject !== 'all') {
          scopeBadge.textContent = currentProject;
          scopeBadge.className = 'text-[11px] font-mono px-2 py-0.5 rounded bg-brand-500/20 border border-brand-500/40 text-brand-300';
        } else {
          scopeBadge.textContent = 'Global Top 6';
          scopeBadge.className = 'text-[11px] font-mono px-2 py-0.5 rounded bg-dark-800 border border-dark-700 text-gray-300';
        }
      }

      const topSessions = costData.top_sessions || [];
      const countBadge = document.getElementById('topCostCountBadge');
      if (countBadge) {
        countBadge.textContent = `${topSessions.length} session${topSessions.length === 1 ? '' : 's'}`;
      }

      const tbody = document.getElementById('topCostDriversBody');
      if (tbody) {
        tbody.innerHTML = '';
        if (topSessions.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" class="py-6 text-center text-gray-500 font-mono text-xs">No sessions recorded</td></tr>';
        } else {
          const isStatic = (typeof IS_STATIC_EXPORT !== "undefined" && IS_STATIC_EXPORT);
          topSessions.forEach(s => {
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-dark-800/60 transition';
            tr.innerHTML = `
              <td class="py-2 px-2 font-medium text-white max-w-[140px] truncate" title="${s.title}">${s.title}</td>
              <td class="py-2 px-2 whitespace-nowrap"><span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-dark-900 border border-dark-700 text-gray-300">${s.project}</span></td>
              <td class="py-2 px-2 text-purple-300 whitespace-nowrap text-[11px]">${s.model_name}</td>
              <td class="py-2 px-2 text-right font-semibold text-indigo-400 font-mono text-[11px]" title="${formatTokens(s.disk_tokens || 0)} disk footprint">${formatTokens(s.tokens || s.total_tokens)}</td>
              <td class="py-2 px-2 text-right font-mono font-bold text-rose-400 text-[11px]">$${s.billed_cost.toFixed(2)}</td>
              <td class="py-2 px-2 text-center">
                ${isStatic ? '' : `<button onclick="inspectSession('${s.id}')" class="px-2 py-0.5 rounded bg-brand-600/80 hover:bg-brand-600 text-[10px] font-medium text-white transition">Inspect</button>`}
              </td>
            `;
            tbody.appendChild(tr);
          });
        }
      }
    }

    function setToolFilter(filter) {
      currentToolFilter = filter;
      const btnAll = document.getElementById('toolBtnAll');
      const btnNative = document.getElementById('toolBtnNative');
      const btnMcp = document.getElementById('toolBtnMcp');
      const btnClasses = {
        active: 'px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition',
        inactive: 'px-2.5 py-1 rounded text-gray-400 hover:text-white transition'
      };
      if (btnAll) btnAll.className = filter === 'all' ? btnClasses.active : btnClasses.inactive;
      if (btnNative) btnNative.className = filter === 'native' ? btnClasses.active : btnClasses.inactive;
      if (btnMcp) btnMcp.className = filter === 'mcp' ? btnClasses.active : btnClasses.inactive;
      renderToolsChart(allToolsData);
    }

    function renderToolsChart(tools) {
      const canvas = document.getElementById('toolsChart');
      if (!canvas) return;
      let filtered = tools || [];
      if (currentToolFilter === 'native') {
        filtered = filtered.filter(t => !t.is_mcp);
      } else if (currentToolFilter === 'mcp') {
        filtered = filtered.filter(t => t.is_mcp);
      }

      const top10 = filtered.slice(0, 10);
      const labels = top10.map(t => t.tool_name);
      const data = top10.map(t => t.total_calls);

      const barBg = currentToolFilter === 'mcp' ? 'rgba(6, 182, 212, 0.75)' : 'rgba(245, 158, 11, 0.75)';
      const barBorder = currentToolFilter === 'mcp' ? '#06b6d4' : '#f59e0b';

      toolsChart = upsertChart(toolsChart, canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'Calls',
            data: data,
            backgroundColor: barBg,
            borderColor: barBorder,
            borderWidth: 1,
            borderRadius: 4
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', font: { size: 10 } } },
            y: { grid: { display: false }, ticks: { color: '#e5e7eb', font: { size: 11, family: 'monospace' } } }
          }
        }
      });
    }

    function renderMcpSection(mcpData) {
      allMcpData = mcpData || [];
      const canvas = document.getElementById('mcpServerChart');
      if (!canvas || !allMcpData || allMcpData.length === 0) return;

      const labels = allMcpData.map(s => s.server);
      const data = allMcpData.map(s => s.calls);
      const colors = ['#06b6d4', '#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#a855f7', '#64748b'];

      mcpServerChart = upsertChart(mcpServerChart, canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.slice(0, labels.length).map(c => c + 'cc'),
            borderColor: colors.slice(0, labels.length),
            borderWidth: 1
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${ctx.raw.toLocaleString()} calls`
              }
            }
          },
          scales: {
            x: {
              beginAtZero: true,
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', font: { size: 10 } }
            },
            y: {
              grid: { display: false },
              ticks: { color: '#9ca3af', font: { size: 10 } }
            }
          }
        }
      });

      const leg = document.getElementById('mcpServerLegend');
      if (leg) {
        leg.innerHTML = '';
        const total = data.reduce((a, b) => a + b, 0) || 1;
        allMcpData.forEach((s, idx) => {
          const pct = ((s.calls / total) * 100).toFixed(1);
          const color = colors[idx % colors.length];
          leg.innerHTML += `
            <div class="flex items-center space-x-1.5">
              <div class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color: ${color}"></div>
              <span class="text-gray-300 font-medium truncate max-w-[90px]" title="${s.server}">${s.server}</span>
              <span class="text-gray-400 ml-auto font-mono text-[11px]">${pct}%</span>
            </div>
          `;
        });
      }

      const select = document.getElementById('mcpServerSelect');
      if (select) {
        select.innerHTML = '<option value="all">All Servers</option>';
        allMcpData.forEach(s => {
          select.innerHTML += `<option value="${s.server}">${s.server} (${s.calls})</option>`;
        });
      }

      filterMcpToolsTable();
    }

    function filterMcpToolsTable() {
      const select = document.getElementById('mcpServerSelect');
      const selectedServer = select ? select.value : 'all';
      const tbody = document.getElementById('mcpToolsTableBody');
      if (!tbody) return;
      tbody.innerHTML = '';

      let toolsList = [];
      allMcpData.forEach(s => {
        if (selectedServer === 'all' || selectedServer === s.server) {
          (s.tools || []).forEach(t => {
            toolsList.push({
              server: s.server,
              tool: t.tool,
              calls: t.calls,
              sessions: t.sessions,
              error_rate: t.error_rate
            });
          });
        }
      });

      toolsList.sort((a, b) => b.calls - a.calls);

      if (toolsList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-gray-500">No MCP tool invocations found</td></tr>';
        return;
      }

      toolsList.forEach(t => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/50 transition';
        tr.innerHTML = `
          <td class="py-1.5 px-3 font-mono text-cyan-300 font-medium">${t.tool}</td>
          <td class="py-1.5 px-3"><span class="px-2 py-0.5 rounded text-[10px] font-mono bg-dark-700 text-gray-300 border border-dark-600">${t.server}</span></td>
          <td class="py-1.5 px-3 text-right font-semibold text-white">${t.calls.toLocaleString()}</td>
          <td class="py-1.5 px-3 text-right text-gray-400 font-mono">${t.sessions.toLocaleString()}</td>
          <td class="py-1.5 px-3 text-right font-mono ${t.error_rate > 0 ? 'text-rose-400 font-bold' : 'text-emerald-400'}">${t.error_rate.toFixed(1)}%</td>
        `;
        tbody.appendChild(tr);
      });
    }

    function renderCategoryChart(categories) {
      const canvas = document.getElementById('categoryChart');
      if (!canvas || !categories) return;
      const top7 = categories.slice(0, 7);
      const labels = top7.map(c => c.category);
      const data = top7.map(c => c.tokens);
      const colors = ['#10b981', '#6366f1', '#3b82f6', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b'];

      categoryChart = upsertChart(categoryChart, canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.map(c => c + 'aa'),
            borderColor: colors,
            borderWidth: 1
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          scales: {
            x: {
              beginAtZero: true,
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', callback: v => formatTokens(v), font: { size: 10 } }
            },
            y: {
              grid: { display: false },
              ticks: { color: '#9ca3af', font: { size: 10 } }
            }
          }
        }
      });
    }

    function renderPhaseChart(phases) {
      const canvas = document.getElementById('phaseChart');
      if (!canvas || !phases || phases.length === 0) return;

      const labels = phases.map(p => p.phase.charAt(0).toUpperCase() + p.phase.slice(1));
      const data = phases.map(p => p.tokens);
      const colors = {
        'Reasoning': '#6366f1',
        'Discovery': '#f59e0b',
        'Execution': '#10b981',
        'Verification': '#a855f7',
        'Other': '#6b7280'
      };
      const bgColors = labels.map(l => colors[l] || '#6366f1');

      phaseChart = upsertChart(phaseChart, canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: bgColors.map(c => c + 'cc'),
            borderColor: bgColors,
            borderWidth: 1
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          scales: {
            x: {
              beginAtZero: true,
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', callback: v => formatTokens(v), font: { size: 10 } }
            },
            y: {
              grid: { display: false },
              ticks: { color: '#9ca3af', font: { size: 10 } }
            }
          }
        }
      });

      const leg = document.getElementById('phaseLegend');
      if (leg) {
        leg.innerHTML = '';
        const total = data.reduce((a, b) => a + b, 0) || 1;
        phases.forEach(p => {
          const name = p.phase.charAt(0).toUpperCase() + p.phase.slice(1);
          const pct = ((p.tokens / total) * 100).toFixed(1);
          const color = colors[name] || '#6366f1';
          leg.innerHTML += `
            <div class="flex items-center space-x-1.5">
              <div class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color: ${color}"></div>
              <span class="text-gray-300 font-medium">${name}</span>
              <span class="text-gray-400 ml-auto font-mono text-[11px]">${pct}%</span>
            </div>
          `;
        });
      }
    }

    function renderHotFiles(files) {
      allHotFiles = files;
      filterHotFiles();
    }

    function setHotFilesTypeFilter(filterType) {
      currentHotFilesFilter = filterType;
      
      const btnClasses = {
        active: 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap',
        inactive: 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap'
      };

      document.getElementById('hfBtnFiles').className = filterType === 'files' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hfBtnDirs').className = filterType === 'dirs' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hfBtnAll').className = filterType === 'all' ? btnClasses.active : btnClasses.inactive;

      if (filterType === 'dirs') {
        document.getElementById('hfColViews').textContent = 'Searches';
        document.getElementById('hfColEdits').textContent = 'Listings';
      } else {
        document.getElementById('hfColViews').textContent = 'Views';
        document.getElementById('hfColEdits').textContent = 'Edits';
      }

      filterHotFiles();
    }

    function filterHotFiles() {
      const q = document.getElementById('hotFilesSearch').value.toLowerCase();
      const tbody = document.getElementById('hotFilesTableBody');
      tbody.innerHTML = '';

      let filtered = allHotFiles.filter(f => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));

      if (currentHotFilesFilter === 'files') {
        filtered = filtered.filter(f => !f.is_dir);
      } else if (currentHotFilesFilter === 'dirs') {
        filtered = filtered.filter(f => f.is_dir);
      }

      for (const f of filtered.slice(0, 40)) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/60 transition';
        
        let typeBadge = '';
        if (f.is_dir) {
          typeBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-amber-950/60 border border-amber-500/30 text-amber-300 flex items-center w-fit space-x-1"><i data-lucide="folder" class="w-2.5 h-2.5 inline mr-1"></i>dir</span>';
        } else {
          const extBadgeClass = f.type === '.ts' ? 'bg-blue-900/60 text-blue-300' :
                               f.type === '.java' ? 'bg-amber-900/60 text-amber-300' :
                               f.type === '.html' ? 'bg-orange-900/60 text-orange-300' :
                               f.type === '.json' ? 'bg-emerald-900/60 text-emerald-300' :
                               f.type === '.md' ? 'bg-purple-900/60 text-purple-300' :
                               'bg-gray-800 text-gray-300';
          typeBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono ${extBadgeClass}">${f.type || 'file'}</span>`;
        }

        const col4Val = f.is_dir ? (f.searches || 0).toLocaleString() : (f.views || 0).toLocaleString();
        const col5Val = f.is_dir ? '-' : (f.edits || 0).toLocaleString();
        const col5Class = f.is_dir ? 'text-gray-500' : 'text-indigo-300 font-semibold';

        tr.innerHTML = `
          <td class="py-2.5 px-3 font-mono font-medium text-white max-w-xs truncate" title="${f.path}">
            ${f.is_dir ? '📁 ' : '📄 '}${f.name}
          </td>
          <td class="py-2.5 px-3">${typeBadge}</td>
          <td class="py-2.5 px-3 text-right font-semibold text-white">${f.touches.toLocaleString()}</td>
          <td class="py-2.5 px-3 text-right text-gray-400">${col4Val}</td>
          <td class="py-2.5 px-3 text-right ${col5Class}">${col5Val}</td>
          <td class="py-2.5 px-3 text-right text-gray-400">${f.sessions.toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
      }
      lucide.createIcons();
    }

    function renderConversations(convs) {
      allConversations = convs;
      filterConversations();
    }

    function filterConversations() {
      const el = document.getElementById('convSearch');
      if (!el) return;
      const q = el.value.toLowerCase();
      const tbody = document.getElementById('convTableBody');
      if (!tbody) return;
      tbody.innerHTML = '';

      const filtered = allConversations.filter(c => c.title.toLowerCase().includes(q) || c.id.toLowerCase().includes(q) || (c.created_at && c.created_at.includes(q)));

      for (const c of filtered.slice(0, 50)) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/60 transition';
        
        const dateStr = c.created_at || '--';
        const activeSec = c.active_agent_seconds || c.active_pairing_seconds || c.duration_seconds || 0;
        const wallSec = c.duration_seconds || 0;
        const timeTitle = `Agent Work: ${formatDuration(c.active_agent_seconds || 0)} | Active Pairing: ${formatDuration(c.active_pairing_seconds || 0)} | Total Span: ${formatDuration(wallSec)}`;
        const isExpanded = expandedConvs.has(c.id);

        let titleHtml = '';
        let stepsHtml = '';
        let costHtml = '';
        let tokensHtml = '';

        let compactionBadge = '';
        if (c.compaction_count > 0) {
          compactionBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium bg-amber-950/80 text-amber-300 border border-amber-500/40 shrink-0" title="${c.compaction_count} compaction events (~${formatTokens(c.pruned_tokens)} tokens pruned from context)">⚡ ${c.compaction_count} pruned</span>`;
        } else if (c.fleet_compaction_count > 0) {
          compactionBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium bg-amber-950/60 text-amber-400 border border-amber-500/30 shrink-0" title="${c.fleet_compaction_count} fleet compaction events (~${formatTokens(c.fleet_pruned_tokens)} tokens pruned across swarm)">⚡ ${c.fleet_compaction_count} fleet pruned</span>`;
        }

        if (currentConvViewMode === 'hierarchy' && c.subagent_count > 0) {
          titleHtml = `
            <div class="flex items-center space-x-1.5 max-w-sm flex-wrap gap-y-1">
              <button onclick="toggleSubagents('${c.id}')" class="p-0.5 rounded hover:bg-dark-700 text-gray-400 hover:text-white transition font-mono text-xs select-none shrink-0" title="Toggle subagents">
                ${isExpanded ? '▼' : '▶'}
              </button>
              <span class="font-medium text-white truncate" title="${c.title}">${c.title}</span>
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-pink-950/80 text-pink-300 border border-pink-500/40 shrink-0 font-medium">
                ${c.subagent_count} sub-agent${c.subagent_count === 1 ? '' : 's'}
              </span>
              ${compactionBadge}
            </div>
          `;
          stepsHtml = `<span class="font-mono text-gray-200 text-xs">${c.total_steps}</span> <span class="text-[10px] text-gray-500 font-mono">/ ${c.fleet_steps}</span>`;
          const cWkStr = c.weekly_quota_pct != null ? ` <span class="text-[10px] text-cyan-300 font-mono">(${c.weekly_quota_pct.toFixed(2)}% wk)</span>` : '';
          const flWkStr = c.fleet_weekly_quota_pct != null ? ` <span class="text-[10px] text-pink-300 font-mono font-medium">(${c.fleet_weekly_quota_pct.toFixed(2)}% wk)</span>` : '';
          costHtml = `<span class="font-bold text-rose-400 font-mono text-[11px]">$${(c.billed_cost || 0).toFixed(2)}${cWkStr}</span> <span class="text-[10px] text-pink-300 font-mono font-medium">/ $${(c.fleet_cost || 0).toFixed(2)}${flWkStr}</span>`;
          tokensHtml = `<span class="font-semibold text-indigo-400 font-mono text-[11px]">${formatTokens(c.tokens || c.total_tokens)}</span> <span class="text-[10px] text-purple-300 font-mono font-medium">/ ${formatTokens(c.fleet_tokens)}</span>`;
        } else {
          let roleBadge = '';
          if (c.parent_id || c.subagent_role) {
            roleBadge = `<span class="mr-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-pink-950/80 text-pink-300 border border-pink-500/40 font-mono shrink-0">${c.subagent_role || 'Sub-Agent'}</span>`;
          }
          titleHtml = `
            <div class="flex items-center space-x-1 max-w-sm flex-wrap gap-y-1">
              ${roleBadge}
              <span class="font-medium text-white truncate" title="${c.title}">${c.title}</span>
              ${compactionBadge}
            </div>
          `;
          stepsHtml = `<span class="text-gray-300 font-mono text-xs">${c.total_steps}</span>`;
          const singleWkStr = c.weekly_quota_pct != null ? ` <span class="text-[10px] text-cyan-300 font-mono">(${c.weekly_quota_pct.toFixed(2)}% wk)</span>` : '';
          costHtml = `<span class="font-bold text-rose-400 font-mono text-[11px]">$${(c.billed_cost || 0).toFixed(2)}${singleWkStr}</span>`;
          tokensHtml = `<span class="font-semibold text-indigo-400 font-mono text-[11px]" title="${formatTokens(c.disk_tokens || 0)} disk footprint">${formatTokens(c.tokens || c.total_tokens)}</span>`;
        }

        tr.innerHTML = `
          <td class="py-2.5 px-3 text-gray-400 whitespace-nowrap font-mono text-[11px]">${dateStr}</td>
          <td class="py-2.5 px-3">${titleHtml}</td>
          <td class="py-2.5 px-3 max-w-[220px] align-middle">${formatModelBadgeHtml(c.model_name)}</td>
          <td class="py-2.5 px-3 text-right">${stepsHtml}</td>
          <td class="py-2.5 px-3 text-right">${costHtml}</td>
          <td class="py-2.5 px-3 text-right">${tokensHtml}</td>
          <td class="py-2.5 px-3 text-right text-gray-300 whitespace-nowrap" title="${timeTitle}">
            <span class="font-medium text-emerald-400 text-xs">${formatDuration(activeSec)}</span>
            <span class="text-[10px] text-gray-500 block font-mono">(${formatDuration(wallSec)})</span>
          </td>
          <td class="py-2.5 px-3 text-center">
            ${(typeof IS_STATIC_EXPORT !== "undefined" && IS_STATIC_EXPORT) ? "" : `<button onclick="inspectSession('${c.id}')" class="px-2.5 py-1 rounded bg-brand-600/80 hover:bg-brand-600 text-[11px] font-medium text-white transition">Inspect</button>`}
          </td>
        `;
        tbody.appendChild(tr);

        // If expanded in hierarchy view, render child rows
        if (currentConvViewMode === 'hierarchy' && isExpanded && c.subagents && c.subagents.length > 0) {
          for (const sub of c.subagents) {
            const childTr = document.createElement('tr');
            childTr.className = 'bg-dark-900/40 hover:bg-dark-800/40 transition border-l-2 border-pink-500/50';
            const childActiveSec = sub.active_agent_seconds || sub.duration_seconds || 0;
            const childWallSec = sub.duration_seconds || 0;
            const childTimeTitle = `Agent Work: ${formatDuration(sub.active_agent_seconds || 0)} | Total Span: ${formatDuration(childWallSec)}`;

            let subCompactionBadge = '';
            if (sub.compaction_count > 0) {
              subCompactionBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium bg-amber-950/80 text-amber-300 border border-amber-500/40 shrink-0" title="${sub.compaction_count} compaction events (~${formatTokens(sub.pruned_tokens)} tokens pruned)">⚡ ${sub.compaction_count} pruned</span>`;
            }

            childTr.innerHTML = `
              <td class="py-2 px-3 text-gray-500 whitespace-nowrap font-mono text-[10px] pl-6">${sub.created_at || '--'}</td>
              <td class="py-2 px-3 pl-8">
                <div class="flex items-center space-x-2 max-w-sm flex-wrap gap-y-1">
                  <span class="text-pink-400 font-mono text-xs">↳</span>
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-pink-950/80 text-pink-300 border border-pink-500/40 font-mono shrink-0">${sub.subagent_role || 'Worker'}</span>
                  <span class="font-mono text-gray-300 truncate text-xs" title="${sub.title}">${sub.title}</span>
                  ${subCompactionBadge}
                </div>
              </td>
              <td class="py-2 px-3 max-w-[220px] align-middle">${formatModelBadgeHtml(sub.model_name || 'inherit')}</td>
              <td class="py-2 px-3 text-right text-gray-400 font-mono text-xs">${sub.total_steps}</td>
              <td class="py-2 px-3 text-right font-mono font-medium text-rose-400/80 text-xs">$${(sub.billed_cost || 0).toFixed(2)}${sub.weekly_quota_pct != null ? ` <span class="text-[10px] text-cyan-300 font-mono">(${sub.weekly_quota_pct.toFixed(2)}% wk)</span>` : ''}</td>
              <td class="py-2 px-3 text-right font-mono font-medium text-indigo-400/80 text-xs">${formatTokens(sub.tokens || sub.total_tokens)}</td>
              <td class="py-2 px-3 text-right text-gray-400 whitespace-nowrap text-xs" title="${childTimeTitle}">
                <span class="text-emerald-400/80">${formatDuration(childActiveSec)}</span>
              </td>
              <td class="py-2 px-3 text-center">
                ${(typeof IS_STATIC_EXPORT !== "undefined" && IS_STATIC_EXPORT) ? "" : `<button onclick="inspectSession('${sub.id}')" class="px-2 py-0.5 rounded bg-pink-600/70 hover:bg-pink-600 text-[10px] font-medium text-white transition">Inspect</button>`}
              </td>
            `;
            tbody.appendChild(childTr);
          }
        }
      }
      lucide.createIcons();
    }

    async function inspectSession(cid) {
      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        const res = await fetch(`/api/session/${cid}?tz=${encodeURIComponent(clientTz)}`);
        const data = await res.json();
        if (!data) return;

        const conv = data.conversation;
        document.getElementById('modalTitle').textContent = conv.title || `Session ${conv.id.substring(0, 8)}`;
        document.getElementById('modalSub').textContent = `ID: ${conv.id} • Started: ${conv.created_at || '--'}`;
        
        const distinctModels = [...new Set(data.steps.map(s => s.model_name).filter(Boolean))];
        const modelLabel = distinctModels.length > 1 ? distinctModels.join(' ➔ ') : (conv.model_name || 'Gemini');
        document.getElementById('modalModel').innerHTML = formatModelBadgeHtml(modelLabel);
        
        const activeSec = conv.active_agent_seconds || conv.active_pairing_seconds || conv.duration_seconds || 0;
        document.getElementById('modalDuration').textContent = formatDuration(activeSec);
        const modalWallSpanEl = document.getElementById('modalWallSpan');
        if (modalWallSpanEl) {
          modalWallSpanEl.textContent = `Span: ${formatDuration(conv.duration_seconds || 0)}`;
        }

        // Fix MIA Total Tokens & Total Steps
        const modalTokensEl = document.getElementById('modalTokens');
        if (modalTokensEl) {
          const tokVal = conv.tokens || conv.total_tokens || 0;
          modalTokensEl.textContent = formatTokens(tokVal);
          modalTokensEl.title = `${tokVal.toLocaleString()} active tokens (Disk Footprint: ${(conv.disk_tokens || conv.total_tokens || 0).toLocaleString()} tokens)`;
        }

        const modalStepsEl = document.getElementById('modalSteps');
        if (modalStepsEl) {
          const stepVal = conv.total_steps || (data.steps ? data.steps.length : 0);
          modalStepsEl.textContent = stepVal.toLocaleString();
          modalStepsEl.title = `${stepVal.toLocaleString()} logged steps in this conversation`;
        }

        // Context Compaction & Working Memory Status
        const compBanner = document.getElementById('modalCompactionBanner');
        const compCard = document.getElementById('modalCompactionCard');
        const compScore = document.getElementById('modalCompactionScore');
        const compStats = document.getElementById('modalCompactionStats');
        const compTitle = document.getElementById('modalCompactionTitle');
        const compDesc = document.getElementById('modalCompactionDesc');

        const compCount = conv.compaction_count || (data.compaction ? data.compaction.count : 0) || 0;
        const prunedTok = conv.pruned_tokens || (data.compaction ? data.compaction.pruned_tokens : 0) || 0;

        if (compCount > 0 || prunedTok > 0) {
          if (compBanner) {
            compBanner.className = 'flex rounded-lg p-2.5 text-xs border items-center justify-between bg-amber-950/40 border-amber-500/50 text-amber-300';
            if (compTitle) compTitle.textContent = 'Context Compaction Detected';
            if (compDesc) compDesc.textContent = `${compCount} truncation event${compCount === 1 ? '' : 's'} pruned earlier history to respect model limits`;
            if (compStats) compStats.textContent = `~${formatTokens(prunedTok)} freed (${prunedTok.toLocaleString()} tokens)`;
          }
          if (compScore) {
            compScore.textContent = `⚡ ${compCount} Prunes`;
            compScore.className = 'font-semibold text-amber-400 font-mono mt-0.5 block';
          }
          if (compCard) {
            compCard.title = `Working memory health: ${compCount} historical compaction event(s) trimmed ~${formatTokens(prunedTok)} (${prunedTok.toLocaleString()} tokens) from active attention window.`;
          }
        } else {
          if (compBanner) {
            compBanner.className = 'hidden';
          }
          if (compScore) {
            compScore.textContent = '100% Retained';
            compScore.className = 'font-semibold text-emerald-400 font-mono mt-0.5 block';
          }
          if (compCard) {
            compCard.title = 'Working memory health: 100% of conversation history is currently retained in active attention window with 0 compactions.';
          }
        }

        // Populate Scorecard
        const toolsCnt = conv.tool_calls_count || 0;
        const promptsCnt = conv.user_prompts || 1;
        const autonomyRatio = (toolsCnt / Math.max(1, promptsCnt)).toFixed(1);
        const elAutonomy = document.getElementById('modalAutonomy');
        if (elAutonomy) elAutonomy.textContent = `${autonomyRatio}x (${toolsCnt} tools)`;
        const cardModalAutonomy = document.getElementById('modalAutonomyCard');
        if (cardModalAutonomy) {
          cardModalAutonomy.title = `Autonomous leverage: ${autonomyRatio}x\n${toolsCnt.toLocaleString()} automated operations executed across ${promptsCnt.toLocaleString()} human prompts in this session.`;
        }

        const elThinking = document.getElementById('modalThinking');
        if (elThinking) elThinking.textContent = formatTokens(conv.thinking_tokens || 0);
        const cardModalThinking = document.getElementById('modalThinkingCard');
        if (cardModalThinking) {
          cardModalThinking.title = `Internal chain-of-thought: ${(conv.thinking_tokens || 0).toLocaleString()} tokens\nGenerated during deep reasoning and deliberative planning phases in this session.`;
        }

        const elReReads = document.getElementById('modalReReads');
        const reReads = conv.redundant_file_reads || 0;
        if (elReReads) {
          elReReads.textContent = reReads === 0 ? '✓ 0 re-reads' : `⚠️ ${reReads} repeated`;
          elReReads.className = reReads === 0 ? 'font-semibold text-emerald-400 font-mono mt-0.5 block' : 'font-semibold text-amber-400 font-mono mt-0.5 block';
        }
        const cardModalReReads = document.getElementById('modalReReadsCard');
        if (cardModalReReads) {
          cardModalReReads.title = `Context cache health: ${reReads} repeated file views in this session (~${formatTokens(reReads * 750)} estimated wasted context tokens).`;
        }

        const elCodeLines = document.getElementById('modalCodeLines');
        if (elCodeLines) elCodeLines.textContent = `+${(conv.lines_written || 0).toLocaleString()} / ~${(conv.lines_edited || 0).toLocaleString()}`;
        const cardModalCodeLines = document.getElementById('modalCodeLinesCard');
        if (cardModalCodeLines) {
          const written = (conv.lines_written || 0).toLocaleString();
          const edited = (conv.lines_edited || 0).toLocaleString();
          const total = ((conv.lines_written || 0) + (conv.lines_edited || 0)).toLocaleString();
          cardModalCodeLines.title = `Code authored in this session: ${total} total lines\n• +${written} new lines written (write_to_file)\n• ~${edited} modified lines replaced (replace_file_content)`;
        }

        if (data.quota || conv.quota) {
          const q = data.quota || conv.quota;
          const sessWk = q.session_weekly_pct != null ? q.session_weekly_pct : (q.weekly_pct || 0);
          const sess5h = q.session_5h_pct != null ? q.session_5h_pct : (q.five_hour_pct || 0);
          const elSessWk = document.getElementById('sessionQuotaImpactWk');
          const elSess5h = document.getElementById('sessionQuotaImpact5h');
          if (elSessWk) elSessWk.textContent = `${Number(sessWk).toFixed(2)}% weekly`;
          if (elSess5h) elSess5h.textContent = `${Number(sess5h).toFixed(2)}% 5h`;
        }

        function formatToolBadge(toolOrSource, activity) {
          const isMcp = (activity && activity.startsWith('MCP')) || (toolOrSource && (toolOrSource.includes(':') || toolOrSource.startsWith('`mcp')));
          if (isMcp) {
            return `<span class="px-1.5 py-0.5 rounded text-[11px] font-mono bg-cyan-950/60 text-cyan-300 border border-cyan-500/30">${toolOrSource}</span>`;
          }
          return `<span class="text-indigo-300 font-mono">${toolOrSource}</span>`;
        }

        const tcBody = document.getElementById('modalTopConsumers');
        tcBody.innerHTML = '';
        for (const s of data.top_consumers) {
          const costStr = (s.billed_cost != null && s.billed_cost > 0)
            ? `$${s.billed_cost.toFixed(4)}${s.weekly_quota_pct != null ? ` <span class="text-[10px] text-cyan-300 font-mono">(${s.weekly_quota_pct.toFixed(2)}% wk)</span>` : ''}`
            : '$0.00';
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-2 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-2 px-2.5 font-medium text-white">${s.activity}</td>
            <td class="py-2 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-2 px-2.5 text-right font-semibold text-white">${formatTokens(s.est_tokens)}</td>
            <td class="py-2 px-2.5 text-right font-mono text-gray-300 whitespace-nowrap">${costStr}</td>
            <td class="py-2 px-2.5 text-gray-400 max-w-xs truncate" title="${s.detail || ''}">${s.detail || '--'}</td>
          `;
          tcBody.appendChild(tr);
        }

        const sBody = document.getElementById('modalStepList');
        sBody.innerHTML = '';
        let lastModel = null;
        for (const s of data.steps) {
          const isModelSwitch = lastModel && s.model_name && s.model_name !== lastModel;
          lastModel = s.model_name || lastModel;

          if (isModelSwitch) {
            const switchTr = document.createElement('tr');
            switchTr.className = 'bg-brand-950/60 border-y border-brand-500/30';
            switchTr.innerHTML = `
              <td colspan="6" class="py-1 px-2.5 text-center text-xs font-semibold text-brand-300">
                🔄 Model Switched to: <span class="text-white">${s.model_name}</span>
              </td>
            `;
            sBody.appendChild(switchTr);
          }

          const sType = s.step_type || s.activity || '';
          if (sType === 'CHECKPOINT' || sType === 'CONVERSATION_HISTORY' || sType.includes('Compaction')) {
            const compTr = document.createElement('tr');
            compTr.className = 'bg-amber-950/60 border-y border-amber-500/50';
            compTr.innerHTML = `
              <td colspan="6" class="py-1.5 px-2.5 text-center text-xs font-semibold text-amber-300">
                ⚡ Memory Compaction Checkpoint #${s.step_index}: Context window was truncated/compacted at this step
              </td>
            `;
            sBody.appendChild(compTr);
          }

          const stepCostStr = (s.billed_cost != null && s.billed_cost > 0)
            ? `$${s.billed_cost.toFixed(4)}${s.weekly_quota_pct != null ? ` <span class="text-[10px] text-cyan-300 font-mono">(${s.weekly_quota_pct.toFixed(2)}% wk)</span>` : ''}`
            : '$0.00';
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-1.5 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-1.5 px-2.5 text-gray-300">${s.activity}</td>
            <td class="py-1.5 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-1.5 px-2.5 text-right text-gray-400">${formatTokens(s.est_tokens)}</td>
            <td class="py-1.5 px-2.5 text-right font-mono text-gray-300 whitespace-nowrap">${stepCostStr}</td>
            <td class="py-1.5 px-2.5 text-gray-400 max-w-md truncate" title="${s.detail || ''}">${s.detail || '--'}</td>
          `;
          sBody.appendChild(tr);
        }

        currentSessionData = data;
        
        // Modal breadcrumb lineage trail
        const breadcrumbsEl = document.getElementById('modalBreadcrumbs');
        if (breadcrumbsEl) {
          breadcrumbsEl.innerHTML = '';
          if (data.ancestors && data.ancestors.length > 0) {
            for (const a of data.ancestors) {
              const btn = document.createElement('button');
              btn.className = 'text-brand-400 hover:text-brand-300 underline transition truncate max-w-[160px] text-xs font-mono';
              btn.title = a.title;
              btn.textContent = a.role ? `[${a.role}] ${a.title}` : a.title;
              btn.onclick = () => inspectSession(a.id);
              breadcrumbsEl.appendChild(btn);

              const sep = document.createElement('span');
              sep.className = 'text-gray-600 font-sans text-xs px-0.5';
              sep.textContent = '>';
              breadcrumbsEl.appendChild(sep);
            }
            const currentSpan = document.createElement('span');
            currentSpan.className = 'text-gray-300 font-semibold truncate max-w-[200px] text-xs font-mono';
            currentSpan.textContent = conv.subagent_role ? `[${conv.subagent_role}] ${conv.title || 'Current'}` : (conv.title || 'Current');
            breadcrumbsEl.appendChild(currentSpan);
            breadcrumbsEl.classList.remove('hidden');
          } else {
            breadcrumbsEl.classList.add('hidden');
          }
        }

        const btnSwarmText = document.getElementById('btnViewSwarmText');
        if (btnSwarmText) {
          const count = (data.fleet_summary && data.fleet_summary.subagent_count > 0) 
            ? data.fleet_summary.subagent_count 
            : (data.children ? data.children.length : 0);
          btnSwarmText.textContent = count > 0 ? `Swarm Fleet (${count})` : 'Swarm Fleet';
        }

        populateDiffSelectors(data.steps);
        renderFlowSequence(data.steps);
        renderSwarmView(data);
        setSessionViewMode('flow');

        document.getElementById('sessionModal').classList.remove('hidden');
        document.getElementById('sessionModal').classList.add('flex');
        lucide.createIcons();
      } catch (err) {
        console.error("Failed to fetch session", err);
      }
    }

    function setSessionViewMode(mode) {
      const btnTable = document.getElementById('btnViewTable');
      const btnFlow = document.getElementById('btnViewFlow');
      const btnDiff = document.getElementById('btnViewDiff');
      const btnSwarm = document.getElementById('btnViewSwarm');
      const tableContainer = document.getElementById('sessionViewTable');
      const flowContainer = document.getElementById('sessionViewFlow');
      const diffContainer = document.getElementById('sessionViewDiff');
      const swarmContainer = document.getElementById('sessionViewSwarm');

      const activeClass = 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap';
      const inactiveClass = 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap';

      btnTable.className = mode === 'table' ? activeClass : inactiveClass;
      btnFlow.className = mode === 'flow' ? activeClass : inactiveClass;
      if (btnDiff) {
        btnDiff.className = (mode === 'diff' ? activeClass : inactiveClass) + ' flex items-center space-x-1';
      }
      if (btnSwarm) {
        btnSwarm.className = (mode === 'swarm' ? activeClass : inactiveClass) + ' flex items-center space-x-1';
      }

      tableContainer.classList.toggle('hidden', mode !== 'table');
      flowContainer.classList.toggle('hidden', mode !== 'flow');
      if (diffContainer) {
        diffContainer.classList.toggle('hidden', mode !== 'diff');
      }
      if (swarmContainer) {
        swarmContainer.classList.toggle('hidden', mode !== 'swarm');
      }

      if (mode === 'diff') {
        onDiffStepChange();
      }
    }

    function renderSwarmView(data) {
      const summaryEl = document.getElementById('modalSwarmSummary');
      const badgeEl = document.getElementById('modalSwarmBadge');
      const treeEl = document.getElementById('modalSwarmTree');
      if (!summaryEl || !treeEl) return;

      const conv = data.conversation || {};
      const summary = data.fleet_summary || {};
      const totalAgents = summary.subagent_count ? (summary.subagent_count + 1) : ((data.children && data.children.length > 0) ? (data.children.length + 1) : 1);
      const totalTokens = summary.total_tokens || conv.tokens || conv.total_tokens || 0;
      const totalCost = summary.total_cost || conv.billed_cost || 0;
      const totalSteps = summary.total_steps || conv.total_steps || 0;
      const localTokens = conv.tokens || conv.total_tokens || 0;
      const shieldedTokens = Math.max(0, totalTokens - localTokens);
      const shieldingRatio = localTokens > 0 ? (totalTokens / localTokens).toFixed(1) : '1.0';

      summaryEl.innerHTML = `
        <div class="p-2.5 rounded-lg bg-pink-950/30 border border-pink-500/40">
          <span class="text-gray-400 block text-[10px] uppercase font-medium">Swarm Fleet</span>
          <span class="font-bold text-pink-400 font-mono mt-0.5 block text-sm">${totalAgents} Agents</span>
          <span class="text-[10px] text-gray-400">${(totalAgents > 1 ? (totalAgents - 1) + ' delegated' : 'Standalone')}</span>
        </div>
        <div class="p-2.5 rounded-lg bg-indigo-950/30 border border-indigo-500/40">
          <span class="text-gray-400 block text-[10px] uppercase font-medium">Fleet Tokens</span>
          <span class="font-bold text-indigo-400 font-mono mt-0.5 block text-sm">${formatTokens(totalTokens)}</span>
          <span class="text-[10px] text-gray-400">${formatTokens(shieldedTokens)} shielded</span>
        </div>
        <div class="p-2.5 rounded-lg bg-rose-950/30 border border-rose-500/40">
          <span class="text-gray-400 block text-[10px] uppercase font-medium">Fleet Cost</span>
          <span class="font-bold text-rose-400 font-mono mt-0.5 block text-sm">$${Number(totalCost).toFixed(2)}</span>
          <span class="text-[10px] text-gray-400">across ${totalSteps} ops</span>
        </div>
        <div class="p-2.5 rounded-lg bg-emerald-950/30 border border-emerald-500/40">
          <span class="text-gray-400 block text-[10px] uppercase font-medium">Context Shielding</span>
          <span class="font-bold text-emerald-400 font-mono mt-0.5 block text-sm">${shieldingRatio}x</span>
          <span class="text-[10px] text-gray-400">parent window saved</span>
        </div>
      `;

      if (badgeEl) {
        badgeEl.textContent = `${totalAgents} agent session${totalAgents === 1 ? '' : 's'} in campaign`;
      }

      treeEl.innerHTML = '';

      function renderTreeNode(node, depth = 0, isLast = false) {
        const nodeCard = document.createElement('div');
        const isCurrent = node.is_current;
        const indentClass = depth === 0 ? '' : depth === 1 ? 'ml-6 border-l-2 border-pink-500/40 pl-3' : 'ml-12 border-l-2 border-purple-500/40 pl-3';
        
        nodeCard.className = `p-3 rounded-lg border transition ${indentClass} ${
          isCurrent 
            ? 'bg-brand-950/40 border-brand-500 ring-1 ring-brand-500/50 shadow-md' 
            : 'bg-dark-900/70 border-dark-700 hover:border-dark-600'
        }`;

        const roleBadgeClass = depth === 0 
          ? 'bg-brand-500/20 text-brand-300 border-brand-500/40' 
          : depth === 1 
            ? 'bg-pink-500/20 text-pink-300 border-pink-500/40' 
            : 'bg-purple-500/20 text-purple-300 border-purple-500/40';

        nodeCard.innerHTML = `
          <div class="flex items-center justify-between flex-wrap gap-2">
            <div class="flex items-center space-x-2">
              ${depth > 0 ? `<span class="text-pink-400 font-mono text-xs">↳</span>` : ''}
              <span class="px-2 py-0.5 rounded text-[11px] font-semibold font-mono border ${roleBadgeClass}">
                ${node.role || (depth === 0 ? 'Root Coordinator' : 'Worker')}
              </span>
              <span class="font-medium text-white text-xs truncate max-w-sm" title="${node.title}">
                ${node.title}
              </span>
              ${isCurrent ? `<span class="px-1.5 py-0.2 rounded text-[10px] bg-emerald-500/20 text-emerald-300 font-mono border border-emerald-500/40">Active</span>` : ''}
            </div>
            <div class="flex items-center space-x-3 text-xs font-mono">
              <span class="text-gray-400">${node.steps} steps</span>
              <span class="text-indigo-400">${formatTokens(node.tokens)} tok</span>
              <span class="text-rose-400 font-bold">$${Number(node.cost || 0).toFixed(2)}</span>
              ${node.duration ? `<span class="text-emerald-400">${formatDuration(node.duration)}</span>` : ''}
              ${!isCurrent ? `
                <button onclick="inspectSession('${node.id}')" class="px-2 py-0.5 rounded bg-brand-600/80 hover:bg-brand-600 text-[10px] text-white font-sans transition">
                  Inspect ➔
                </button>
              ` : ''}
            </div>
          </div>
        `;
        treeEl.appendChild(nodeCard);

        if (node.children && node.children.length > 0) {
          node.children.forEach((child, i) => {
            renderTreeNode(child, depth + 1, i === node.children.length - 1);
          });
        }
      }

      if (data.fleet_tree) {
        renderTreeNode(data.fleet_tree, 0, true);
      } else if (data.children && data.children.length > 0) {
        renderTreeNode({
          id: conv.id,
          title: conv.title,
          role: conv.subagent_role || 'Root Coordinator',
          depth: 0,
          steps: conv.total_steps || 0,
          tokens: conv.tokens || conv.total_tokens || 0,
          cost: conv.billed_cost || 0,
          duration: conv.duration_seconds || 0,
          is_current: true,
          children: data.children.map(ch => ({
            id: ch.id,
            title: ch.title,
            role: ch.role,
            depth: ch.depth || 1,
            steps: ch.steps,
            tokens: ch.tokens,
            cost: ch.cost,
            duration: 0,
            is_current: false,
            children: []
          }))
        }, 0, true);
      } else {
        treeEl.innerHTML = `
          <div class="text-center py-6 text-gray-500 font-mono text-xs">
            This session operated as a standalone agent with no delegated sub-agents.
          </div>
        `;
      }
      lucide.createIcons();
    }

    function populateDiffSelectors(steps) {
      const selectA = document.getElementById('diffSelectA');
      const selectB = document.getElementById('diffSelectB');
      if (!selectA || !selectB) return;
      selectA.innerHTML = '';
      selectB.innerHTML = '';

      if (!steps || steps.length === 0) return;

      for (let i = 0; i < steps.length; i++) {
        const s = steps[i];
        const label = `Step #${s.step_index}: ${s.activity || 'Step'} (${formatTokens(s.est_tokens)} tok)`;
        const optA = document.createElement('option');
        optA.value = s.step_index;
        optA.textContent = label;
        selectA.appendChild(optA);

        const optB = document.createElement('option');
        optB.value = s.step_index;
        optB.textContent = label;
        selectB.appendChild(optB);
      }

      if (steps.length > 1) {
        selectA.value = steps[steps.length - 2].step_index;
        selectB.value = steps[steps.length - 1].step_index;
      } else {
        selectA.value = steps[0].step_index;
        selectB.value = steps[0].step_index;
      }
    }

    function setDiffPreset(preset) {
      if (!currentSessionData || !currentSessionData.steps || currentSessionData.steps.length === 0) return;
      const steps = currentSessionData.steps;
      const selectA = document.getElementById('diffSelectA');
      const selectB = document.getElementById('diffSelectB');
      if (!selectA || !selectB) return;

      if (preset === 'prev') {
        const curBIdx = selectB.selectedIndex;
        if (curBIdx > 0) {
          selectA.selectedIndex = curBIdx - 1;
        } else if (steps.length > 1) {
          selectA.selectedIndex = steps.length - 2;
          selectB.selectedIndex = steps.length - 1;
        }
      } else if (preset === 'first_to_last') {
        selectA.selectedIndex = 0;
        selectB.selectedIndex = steps.length - 1;
      }
      onDiffStepChange();
    }

    async function onDiffStepChange() {
      const selectA = document.getElementById('diffSelectA');
      const selectB = document.getElementById('diffSelectB');
      const toggleCumulative = document.getElementById('diffCumulativeToggle');
      if (!selectA || !selectB || !currentSessionData) return;

      const stepA = parseInt(selectA.value, 10);
      const stepB = parseInt(selectB.value, 10);
      const cumulative = toggleCumulative ? toggleCumulative.checked : true;

      await loadTurnDiff(currentSessionData.conversation.id, stepA, stepB, cumulative);
    }

    function computeLocalTurnDiff(steps, stepA, stepB, cumulative) {
      let blocksA = [];
      let blocksB = [];

      for (const s of steps) {
        const sBlocks = s.blocks || [];
        if (cumulative) {
          if (s.step_index <= stepA) blocksA.push(...sBlocks);
          if (s.step_index <= stepB) blocksB.push(...sBlocks);
        } else {
          if (s.step_index === stepA) blocksA.push(...sBlocks);
          if (s.step_index === stepB) blocksB.push(...sBlocks);
        }
      }

      function norm(b) {
        const kind = b.block_type || b.type || b.kind || 'generic';
        const content = b.content || b.text || '';
        const hash = b.block_hash || b.hash || `${kind}:${content.substring(0, 100)}`;
        const tokens = b.est_tokens || Math.max(1, Math.round(content.length / 3.8));
        return {
          kind,
          block_type: kind,
          content,
          hash,
          step_index: b.step_index || 0,
          call_id: b.call_id,
          tool_name: b.tool_name,
          est_tokens: tokens
        };
      }

      const normA = blocksA.map(norm);
      const normB = blocksB.map(norm);

      const aKeys = new Set(normA.map(x => x.hash));
      const bKeys = new Set(normB.map(x => x.hash));

      const added = normB.filter(x => !aKeys.has(x.hash));
      const removed = normA.filter(x => !bKeys.has(x.hash));
      const common = normB.filter(x => aKeys.has(x.hash));

      const addedTokens = added.reduce((sum, x) => sum + x.est_tokens, 0);
      const removedTokens = removed.reduce((sum, x) => sum + x.est_tokens, 0);
      const commonTokens = common.reduce((sum, x) => sum + x.est_tokens, 0);

      const stepAObj = steps.find(s => s.step_index === stepA) || { step_index: stepA };
      const stepBObj = steps.find(s => s.step_index === stepB) || { step_index: stepB };

      return {
        conversation_id: currentSessionData.conversation.id,
        step_a: stepAObj,
        step_b: stepBObj,
        cumulative: cumulative,
        added: added,
        removed: removed,
        common: common,
        stats: {
          added_count: added.length,
          removed_count: removed.length,
          common_count: common.length,
          added_tokens: addedTokens,
          removed_tokens: removedTokens,
          common_tokens: commonTokens,
          net_token_delta: addedTokens - removedTokens,
          compaction_detected: removed.length > 0,
          pruned_step_indices: [...new Set(removed.map(x => x.step_index))],
          pruned_kinds: {},
          summary: `+${added.length} blocks (~${addedTokens} tok)`
        }
      };
    }

    async function loadTurnDiff(cid, stepA, stepB, cumulative) {
      try {
        let diffData = null;
        if (typeof IS_STATIC_EXPORT !== 'undefined' && IS_STATIC_EXPORT) {
          diffData = computeLocalTurnDiff(currentSessionData.steps, stepA, stepB, cumulative);
        } else {
          const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
          const res = await fetch(`/api/turn-diff?conv_id=${encodeURIComponent(cid)}&a=${stepA}&b=${stepB}&cumulative=${cumulative}&tz=${encodeURIComponent(clientTz)}`);
          if (res.ok) {
            diffData = await res.json();
          } else {
            diffData = computeLocalTurnDiff(currentSessionData.steps, stepA, stepB, cumulative);
          }
        }
        renderTurnDiff(diffData);
      } catch (e) {
        console.error("Failed to fetch turn diff", e);
        if (currentSessionData && currentSessionData.steps) {
          const fallback = computeLocalTurnDiff(currentSessionData.steps, stepA, stepB, cumulative);
          renderTurnDiff(fallback);
        }
      }
    }

    function renderDiffBlock(block, mode) {
      const isAdded = mode === 'added';
      const isRemoved = mode === 'removed';
      const kind = block.block_type || block.kind || 'generic';
      const content = block.content || block.text || '';
      const tokens = block.est_tokens || 0;
      const stepIdx = block.step_index || 0;

      let borderCls = 'border-l-4 border-l-gray-600 bg-dark-900/30';
      let badgeCls = 'bg-gray-500/20 text-gray-300 border-gray-500/40';
      let signLabel = '= Retained';

      if (isAdded) {
        borderCls = 'border-l-4 border-l-emerald-500 bg-emerald-950/20';
        badgeCls = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40';
        signLabel = '+ Added';
      } else if (isRemoved) {
        borderCls = 'border-l-4 border-l-rose-500 bg-rose-950/20';
        badgeCls = 'bg-rose-500/20 text-rose-300 border-rose-500/40';
        signLabel = '- Evicted';
      }

      let kindIcon = 'file-text';
      let kindTitle = kind.toUpperCase();
      if (kind === 'user') { kindIcon = 'user'; kindTitle = 'User Prompt'; }
      else if (kind === 'assistant') { kindIcon = 'message-square'; kindTitle = 'Assistant Response'; }
      else if (kind === 'thinking') { kindIcon = 'brain-circuit'; kindTitle = 'Model Reasoning'; }
      else if (kind === 'tool_use') { kindIcon = 'wrench'; kindTitle = `Tool Call (${block.name || block.tool_name || 'Tool'})`; }
      else if (kind === 'tool_result') { kindIcon = 'check-square'; kindTitle = 'Tool Output'; }
      else if (kind === 'system') { kindIcon = 'cpu'; kindTitle = 'System Context'; }

      return `
        <div class="flex flex-col bg-dark-800 rounded-lg border border-dark-700 overflow-hidden ${borderCls} p-3.5">
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase font-mono border ${badgeCls}">${signLabel}</span>
              <div class="flex items-center space-x-1.5 text-xs font-semibold text-gray-200">
                <i data-lucide="${kindIcon}" class="w-3.5 h-3.5 opacity-80"></i>
                <span>${kindTitle}</span>
              </div>
            </div>
            <div class="flex items-center space-x-2 text-[10px] font-mono">
              <span class="text-gray-400 font-bold">~${formatTokens(tokens)} tok</span>
              <span class="text-gray-500">Step #${stepIdx}</span>
            </div>
          </div>
          ${renderCollapsibleBody(content)}
        </div>
      `;
    }

    function renderTurnDiff(diffData) {
      if (!diffData) return;
      const stats = diffData.stats || {};

      // 1. Update KPI numbers
      document.getElementById('diffKpiAdded').textContent = `+${formatTokens(stats.added_tokens || 0)} tok`;
      document.getElementById('diffKpiAddedSub').textContent = `${(stats.added_count || 0).toLocaleString()} block${stats.added_count === 1 ? '' : 's'}`;

      document.getElementById('diffKpiRemoved').textContent = `-${formatTokens(stats.removed_tokens || 0)} tok`;
      document.getElementById('diffKpiRemovedSub').textContent = `${(stats.removed_count || 0).toLocaleString()} block${stats.removed_count === 1 ? '' : 's'}`;

      const delta = stats.net_token_delta || 0;
      const deltaEl = document.getElementById('diffKpiDelta');
      deltaEl.textContent = `${delta >= 0 ? '+' : ''}${formatTokens(delta)} tok`;
      deltaEl.className = delta > 0 ? 'font-bold text-emerald-400 font-mono mt-0.5 block text-sm' : (delta < 0 ? 'font-bold text-rose-400 font-mono mt-0.5 block text-sm' : 'font-bold text-indigo-300 font-mono mt-0.5 block text-sm');

      const compactionEl = document.getElementById('diffKpiCompaction');
      const compactionAlert = document.getElementById('diffCompactionAlert');
      const compactionDetail = document.getElementById('diffCompactionDetail');

      if (stats.compaction_detected) {
        compactionEl.textContent = `⚠️ Compaction (${stats.removed_count} pruned)`;
        compactionEl.className = 'font-bold text-amber-400 text-xs mt-0.5';
        if (compactionAlert) {
          compactionAlert.classList.remove('hidden');
          const prunedSteps = (stats.pruned_step_indices || []).join(', ') || 'earlier turns';
          compactionDetail.textContent = `Context Compaction / Truncation Boundary Detected: Older history blocks from step(s) ${prunedSteps} (~${formatTokens(stats.removed_tokens)} tokens) were evicted from the prompt window.`;
        }
      } else {
        compactionEl.textContent = '✓ Clean (0 pruned)';
        compactionEl.className = 'font-bold text-emerald-400 text-xs mt-0.5';
        if (compactionAlert) {
          compactionAlert.classList.add('hidden');
        }
      }

      // 2. Render diff stream
      const container = document.getElementById('diffStreamContainer');
      container.innerHTML = '';

      const added = diffData.added || [];
      const removed = diffData.removed || [];
      const common = diffData.common || [];

      if (added.length === 0 && removed.length === 0 && common.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-500 italic p-6 text-center">No blocks found between Turn A and Turn B.</div>';
        lucide.createIcons();
        return;
      }

      // Evicted blocks section
      if (removed.length > 0) {
        const header = document.createElement('div');
        header.className = 'text-xs font-bold text-rose-400 uppercase tracking-wider flex items-center space-x-1.5 pt-1';
        header.innerHTML = `<i data-lucide="minus-circle" class="w-3.5 h-3.5"></i><span>Evicted / Pruned from Context (${removed.length} blocks • ~${formatTokens(stats.removed_tokens)} tok)</span>`;
        container.appendChild(header);

        for (const b of removed) {
          const el = document.createElement('div');
          el.innerHTML = renderDiffBlock(b, 'removed');
          container.appendChild(el.firstElementChild);
        }
      }

      // Added blocks section
      if (added.length > 0) {
        const header = document.createElement('div');
        header.className = 'text-xs font-bold text-emerald-400 uppercase tracking-wider flex items-center space-x-1.5 pt-2';
        header.innerHTML = `<i data-lucide="plus-circle" class="w-3.5 h-3.5"></i><span>Added to Context (${added.length} blocks • ~${formatTokens(stats.added_tokens)} tok)</span>`;
        container.appendChild(header);

        for (const b of added) {
          const el = document.createElement('div');
          el.innerHTML = renderDiffBlock(b, 'added');
          container.appendChild(el.firstElementChild);
        }
      }

      // Retained / Common blocks section (collapsible)
      if (common.length > 0) {
        const details = document.createElement('details');
        details.className = 'group bg-dark-900/60 rounded-lg border border-dark-700/60 p-3 mt-2';
        
        let commonBlocksHtml = '';
        for (const b of common) {
          commonBlocksHtml += renderDiffBlock(b, 'common');
        }

        details.innerHTML = `
          <summary class="text-xs text-gray-400 font-semibold cursor-pointer select-none hover:text-gray-200 transition flex items-center justify-between">
            <div class="flex items-center space-x-1.5">
              <span class="inline-block transition group-open:rotate-90">▸</span>
              <span>= Retained Context (${common.length} blocks • ~${formatTokens(stats.common_tokens)} tok)</span>
            </div>
            <span class="text-[10px] text-gray-500 font-mono">click to expand</span>
          </summary>
          <div class="mt-3 space-y-2 pt-2 border-t border-dark-800">
            ${commonBlocksHtml}
          </div>
        `;
        container.appendChild(details);
      }

      lucide.createIcons();
    }

    function hashCode(str) {
      let hash = 0;
      if (!str || str.length === 0) return hash;
      for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;
      }
      return hash;
    }

    function getColorFromCallId(callId) {
      const colors = ['#f87171', '#fb923c', '#fbbf24', '#a3e635', '#34d399', '#22d3ee', '#38bdf8', '#818cf8', '#a78bfa', '#e879f9', '#f43f5e'];
      if (!callId) return '#6b7280';
      const hash = Math.abs(hashCode(callId));
      return colors[hash % colors.length];
    }

    function renderCollapsibleBody(content) {
      if (!content) return '';
      let formatted = content;
      try {
        const trimmed = content.trim();
        if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
          formatted = JSON.stringify(JSON.parse(trimmed), null, 2);
        }
      } catch (e) {}

      const lines = formatted.split('\n');
      const escapedContent = formatted.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

      if (lines.length > 15 || formatted.length > 800) {
        return `
          <details class="group mt-2">
            <summary class="text-[10px] text-gray-400 cursor-pointer select-none hover:text-gray-200 transition font-mono flex items-center space-x-1 py-1">
              <span class="inline-block transition group-open:rotate-90">▸</span>
              <span>show ${lines.length} lines (${Math.round(formatted.length / 1024 * 10) / 10} KB)</span>
            </summary>
            <pre class="mt-1 text-[10px] text-gray-300 bg-dark-900/80 p-2.5 rounded border border-dark-700/60 whitespace-pre-wrap break-all overflow-x-auto max-h-[400px] overflow-y-auto font-mono leading-relaxed">${escapedContent}</pre>
          </details>
        `;
      } else {
        return `<pre class="mt-2 text-[10px] text-gray-300 bg-dark-900/80 p-2.5 rounded border border-dark-700/60 whitespace-pre-wrap break-all font-mono leading-relaxed">${escapedContent}</pre>`;
      }
    }

    function renderFlowSequence(steps) {
      const container = document.getElementById('modalFlowSequence');
      container.innerHTML = '';

      if (!steps || steps.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-500 italic p-4 text-center">No steps found for this session.</div>';
        return;
      }

      let subagentCidMap = {};

      for (const step of steps) {
        const blocks = (step.blocks && step.blocks.length > 0) ? step.blocks : [];

        // Fallback for steps without parsed transcript blocks
        if (blocks.length === 0) {
          const row = document.createElement('div');
          row.className = 'flex flex-col bg-dark-800 rounded-lg border border-dark-700 p-3.5 border-l-4 border-l-gray-600';
          row.innerHTML = `
            <div class="flex items-center justify-between">
              <div class="flex items-center space-x-2 text-xs font-semibold text-gray-200">
                <i data-lucide="layers" class="w-3.5 h-3.5 opacity-70"></i>
                <span>${step.activity || 'Step'}</span>
                <span class="text-gray-400 font-mono text-[11px] font-normal">(${step.tool_or_source || step.source || ''})</span>
              </div>
              <div class="text-[10px] text-gray-500 font-mono">Step #${step.step_index}</div>
            </div>
            ${step.detail ? `<div class="mt-1 text-[11px] text-gray-300 font-mono">${step.detail}</div>` : ''}
          `;
          container.appendChild(row);
          continue;
        }

        for (const b of blocks) {
          const blockType = b.block_type || b.type;
          const callId = b.call_id;
          const content = b.content || '';
          const colorHex = b.color || getColorFromCallId(callId);

          const row = document.createElement('div');
          row.className = 'flex flex-col bg-dark-800 rounded-lg border border-dark-700 overflow-hidden';

          if (blockType === 'user' || blockType === 'system') {
            const isUser = blockType === 'user';
            const title = isUser ? 'User Prompt' : 'System';
            const icon = isUser ? 'user' : 'cpu';
            const headerColor = isUser ? 'border-l-blue-500' : 'border-l-gray-600';
            const bgColor = isUser ? 'bg-blue-950/20' : 'bg-dark-900/30';

            row.innerHTML = `
              <div class="p-3.5 border-l-4 ${headerColor} ${bgColor}">
                <div class="flex items-center justify-between">
                  <div class="flex items-center space-x-2 text-xs font-semibold text-gray-200">
                    <i data-lucide="${icon}" class="w-3.5 h-3.5 text-blue-400"></i>
                    <span>${title}</span>
                  </div>
                  <div class="text-[10px] text-gray-500 font-mono">Step #${step.step_index}</div>
                </div>
                ${renderCollapsibleBody(content)}
              </div>
            `;
            container.appendChild(row);
            continue;
          }

          if (blockType === 'thinking') {
            row.innerHTML = `
              <div class="p-3.5 border-l-4 border-l-purple-500 bg-purple-950/15">
                <div class="flex items-center justify-between">
                  <div class="flex items-center space-x-2 text-xs font-semibold text-purple-300">
                    <i data-lucide="brain-circuit" class="w-3.5 h-3.5 text-purple-400"></i>
                    <span>Model Reasoning (Thinking)</span>
                  </div>
                  <div class="text-[10px] text-gray-500 font-mono">Step #${step.step_index}</div>
                </div>
                ${renderCollapsibleBody(content)}
              </div>
            `;
            container.appendChild(row);
            continue;
          }

          if (blockType === 'assistant') {
            row.innerHTML = `
              <div class="p-3.5 border-l-4 border-l-indigo-500 bg-indigo-950/15">
                <div class="flex items-center justify-between">
                  <div class="flex items-center space-x-2 text-xs font-semibold text-indigo-200">
                    <i data-lucide="message-square" class="w-3.5 h-3.5 text-indigo-400"></i>
                    <span>Assistant Response</span>
                  </div>
                  <div class="text-[10px] text-gray-500 font-mono">Step #${step.step_index}</div>
                </div>
                ${renderCollapsibleBody(content)}
              </div>
            `;
            container.appendChild(row);
            continue;
          }

          if (blockType === 'tool_use') {
            row.style.borderLeft = `4px solid ${colorHex}`;
            const toolName = b.tool_name || 'Tool Call';
            const isSubagent = b.is_subagent || toolName === 'invoke_subagent';
            const isSkill = b.is_skill || Boolean(b.skill_name);
            const isMcp = Boolean(b.mcp_server) || toolName.startsWith('mcp_') || toolName === 'call_mcp_tool';

            let badgesHtml = '';
            if (isSubagent) {
              badgesHtml += `<span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-pink-950/80 text-pink-300 border border-pink-500/40 flex items-center space-x-1"><i data-lucide="bot" class="w-3 h-3"></i><span>Sub-Agent</span></span>`;
            }
            if (isSkill) {
              badgesHtml += `<span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-purple-950/80 text-purple-300 border border-purple-500/40 flex items-center space-x-1"><i data-lucide="sparkles" class="w-3 h-3"></i><span>Skill: ${b.skill_name || 'Skill'}</span></span>`;
            }
            if (isMcp) {
              const serverName = b.mcp_server || toolName.replace('mcp_', '');
              badgesHtml += `<span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-cyan-950/80 text-cyan-300 border border-cyan-500/40 font-mono flex items-center space-x-1"><i data-lucide="plug" class="w-3 h-3"></i><span>MCP: ${serverName}</span></span>`;
            }

            // Sub-agent task cards isolation
            let subagentCardHtml = '';
            if (isSubagent && b.subagents && b.subagents.length > 0) {
              for (const sa of b.subagents) {
                const saRole = sa.Role || sa.role || 'Autonomous Worker';
                const saModel = sa.Model || sa.model || 'inherit';
                const saType = sa.TypeName || sa.type || 'self';
                const saPrompt = sa.Prompt || sa.prompt || '';
                const promptLines = saPrompt ? saPrompt.split('\n').length : 0;

                subagentCardHtml += `
                  <div class="mt-3 p-3 rounded-lg bg-dark-900/90 border border-pink-500/30 space-y-2">
                    <div class="flex items-center justify-between">
                      <div class="flex items-center space-x-2">
                        <span class="px-2 py-0.5 rounded bg-pink-500/20 text-pink-300 font-semibold text-xs font-mono flex items-center space-x-1">
                          <i data-lucide="bot" class="w-3 h-3"></i>
                          <span>${saRole}</span>
                        </span>
                        <span class="px-1.5 py-0.5 rounded bg-dark-800 text-gray-300 text-[10px] font-mono border border-dark-700">model: ${saModel}</span>
                        <span class="px-1.5 py-0.5 rounded bg-dark-800 text-gray-400 text-[10px] font-mono border border-dark-700">type: ${saType}</span>
                      </div>
                      <div class="flex items-center space-x-2">
                        ${(() => {
                          if (currentSessionData && currentSessionData.children) {
                            const matchedChild = currentSessionData.children.find(ch => 
                              (ch.step_index === step.step_index) || 
                              (ch.role && saRole && ch.role.toLowerCase() === saRole.toLowerCase())
                            );
                            if (matchedChild) {
                              return `<button onclick="inspectSession('${matchedChild.id}')" class="px-2 py-0.5 rounded bg-pink-600/80 hover:bg-pink-600 text-white font-mono text-[10px] transition flex items-center space-x-1"><i data-lucide="external-link" class="w-3 h-3"></i><span>Inspect Flow (${matchedChild.steps} ops)</span></button>`;
                            }
                          }
                          return '';
                        })()}
                        <span class="text-[10px] text-emerald-400 font-mono font-medium flex items-center space-x-1">
                          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                          <span>Dispatched</span>
                        </span>
                      </div>
                    </div>
                    ${saPrompt ? `
                      <details class="group">
                        <summary class="text-[10px] text-pink-300/80 cursor-pointer select-none hover:text-pink-200 transition font-mono flex items-center space-x-1">
                          <span class="inline-block transition group-open:rotate-90">▸</span>
                          <span>show task prompt (${promptLines} lines)</span>
                        </summary>
                        <pre class="mt-1 text-[10px] text-gray-300 bg-dark-950/80 p-2 rounded border border-dark-800 whitespace-pre-wrap break-all font-mono max-h-[300px] overflow-y-auto">${saPrompt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>
                      </details>
                    ` : ''}
                  </div>
                `;
              }
            }

            row.innerHTML = `
              <div class="p-3.5 bg-amber-950/10">
                <div class="flex items-center justify-between">
                  <div class="flex items-center space-x-2.5">
                    <span class="px-2 py-0.5 rounded text-xs font-mono font-bold border flex items-center space-x-1.5" style="background-color: ${colorHex}20; color: ${colorHex}; border-color: ${colorHex}50;">
                      <i data-lucide="wrench" class="w-3.5 h-3.5"></i>
                      <span>${toolName}</span>
                    </span>
                    ${badgesHtml}
                  </div>
                  <div class="flex items-center space-x-2 text-[10px] text-gray-500 font-mono">
                    ${callId ? `<span class="px-1.5 py-0.5 rounded bg-dark-900 border border-dark-700 text-gray-400">id: ${callId}</span>` : ''}
                    <span>Step #${step.step_index}</span>
                  </div>
                </div>
                ${subagentCardHtml ? subagentCardHtml : renderCollapsibleBody(content)}
              </div>
            `;
            container.appendChild(row);
            continue;
          }

          if (blockType === 'tool_result') {
            row.style.borderLeft = `4px solid ${colorHex}`;
            const pairedName = b.paired_tool_name || 'Tool';
            const isSubagent = b.is_subagent || pairedName === 'invoke_subagent';
            const subagentCid = b.subagent_conversation_id;

            let inspectSubagentBtn = '';
            if (subagentCid) {
              inspectSubagentBtn = `
                <button onclick="inspectSession('${subagentCid}')" class="px-2.5 py-1 rounded bg-pink-600 hover:bg-pink-500 text-white font-medium text-[11px] transition shadow-md shadow-pink-900/30 flex items-center space-x-1.5">
                  <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
                  <span>Inspect Sub-Agent Flow</span>
                </button>
              `;
            }

            row.innerHTML = `
              <div class="p-3.5 bg-emerald-950/10">
                <div class="flex items-center justify-between">
                  <div class="flex items-center space-x-2.5">
                    <span class="px-2 py-0.5 rounded text-xs font-mono font-bold border flex items-center space-x-1.5" style="background-color: ${colorHex}20; color: ${colorHex}; border-color: ${colorHex}50;">
                      <i data-lucide="check-square" class="w-3.5 h-3.5"></i>
                      <span>${pairedName} Result</span>
                    </span>
                    ${isSubagent ? `<span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-pink-950/80 text-pink-300 border border-pink-500/40 font-mono">Sub-Agent Output</span>` : ''}
                    ${inspectSubagentBtn}
                  </div>
                  <div class="flex items-center space-x-2 text-[10px] text-gray-500 font-mono">
                    ${callId ? `<span class="px-1.5 py-0.5 rounded bg-dark-900 border border-dark-700 text-gray-400">id: ${callId}</span>` : ''}
                    <span>Step #${step.step_index}</span>
                  </div>
                </div>
                ${renderCollapsibleBody(content)}
              </div>
            `;
            container.appendChild(row);
            continue;
          }

          // Other block types
          row.innerHTML = `
            <div class="p-3.5 border-l-4 border-l-gray-600 bg-dark-900/30">
              <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2 text-xs font-semibold text-gray-200">
                  <i data-lucide="circle" class="w-3.5 h-3.5 opacity-70"></i>
                  <span>${blockType}</span>
                </div>
                <div class="text-[10px] text-gray-500 font-mono">Step #${step.step_index}</div>
              </div>
              ${renderCollapsibleBody(content)}
            </div>
          `;
          container.appendChild(row);
        }
      }
      lucide.createIcons();
    }

    function closeModal() {
      const modal = document.getElementById('sessionModal');
      if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
      }
    }

    function initLiveSyncSSE() {
      let evtSource = null;

      function connect() {
        evtSource = new EventSource('/api/events');

        evtSource.addEventListener('connected', (event) => {
          const badge = document.getElementById('liveSyncBadge');
          const text = document.getElementById('syncText');
          if (badge) badge.className = 'px-3 py-1.5 rounded-md bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 text-xs font-medium flex items-center space-x-1.5 transition select-none';
          if (text) text.textContent = 'Live Sync: SSE Active';
          try {
            if (event.data) {
              const d = JSON.parse(event.data);
              if (d && d.quota) renderQuotaSection(d.quota);
            }
          } catch (e) {}
        });

        evtSource.addEventListener('db_update', (event) => {
          console.log("⚡ SQLite change detected via SSE. Refreshing UI...");
          try {
            if (event.data) {
              const d = JSON.parse(event.data);
              if (d && d.quota) renderQuotaSection(d.quota);
            }
          } catch (e) {}
          fetchDashboardData();
        });

        evtSource.addEventListener('quota_update', (event) => {
          try {
            if (event.data) {
              const d = JSON.parse(event.data);
              renderQuotaSection(d.quota || d);
            }
          } catch (e) {
            console.error("Failed to parse quota_update SSE event", e);
          }
        });

        evtSource.onerror = () => {
          const badge = document.getElementById('liveSyncBadge');
          const text = document.getElementById('syncText');
          if (badge) badge.className = 'px-3 py-1.5 rounded-md bg-amber-950/60 border border-amber-500/30 text-amber-300 text-xs font-medium flex items-center space-x-1.5 transition select-none';
          if (text) text.textContent = 'Live Sync: Reconnecting...';
          try { evtSource.close(); } catch (e) {}
          setTimeout(connect, 3000);
        };
      }

      connect();
    }

    document.addEventListener('DOMContentLoaded', async () => {
      updateProjectFilterUI(currentProject);

      try {
        const projects = await fetch('/api/projects').then(r => r.json());
        const filterSelect = document.getElementById('projectFilter');
        if (filterSelect && Array.isArray(projects) && projects.length > 0) {
          projects.forEach(p => {
            if (!Array.from(filterSelect.options).some(o => o.value === p)) {
              const opt = document.createElement('option');
              opt.value = p;
              opt.textContent = p;
              filterSelect.appendChild(opt);
            }
          });
          filterSelect.value = currentProject;
          filterSelect.classList.remove('hidden');
        }
      } catch (e) {
        console.error('Failed to load projects', e);
      }
      fetchDashboardData();
      if (typeof IS_STATIC_EXPORT === "undefined" || !IS_STATIC_EXPORT) { initLiveSyncSSE(); }
    });