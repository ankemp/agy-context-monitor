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
    let toolsChart = null;
    let categoryChart = null;
    let phaseChart = null;
    let mcpServerChart = null;

    let allHotFiles = [];
    let allConversations = [];
    let allTimelineData = [];
    let allToolsData = [];
    let allMcpData = [];
    let currentTimelineDays = 30;
    let currentToolFilter = 'all';
    let currentHotFilesFilter = 'files';
    let cachedActivityMatrix = null;
    let currentHeatmapMode = 'year';
    let localTimezoneAbbr = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';

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

    async function fetchDashboardData() {
      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        const tzParam = clientTz ? `?tz=${encodeURIComponent(clientTz)}` : '';

        const [summaryRes, timelineRes, toolsRes, mcpRes, hotFilesRes, matrixRes, convsRes] = await Promise.all([
          fetch(`/api/summary${tzParam}`).then(r => r.json()),
          fetch(`/api/timeline${tzParam}`).then(r => r.json()),
          fetch('/api/tools').then(r => r.json()),
          fetch('/api/mcp').then(r => r.json()),
          fetch('/api/hot-files').then(r => r.json()),
          fetch(`/api/activity-matrix${tzParam}`).then(r => r.json()),
          fetch(`/api/conversations${tzParam}`).then(r => r.json())
        ]);

        allTimelineData = timelineRes || [];
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
        setTimelineRange(currentTimelineDays);
        renderModelsChart(summaryRes.models);
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

      const elThinking = document.getElementById('kpiThinking');
      if (elThinking) elThinking.textContent = formatTokens(data.total_thinking || 0);

      const elCodeLines = document.getElementById('kpiCodeLines');
      if (elCodeLines) elCodeLines.textContent = (data.total_code_lines || 0).toLocaleString();
      const elCodeSub = document.getElementById('kpiCodeSub');
      if (elCodeSub) elCodeSub.textContent = `+${(data.lines_written || 0).toLocaleString()} / ~${(data.lines_edited || 0).toLocaleString()}`;

      const elTimeSaved = document.getElementById('kpiTimeSaved');
      if (elTimeSaved) elTimeSaved.textContent = `~${(data.saved_hours || 0).toLocaleString()}h`;

      // Context Hygiene Diagnostics
      const elReReadRate = document.getElementById('diagReReadRate');
      if (elReReadRate) elReReadRate.textContent = `${data.re_read_rate || 0}%`;
      const elReReadBar = document.getElementById('diagReReadBar');
      if (elReReadBar) elReReadBar.style.width = `${Math.min(data.re_read_rate || 0, 100)}%`;
      const elReReadCount = document.getElementById('diagReReadCount');
      if (elReReadCount) elReReadCount.textContent = `${(data.total_re_reads || 0).toLocaleString()} repeated views`;
      const elReReadWaste = document.getElementById('diagReReadWaste');
      if (elReReadWaste) elReReadWaste.textContent = `~${formatTokens(data.re_read_waste_tokens || 0)} waste`;

      const elResilienceRate = document.getElementById('diagResilienceRate');
      if (elResilienceRate) elResilienceRate.textContent = `${data.resilience_rate || 100}%`;
      const elResilienceBar = document.getElementById('diagResilienceBar');
      if (elResilienceBar) elResilienceBar.style.width = `${Math.min(data.resilience_rate || 100, 100)}%`;
      const elErrorCount = document.getElementById('diagErrorCount');
      if (elErrorCount) elErrorCount.textContent = `${(data.failed_tool_calls || 0).toLocaleString()} errors resolved`;

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
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
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

          cell.addEventListener('mouseenter', () => { document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`; });
          cell.addEventListener('mouseleave', () => { document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations'; });
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
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
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
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
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
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
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
        active: 'px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition',
        inactive: 'px-2.5 py-1 rounded text-gray-400 hover:text-white transition'
      };

      if (btn7d) btn7d.className = days === 7 ? btnClasses.active : btnClasses.inactive;
      if (btn14d) btn14d.className = days === 14 ? btnClasses.active : btnClasses.inactive;
      if (btn30d) btn30d.className = days === 30 ? btnClasses.active : btnClasses.inactive;
      if (btnAll) btnAll.className = days === null ? btnClasses.active : btnClasses.inactive;
      
      const sliced = days ? allTimelineData.slice(-days) : allTimelineData;
      renderTimelineChart(sliced);
    }
    window.setTimelineRange = setTimelineRange;

    function renderTimelineChart(data) {
      const ctx = document.getElementById('timelineChart').getContext('2d');
      const labels = data.map(d => d.day);
      const tokens = data.map(d => d.tokens);
      const sessions = data.map(d => d.sessions);
      const costs = data.map(d => d.cost || 0);

      if (timelineChart) timelineChart.destroy();

      timelineChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Tokens Consumed',
              data: tokens,
              backgroundColor: 'rgba(99, 102, 241, 0.65)',
              borderColor: '#6366f1',
              borderWidth: 1,
              borderRadius: 4,
              yAxisID: 'y',
              order: 3
            },
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
              borderColor: '#38bdf8',
              backgroundColor: '#38bdf8',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 2,
              pointHoverRadius: 4,
              yAxisID: 'y1',
              order: 2
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#9ca3af', font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  if (ctx.dataset.label === 'Tokens Consumed') return `Tokens: ${formatTokens(ctx.raw)}`;
                  if (ctx.dataset.label === 'Est. API Value ($)') return `Est. API Value: $${Number(ctx.raw || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
                  return `Sessions: ${ctx.raw}`;
                }
              }
            }
          },
          scales: {
            x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', maxTicksLimit: 12, font: { size: 10 } } },
            y: {
              type: 'linear',
              position: 'left',
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', callback: v => formatTokens(v), font: { size: 10 } },
              title: { display: true, text: 'Tokens', color: '#6b7280', font: { size: 10 } }
            },
            y1: {
              type: 'linear',
              position: 'right',
              grid: { drawOnChartArea: false },
              ticks: { color: '#38bdf8', font: { size: 10 } },
              title: { display: true, text: 'Sessions', color: '#38bdf8', font: { size: 10 } }
            },
            yCost: {
              type: 'linear',
              position: 'right',
              grid: { drawOnChartArea: false },
              ticks: {
                color: '#f43f5e',
                callback: v => '$' + (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(v < 10 ? 2 : 0)),
                font: { size: 10 }
              },
              title: { display: true, text: 'Est. API Value', color: '#f43f5e', font: { size: 10 } }
            }
          }
        }
      });
    }

    function renderModelsChart(models) {
      const ctx = document.getElementById('modelsChart').getContext('2d');
      const labels = models.map(m => m.model_name);
      const data = models.map(m => m.tokens);
      const colors = ['#6366f1', '#a855f7', '#ec4899', '#3b82f6', '#10b981', '#f59e0b'];

      if (modelsChart) modelsChart.destroy();

      modelsChart = new Chart(ctx, {
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
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          cutout: '65%'
        }
      });
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
      const ctx = document.getElementById('toolsChart').getContext('2d');
      let filtered = tools || [];
      if (currentToolFilter === 'native') {
        filtered = filtered.filter(t => !t.is_mcp);
      } else if (currentToolFilter === 'mcp') {
        filtered = filtered.filter(t => t.is_mcp);
      }

      const top10 = filtered.slice(0, 10);
      const labels = top10.map(t => t.tool_name);
      const data = top10.map(t => t.total_calls);

      if (toolsChart) toolsChart.destroy();

      const barBg = currentToolFilter === 'mcp' ? 'rgba(6, 182, 212, 0.75)' : 'rgba(245, 158, 11, 0.75)';
      const barBorder = currentToolFilter === 'mcp' ? '#06b6d4' : '#f59e0b';

      toolsChart = new Chart(ctx, {
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
      const ctx = document.getElementById('mcpServerChart').getContext('2d');
      if (!allMcpData || allMcpData.length === 0) return;

      const labels = allMcpData.map(s => s.server);
      const data = allMcpData.map(s => s.calls);
      const colors = ['#06b6d4', '#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#a855f7', '#64748b'];

      if (mcpServerChart) mcpServerChart.destroy();

      mcpServerChart = new Chart(ctx, {
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
      const ctx = document.getElementById('categoryChart').getContext('2d');
      const top7 = categories.slice(0, 7);
      const labels = top7.map(c => c.category);
      const data = top7.map(c => c.tokens);
      const colors = ['#10b981', '#6366f1', '#3b82f6', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b'];

      if (categoryChart) categoryChart.destroy();

      categoryChart = new Chart(ctx, {
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
      const ctx = document.getElementById('phaseChart').getContext('2d');
      if (!phases || phases.length === 0) return;

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

      if (phaseChart) phaseChart.destroy();

      phaseChart = new Chart(ctx, {
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

        tr.innerHTML = `
          <td class="py-2.5 px-3 text-gray-400 whitespace-nowrap font-mono text-[11px]">${dateStr}</td>
          <td class="py-2.5 px-3 font-medium text-white max-w-sm truncate" title="${c.title}">${c.title}</td>
          <td class="py-2.5 px-3 text-purple-300 whitespace-nowrap">${c.model_name}</td>
          <td class="py-2.5 px-3 text-right text-gray-300">${c.total_steps}</td>
          <td class="py-2.5 px-3 text-right font-semibold text-indigo-400">${formatTokens(c.total_tokens)}</td>
          <td class="py-2.5 px-3 text-right text-gray-300 whitespace-nowrap" title="${timeTitle}">
            <span class="font-medium text-emerald-400">${formatDuration(activeSec)}</span>
            <span class="text-[10px] text-gray-500 block">(${formatDuration(wallSec)})</span>
          </td>
          <td class="py-2.5 px-3 text-center">
            ${(typeof IS_STATIC_EXPORT !== "undefined" && IS_STATIC_EXPORT) ? "" : `<button onclick="inspectSession('${c.id}')" class="px-2.5 py-1 rounded bg-brand-600/80 hover:bg-brand-600 text-[11px] font-medium text-white transition">Inspect</button>`}
          </td>
        `;
        tbody.appendChild(tr);
      }
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
        document.getElementById('modalModel').textContent = modelLabel;
        
        const activeSec = conv.active_agent_seconds || conv.active_pairing_seconds || conv.duration_seconds || 0;
        document.getElementById('modalDuration').textContent = formatDuration(activeSec);
        const modalWallSpanEl = document.getElementById('modalWallSpan');
        if (modalWallSpanEl) {
          modalWallSpanEl.textContent = `Span: ${formatDuration(conv.duration_seconds || 0)}`;
        }

        // Populate Scorecard
        const toolsCnt = conv.tool_calls_count || 0;
        const promptsCnt = conv.user_prompts || 1;
        const autonomyRatio = (toolsCnt / Math.max(1, promptsCnt)).toFixed(1);
        const elAutonomy = document.getElementById('modalAutonomy');
        if (elAutonomy) elAutonomy.textContent = `${autonomyRatio}x (${toolsCnt} tools)`;

        const elThinking = document.getElementById('modalThinking');
        if (elThinking) elThinking.textContent = formatTokens(conv.thinking_tokens || 0);

        const elReReads = document.getElementById('modalReReads');
        const reReads = conv.redundant_file_reads || 0;
        if (elReReads) {
          elReReads.textContent = reReads === 0 ? '✓ 0 re-reads' : `⚠️ ${reReads} repeated`;
          elReReads.className = reReads === 0 ? 'font-semibold text-emerald-400 font-mono mt-0.5 block' : 'font-semibold text-amber-400 font-mono mt-0.5 block';
        }

        const elCodeLines = document.getElementById('modalCodeLines');
        if (elCodeLines) elCodeLines.textContent = `+${(conv.lines_written || 0).toLocaleString()} / ~${(conv.lines_edited || 0).toLocaleString()}`;

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
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-2 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-2 px-2.5 font-medium text-white">${s.activity}</td>
            <td class="py-2 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-2 px-2.5 text-right font-semibold text-white">${formatTokens(s.est_tokens)}</td>
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
              <td colspan="5" class="py-1 px-2.5 text-center text-xs font-semibold text-brand-300">
                🔄 Model Switched to: <span class="text-white">${s.model_name}</span>
              </td>
            `;
            sBody.appendChild(switchTr);
          }

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-1.5 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-1.5 px-2.5 text-gray-300">${s.activity}</td>
            <td class="py-1.5 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-1.5 px-2.5 text-right text-gray-400">${formatTokens(s.est_tokens)}</td>
            <td class="py-1.5 px-2.5 text-gray-400 max-w-md truncate" title="${s.detail || ''}">${s.detail || '--'}</td>
          `;
          sBody.appendChild(tr);
        }

        renderFlowSequence(data.steps);
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
      const tableContainer = document.getElementById('sessionViewTable');
      const flowContainer = document.getElementById('sessionViewFlow');

      const activeClass = 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap';
      const inactiveClass = 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap';

      if (mode === 'table') {
        btnTable.className = activeClass;
        btnFlow.className = inactiveClass;
        tableContainer.classList.remove('hidden');
        flowContainer.classList.add('hidden');
      } else {
        btnFlow.className = activeClass;
        btnTable.className = inactiveClass;
        flowContainer.classList.remove('hidden');
        tableContainer.classList.add('hidden');
      }
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
                      <span class="text-[10px] text-emerald-400 font-mono font-medium flex items-center space-x-1">
                        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                        <span>Dispatched</span>
                      </span>
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

        evtSource.addEventListener('connected', () => {
          const badge = document.getElementById('liveSyncBadge');
          const text = document.getElementById('syncText');
          if (badge) badge.className = 'px-3 py-1.5 rounded-md bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 text-xs font-medium flex items-center space-x-1.5 transition select-none';
          if (text) text.textContent = 'Live Sync: SSE Active';
        });

        evtSource.addEventListener('db_update', (event) => {
          console.log("⚡ SQLite change detected via SSE. Refreshing UI...");
          fetchDashboardData();
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

    document.addEventListener('DOMContentLoaded', () => {
      fetchDashboardData();
      if (typeof IS_STATIC_EXPORT === "undefined" || !IS_STATIC_EXPORT) { initLiveSyncSSE(); }
    });