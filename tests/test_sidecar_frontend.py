from pathlib import Path


def test_frontend_files_exist_and_invariants():
    html_path = Path("sidecars/panel/index.html")
    css_path = Path("sidecars/panel/styles.css")
    js_path = Path("sidecars/panel/app.js")

    assert html_path.exists(), "sidecars/panel/index.html must exist"
    assert css_path.exists(), "sidecars/panel/styles.css must exist"
    assert js_path.exists(), "sidecars/panel/app.js must exist"

    html = html_path.read_text()
    css = css_path.read_text()
    js = js_path.read_text()

    # Index HTML checks
    assert '<script src="/preload.js"></script>' in html or 'src="/preload.js"' in html
    assert 'href="/styles.css"' in html
    assert 'src="/app.js"' in html
    assert 'id="capacity-bar"' in html
    assert 'id="compaction-banner"' in html
    assert 'id="global-card"' in html
    assert 'id="quota-card"' in html
    assert 'id="quota-weekly-pct"' in html
    assert 'id="quota-5h-pct"' in html
    assert 'id="quota-session-impact"' in html
    assert 'id="quota-weekly-bar"' in html
    assert 'id="quota-5h-bar"' in html
    assert 'id="quota-tab-gemini"' in html
    assert 'id="quota-tab-3p"' in html
    assert 'id="inspect-banner"' in html
    assert 'id="inspect-banner-label"' in html
    assert 'id="btn-reset-inspect"' in html
    assert 'id="fleet-card"' in html
    assert 'id="fleet-title"' in html
    assert 'id="fleet-count-badge"' in html
    assert 'id="fleet-summary-strip"' in html
    assert 'id="fleet-total-cost"' in html
    assert 'id="fleet-total-tokens"' in html
    assert 'id="fleet-shielding"' in html
    assert 'id="fleet-node-list"' in html

    # Styles CSS checks: Theme tokens & static visual invariant
    assert "var(--background" in css
    assert "var(--card" in css
    assert "var(--foreground" in css
    assert "--vscode-" not in css, "Must not use --vscode-* variables"
    assert "transition: none !important" in css, "Must enforce static visuals invariant"
    assert "animation: none !important" in css, "Must enforce static visuals invariant"
    assert ".bar-segment" in css, "Must support multi-segment capacity bar styling"
    assert ".tab-btn" in css, "Must support quota model family tab button styling"
    assert ".quota-bar" in css, "Must support quota bar styling"
    assert ".cost-quota-group" in css, (
        "Must support unified cost-quota badge group styling"
    )
    assert "white-space: nowrap" in css, "Must prevent badge group line breaks"
    assert "flex-shrink: 0" in css, "Must prevent badge group squishing"
    assert "#quota-badge" in css, "Must style quota-badge within cost-quota group"
    assert 'class="cost-quota-group"' in html, (
        "HTML header must contain cost-quota-group"
    )
    assert "#inspect-banner" in css
    assert "#btn-reset-inspect" in css
    assert "#fleet-card" in css
    assert ".inspect-banner" in css
    assert ".reset-inspect-btn" in css
    assert ".fleet-summary-strip" in css
    assert ".fleet-list" in css
    assert ".fleet-node-item" in css
    assert ".fleet-mini-track" in css
    assert ".fleet-mini-bar" in css

    # App JS checks: Sidecar API, multi-segment, global mode, quota, and lifecycle guards
    assert "window.sidecar" in js
    assert "/api/session" in js
    assert "/api/global" in js
    assert "renderMultiSegmentBar" in js
    assert "renderQuota" in js
    assert "updateQuotaHeaderBadge" in js
    assert "updateCostHeaderBadge" in js
    assert "renderFleetCard" in js
    assert "renderGlobal" in js
    assert "fetchSessionData" in js
    assert "shortenModelName" in js
    assert "inspectedCid" in js
    assert "quota-weekly-pct" in js
    assert "quota-tab-gemini" in js
    assert "visibilitychange" in js, (
        "Must guard stream with visibility change listener"
    )
    assert "setInterval" not in js, "Must eliminate polling loop to satisfy Zero polling invariant"
    assert "EventSource" in js, "Must use EventSource for reactive SSE updates"
    assert "sidecar.agent.sendMessage" in js or "agent.sendMessage" in js


def test_sidecar_frontend_render_quota_execution():
    """Executes app.js in Node with a simulated DOM to deeply verify quota rendering and tab switching."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];

    for (const id of ids) {
      elements[id] = makeEl(id);
    }

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      sidecar: {
        conversationId: 'conv-gemini-1',
        fetch: async () => ({
          ok: true,
          json: async () => ({
            model_name: 'Gemini 3.8 Flash',
            billed_cost: 6.72,
            pct: 15.0,
            active_tokens: 150000,
            max_tokens: 1000000,
            weekly_quota_pct: 1.0,
            categories: [],
            top_steps: [],
            quota: {
              prefix: 'gemini',
              weekly_remaining_pct: 85.0,
              five_hour_remaining_pct: 90.0,
              session_weekly_pct: 1.0,
              weekly_reset_countdown: '6d 8h',
              five_hour_reset_countdown: '3h 12m',
              calibrated_weekly_capacity: 672.0,
              calibrated_5h_capacity: 236.7,
              buckets: {
                'gemini-weekly': { remaining_pct: 85.0, session_impact_pct: 1.0, calibrated_capacity: 672.0, reset_countdown: '6d 8h' },
                'gemini-5h': { remaining_pct: 90.0, session_impact_pct: 2.84, calibrated_capacity: 236.7, reset_countdown: '3h 12m' },
                '3p-weekly': { remaining_pct: 95.0, session_impact_pct: 0.0, calibrated_capacity: 200.0, reset_countdown: '6d 8h' },
                '3p-5h': { remaining_pct: 98.0, session_impact_pct: 0.0, calibrated_capacity: 70.0, reset_countdown: '3h 12m' }
              }
            }
          })
        })
      }
    };

    let intervals = [];
    const customSetInterval = (fn, ms) => {
      const id = setTimeout(fn, ms);
      intervals.push(id);
      return id;
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, customSetInterval);

    setTimeout(() => {
      intervals.forEach(clearTimeout);

      const results = {
        costBadge: elements['cost-badge'].textContent,
        weeklyPct: elements['quota-weekly-pct'].textContent,
        weeklyPctClass: elements['quota-weekly-pct'].className,
        weeklyBarWidth: elements['quota-weekly-bar'].style.width,
        weeklyBarClass: elements['quota-weekly-bar'].className,
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        quotaBadgeTitle: elements['quota-badge'].title,
        quotaBadgeClass: elements['quota-badge'].className,
        geminiTabSelected: elements['quota-tab-gemini'].attributes['aria-selected'],
        threepTabSelected: elements['quota-tab-3p'].attributes['aria-selected']
      };

      // Click 3P Tab
      elements['quota-tab-3p'].dispatchEvent('click');

      results.afterClick3p = {
        weeklyPct: elements['quota-weekly-pct'].textContent,
        weeklyCapSub: elements['quota-weekly-sub'].textContent,
        weeklyBarWidth: elements['quota-weekly-bar'].style.width,
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        quotaBadgeTitle: elements['quota-badge'].title,
        quotaBadgeClass: elements['quota-badge'].className,
        geminiTabSelected: elements['quota-tab-gemini'].attributes['aria-selected'],
        threepTabSelected: elements['quota-tab-3p'].attributes['aria-selected']
      };

      console.log(JSON.stringify(results));
      process.exit(0);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node DOM execution failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["costBadge"] == "$6.72"
    assert data["weeklyPct"] == "85.0% remaining"
    assert "ok" in data["weeklyBarClass"]
    assert data["weeklyBarWidth"] == "85%"
    assert data["sessionImpact"] == "1.00% session share"
    assert data["quotaBadge"] == "1.00% wk"
    assert data["quotaBadgeTitle"] == "Session Quota Impact"
    assert "ok" in data["quotaBadgeClass"]
    assert data["geminiTabSelected"] == "true"
    assert data["threepTabSelected"] == "false"

    # After clicking 3P tab
    after = data["afterClick3p"]
    assert after["weeklyPct"] == "95.0% remaining"
    assert "$200.00" in after["weeklyCapSub"]
    assert after["sessionImpact"] == "0.00% session share"
    assert after["quotaBadge"] == "1.00% wk"
    assert after["quotaBadgeTitle"] == "Session Quota Impact"
    assert "ok" in after["quotaBadgeClass"]
    assert after["geminiTabSelected"] == "false"
    assert after["threepTabSelected"] == "true"


def test_sidecar_frontend_global_mode_execution():
    """Verifies that in global mode (no active conversation), quota card displays fleet total."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];

    for (const id of ids) {
      elements[id] = makeEl(id);
    }

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      sidecar: {
        conversationId: null, // Global mode
        fetch: async () => ({
          ok: true,
          json: async () => ({
            total_conversations: 5,
            root_conversations: 2,
            subagent_conversations: 3,
            total_tokens: 250000,
            total_cost: 13.44,
            weekly_quota_pct: 2.0,
            quota: {
              prefix: 'gemini',
              weekly_remaining_pct: 85.0,
              five_hour_remaining_pct: 90.0,
              session_weekly_pct: 2.0,
              calibrated_weekly_capacity: 672.0,
              calibrated_5h_capacity: 236.7,
              buckets: {
                'gemini-weekly': { remaining_pct: 85.0, session_impact_pct: 2.0, calibrated_capacity: 672.0 },
                'gemini-5h': { remaining_pct: 90.0, session_impact_pct: 5.68, calibrated_capacity: 236.7 },
                '3p-weekly': { remaining_pct: 95.0, session_impact_pct: 0.0, calibrated_capacity: 200.0 },
                '3p-5h': { remaining_pct: 98.0, session_impact_pct: 0.0, calibrated_capacity: 70.0 }
              }
            }
          })
        })
      }
    };

    let intervals = [];
    const customSetInterval = (fn, ms) => {
      const id = setTimeout(fn, ms);
      intervals.push(id);
      return id;
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, customSetInterval);

    setTimeout(() => {
      intervals.forEach(clearTimeout);

      const results = {
        costBadge: elements['cost-badge'].textContent,
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        quotaBadgeTitle: elements['quota-badge'].title,
        quotaBadgeClass: elements['quota-badge'].className,
        globalQuota: elements['global-quota'].textContent,
        globalConvs: elements['global-convs'].textContent
      };

      // Click 3P Tab in global mode - badge should stay pinned to fleet total
      elements['quota-tab-3p'].dispatchEvent('click');

      results.afterClick3p = {
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        quotaBadgeTitle: elements['quota-badge'].title,
        quotaBadgeClass: elements['quota-badge'].className
      };

      console.log(JSON.stringify(results));
      process.exit(0);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node DOM global execution failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["costBadge"] == "$13.44"
    assert "fleet total" in data["sessionImpact"]
    assert data["quotaBadge"] == "2.00% fleet"
    assert data["quotaBadgeTitle"] == "Fleet Quota Impact"
    assert "ok" in data["quotaBadgeClass"]
    assert "2.00% wk" in data["globalQuota"]
    assert data["globalConvs"] == "2 Campaigns · 3 Sub-Agents"

    # Verify quota-badge stays pinned to fleet total after clicking 3P tab in global mode
    after = data["afterClick3p"]
    assert after["quotaBadge"] == "2.00% fleet"
    assert after["quotaBadgeTitle"] == "Fleet Quota Impact"
    assert "ok" in after["quotaBadgeClass"]
    assert "0.00% fleet total" in after["sessionImpact"]


def test_sidecar_frontend_update_quota_header_badge_unit():
    """Directly verifies updateQuotaHeaderBadge threshold classification, formatting, and edge cases."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    let badgeEl = { textContent: '', className: '', title: '' };
    const elements = { 'quota-badge': badgeEl };
    const $ = (id) => elements[id];

    eval(code.slice(code.indexOf('function updateQuotaHeaderBadge'), code.indexOf('let toastTimer;')));

    const runs = {};

    updateQuotaHeaderBadge(0, false);
    runs.zeroSession = { ...badgeEl };

    updateQuotaHeaderBadge(4.99, false);
    runs.underWarnSession = { ...badgeEl };

    updateQuotaHeaderBadge(5.0, false);
    runs.warnSession = { ...badgeEl };

    updateQuotaHeaderBadge(9.99, true);
    runs.warnGlobal = { ...badgeEl };

    updateQuotaHeaderBadge(10.0, true);
    runs.errorGlobal = { ...badgeEl };

    updateQuotaHeaderBadge(15.556, true);
    runs.highErrorGlobal = { ...badgeEl };

    updateQuotaHeaderBadge(null, false);
    runs.nullSession = { ...badgeEl };

    updateQuotaHeaderBadge(undefined, true);
    runs.undefinedGlobal = { ...badgeEl };

    updateQuotaHeaderBadge('not-a-number', false);
    runs.nanSession = { ...badgeEl };

    // Graceful no-op when badge element is missing
    elements['quota-badge'] = null;
    let missingNoError = true;
    try {
      updateQuotaHeaderBadge(5.0, false);
    } catch (e) {
      missingNoError = false;
    }
    runs.missingNoError = missingNoError;

    console.log(JSON.stringify(runs));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["zeroSession"]["textContent"] == "0.00% wk"
    assert data["zeroSession"]["title"] == "Session Quota Impact"
    assert data["zeroSession"]["className"] == "badge ok"

    assert data["underWarnSession"]["textContent"] == "4.99% wk"
    assert data["underWarnSession"]["title"] == "Session Quota Impact"
    assert data["underWarnSession"]["className"] == "badge ok"

    assert data["warnSession"]["textContent"] == "5.00% wk"
    assert data["warnSession"]["title"] == "Session Quota Impact"
    assert data["warnSession"]["className"] == "badge warning"

    assert data["warnGlobal"]["textContent"] == "9.99% fleet"
    assert data["warnGlobal"]["title"] == "Fleet Quota Impact"
    assert data["warnGlobal"]["className"] == "badge warning"

    assert data["errorGlobal"]["textContent"] == "10.00% fleet"
    assert data["errorGlobal"]["title"] == "Fleet Quota Impact"
    assert data["errorGlobal"]["className"] == "badge error"

    assert data["highErrorGlobal"]["textContent"] == "15.56% fleet"
    assert data["highErrorGlobal"]["title"] == "Fleet Quota Impact"
    assert data["highErrorGlobal"]["className"] == "badge error"

    assert data["nullSession"]["textContent"] == "0.00% wk"
    assert data["nullSession"]["className"] == "badge ok"

    assert data["undefinedGlobal"]["textContent"] == "0.00% fleet"
    assert data["undefinedGlobal"]["className"] == "badge ok"

    assert data["nanSession"]["textContent"] == "0.00% wk"
    assert data["nanSession"]["className"] == "badge ok"

    assert data["missingNoError"] is True


def test_sidecar_frontend_swarm_fleet_execution():
    """Executes app.js in Node with a simulated DOM to deeply verify swarm fleet rendering,
    node switching, inspect banner, and reset behavior."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];

    for (const id of ids) {
      elements[id] = makeEl(id);
    }

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    let fetchCalls = [];
    const mockWindow = {
      sidecar: {
        conversationId: 'conv-root-1',
        fetch: async (url) => {
          fetchCalls.push(url);
          const isInspectingWorker = url.includes('inspect_cid=subagent-worker-1');
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-root-1',
              model_name: isInspectingWorker ? 'Claude 3.7 Sonnet' : 'Gemini 3.7 Flash',
              billed_cost: isInspectingWorker ? 6.80 : 6.00,
              pct: isInspectingWorker ? 45.0 : 15.0,
              active_tokens: isInspectingWorker ? 90000 : 150000,
              max_tokens: isInspectingWorker ? 200000 : 1000000,
              weekly_quota_pct: isInspectingWorker ? 1.01 : 0.89,
              categories: [],
              top_steps: [],
              quota: {
                prefix: 'gemini',
                weekly_remaining_pct: 85.0,
                five_hour_remaining_pct: 90.0,
                session_weekly_pct: isInspectingWorker ? 1.01 : 0.89,
                buckets: {
                  'gemini-weekly': { remaining_pct: 85.0, session_impact_pct: isInspectingWorker ? 1.01 : 0.89, calibrated_capacity: 672.0 }
                }
              },
              fleet: {
                is_swarm: true,
                root_id: 'conv-root-1',
                active_cid: 'conv-root-1',
                inspected_cid: isInspectingWorker ? 'subagent-worker-1' : 'conv-root-1',
                is_inspecting_other: isInspectingWorker,
                inspected_role: isInspectingWorker ? 'Worker' : 'Root Coordinator',
                agent_count: 2,
                subagent_count: 1,
                total_cost: 12.80,
                total_tokens: 450000,
                total_disk_tokens: 450000,
                total_steps: 40,
                weekly_quota_pct: 1.90,
                five_hour_quota_pct: 5.41,
                shielded_tokens: 300000,
                shielding_ratio: 3.0,
                nodes: [
                  {
                    id: 'subagent-worker-1',
                    model_name: 'Claude 3.7 Sonnet',
                    billed_cost: 6.80,
                    total_tokens: 300000,
                    active_tokens: 90000,
                    max_tokens: 200000,
                    context_pct: 45.0,
                    weekly_quota_pct: 1.01,
                    role: 'Worker',
                    subagent_role: 'Worker',
                    is_root: false,
                    is_active: false,
                    is_active_session: false
                  },
                  {
                    id: 'conv-root-1',
                    model_name: 'Gemini 3.7 Flash',
                    billed_cost: 6.00,
                    total_tokens: 150000,
                    active_tokens: 150000,
                    max_tokens: 1000000,
                    context_pct: 15.0,
                    weekly_quota_pct: 0.89,
                    role: 'Root Coordinator',
                    subagent_role: 'Root Coordinator',
                    is_root: true,
                    is_active: true,
                    is_active_session: true
                  }
                ]
              }
            })
          };
        }
      }
    };

    let intervals = [];
    const customSetInterval = (fn, ms) => {
      const id = setTimeout(fn, ms);
      intervals.push(id);
      return id;
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, customSetInterval);

    setTimeout(() => {
      intervals.forEach(clearTimeout);

      const initial = {
        fetchCalls: [...fetchCalls],
        costBadge: elements['cost-badge'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        sessionImpact: elements['quota-session-impact'].textContent,
        fleetCardHidden: elements['fleet-card'].hidden,
        fleetCountBadge: elements['fleet-count-badge'].textContent,
        fleetTotalCost: elements['fleet-total-cost'].textContent,
        fleetTotalTokens: elements['fleet-total-tokens'].textContent,
        fleetShielding: elements['fleet-shielding'].textContent,
        inspectBannerHidden: elements['inspect-banner'].hidden,
        nodeCount: elements['fleet-node-list'].children.length,
        nodes: elements['fleet-node-list'].children.map(c => ({
          className: c.className,
          innerHTML: c.innerHTML,
          nodeId: c.getAttribute('data-node-id')
        }))
      };

      // Step 2: Click worker node (first in list)
      const workerEl = elements['fleet-node-list'].children[0];
      workerEl.dispatchEvent('click');

      // Wait a tick for async refreshTelemetry
      setTimeout(() => {
        const afterClickWorker = {
          fetchCalls: [...fetchCalls],
          costBadge: elements['cost-badge'].textContent,
          quotaBadge: elements['quota-badge'].textContent,
          inspectBannerHidden: elements['inspect-banner'].hidden,
          inspectBannerLabel: elements['inspect-banner-label'].textContent,
          workerInspected: elements['fleet-node-list'].children[0].className.includes('inspected'),
          rootInspected: elements['fleet-node-list'].children[1].className.includes('inspected')
        };

        // Step 3: Click Reset Inspect button
        elements['btn-reset-inspect'].dispatchEvent('click');

        setTimeout(() => {
          const afterReset = {
            fetchCalls: [...fetchCalls],
            inspectBannerHidden: elements['inspect-banner'].hidden,
            costBadge: elements['cost-badge'].textContent,
            workerInspected: elements['fleet-node-list'].children[0].className.includes('inspected'),
            rootInspected: elements['fleet-node-list'].children[1].className.includes('inspected')
          };

          console.log(JSON.stringify({ initial, afterClickWorker, afterReset }));
          process.exit(0);
        }, 50);
      }, 50);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node DOM swarm execution failed: {res.stderr}"
    data = json.loads(res.stdout)

    initial = data["initial"]
    assert initial["costBadge"] == "$6.00 · $12.80 fleet"
    assert initial["quotaBadge"] == "0.89% · 1.90% fleet wk"
    assert initial["sessionImpact"] == "0.89% session · 1.90% fleet"
    assert initial["fleetCardHidden"] is False
    assert initial["fleetCountBadge"] == "2 Agents"
    assert initial["fleetTotalCost"] == "$12.80"
    assert initial["fleetTotalTokens"] == "450.0k"
    assert initial["fleetShielding"] == "3.0x"
    assert initial["inspectBannerHidden"] is True
    assert initial["nodeCount"] == 2

    # Node 0 is Worker, Node 1 is Root
    worker_node = initial["nodes"][0]
    root_node = initial["nodes"][1]
    assert "Worker" in worker_node["innerHTML"]
    assert "3.7 Sonnet" in worker_node["innerHTML"]
    assert "$6.80 · 1.01% wk" in worker_node["innerHTML"]
    assert "fleet-mini-bar" in worker_node["innerHTML"]
    assert "45.0%" in worker_node["innerHTML"]

    assert "Root" in root_node["innerHTML"]
    assert "Root Coordinator" in root_node["innerHTML"]
    assert "inspected" in root_node["className"]

    # After clicking worker node
    after_click = data["afterClickWorker"]
    assert any("inspect_cid=subagent-worker-1" in call for call in after_click["fetchCalls"])
    assert after_click["inspectBannerHidden"] is False
    assert after_click["inspectBannerLabel"] == "Viewing: Worker"
    assert after_click["workerInspected"] is True
    assert after_click["rootInspected"] is False

    # After clicking Back to Active Session button
    after_reset = data["afterReset"]
    assert after_reset["inspectBannerHidden"] is True
    assert after_reset["workerInspected"] is False
    assert after_reset["rootInspected"] is True


def test_sidecar_frontend_dual_badges_unit():
    """Unit tests updateCostHeaderBadge and updateQuotaHeaderBadge with swarm fleet data and thresholds."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    let costBadgeEl = { textContent: '', className: '', title: '' };
    let quotaBadgeEl = { textContent: '', className: '', title: '' };
    const elements = { 'cost-badge': costBadgeEl, 'quota-badge': quotaBadgeEl };
    const $ = (id) => elements[id];

    let isGlobalMode = false;
    eval(code.slice(code.indexOf('function formatTokens'), code.indexOf('let toastTimer;')));

    const runs = {};

    // Cost badge: Non-swarm
    updateCostHeaderBadge(4.25, { is_swarm: false, total_cost: 12.80 }, false);
    runs.costNonSwarm = { ...costBadgeEl };

    // Cost badge: Swarm
    updateCostHeaderBadge(4.25, { is_swarm: true, total_cost: 12.80 }, false);
    runs.costSwarm = { ...costBadgeEl };

    // Cost badge: Swarm zero
    updateCostHeaderBadge(0, { is_swarm: true, total_cost: 0 }, false);
    runs.costSwarmZero = { ...costBadgeEl };

    // Cost badge: Global mode
    updateCostHeaderBadge(12.80, null, true);
    runs.costGlobal = { ...costBadgeEl };

    // Quota badge: Swarm ok (both < 5%)
    updateQuotaHeaderBadge(0.63, false, { is_swarm: true, weekly_quota_pct: 1.90 });
    runs.quotaSwarmOk = { ...quotaBadgeEl };

    // Quota badge: Swarm warning by fleet (>= 5%)
    updateQuotaHeaderBadge(2.00, false, { is_swarm: true, weekly_quota_pct: 6.50 });
    runs.quotaSwarmWarnFleet = { ...quotaBadgeEl };

    // Quota badge: Swarm warning by session (>= 5%)
    updateQuotaHeaderBadge(7.50, false, { is_swarm: true, weekly_quota_pct: 2.00 });
    runs.quotaSwarmWarnSession = { ...quotaBadgeEl };

    // Quota badge: Swarm error by fleet (>= 10%)
    updateQuotaHeaderBadge(2.00, false, { is_swarm: true, weekly_quota_pct: 11.20 });
    runs.quotaSwarmErrorFleet = { ...quotaBadgeEl };

    // Quota badge: Swarm error by session (>= 10%)
    updateQuotaHeaderBadge(12.50, false, { is_swarm: true, weekly_quota_pct: 3.00 });
    runs.quotaSwarmErrorSession = { ...quotaBadgeEl };

    console.log(JSON.stringify(runs));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["costNonSwarm"]["textContent"] == "$4.25"
    assert data["costSwarm"]["textContent"] == "$4.25 · $12.80 fleet"
    assert data["costSwarm"]["title"] == "Session Cost · Fleet Cost"
    assert data["costSwarmZero"]["textContent"] == "$0.00 · $0.00 fleet"
    assert data["costGlobal"]["textContent"] == "$12.80"

    assert data["quotaSwarmOk"]["textContent"] == "0.63% · 1.90% fleet wk"
    assert data["quotaSwarmOk"]["className"] == "badge ok"

    assert data["quotaSwarmWarnFleet"]["textContent"] == "2.00% · 6.50% fleet wk"
    assert data["quotaSwarmWarnFleet"]["className"] == "badge warning"

    assert data["quotaSwarmWarnSession"]["textContent"] == "7.50% · 2.00% fleet wk"
    assert data["quotaSwarmWarnSession"]["className"] == "badge warning"

    assert data["quotaSwarmErrorFleet"]["textContent"] == "2.00% · 11.20% fleet wk"
    assert data["quotaSwarmErrorFleet"]["className"] == "badge error"

    assert data["quotaSwarmErrorSession"]["textContent"] == "12.50% · 3.00% fleet wk"
    assert data["quotaSwarmErrorSession"]["className"] == "badge error"


def test_sidecar_frontend_shorten_model_name_unit():
    """Unit tests shortenModelName with Gemini, Claude, hyphenated prefixes, medium, and truncation."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    eval(code.slice(code.indexOf('function shortenModelName'), code.indexOf('function updateCostHeaderBadge')));

    const runs = {
      defaultVal: shortenModelName(null),
      emptyVal: shortenModelName(''),
      geminiSpace: shortenModelName('Gemini 3.7 Flash'),
      geminiHyphen: shortenModelName('gemini-2.5-flash'),
      claudeSpace: shortenModelName('Claude 3.7 Sonnet'),
      claudeHyphen: shortenModelName('claude-3-5-sonnet'),
      mediumFlag: shortenModelName('Gemini 3.8 Flash (Medium)'),
      customShort: shortenModelName('custom-gpt-4o'),
      customLong: shortenModelName('deepseek-r1-distill-llama-70b')
    };

    console.log(JSON.stringify(runs));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["defaultVal"] == "Gemini"
    assert data["emptyVal"] == "Gemini"
    assert data["geminiSpace"] == "3.7 Flash"
    assert data["geminiHyphen"] == "2.5-flash"
    assert data["claudeSpace"] == "3.7 Sonnet"
    assert data["claudeHyphen"] == "3-5-sonnet"
    assert data["mediumFlag"] == "3.8 Flash (Med)"
    assert data["customShort"] == "custom-gpt-4o"
    assert len(data["customLong"]) <= 20
    assert data["customLong"].endswith("…")


def test_sidecar_frontend_node_click_and_reset_inspect():
    """Verifies clicking root node resets inspect, switching conversations clears inspect,
    and non-swarm sessions hide fleet card and reset inspect."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt, data) { if (this.listeners[evt]) this.listeners[evt](data); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];

    for (const id of ids) {
      elements[id] = makeEl(id);
    }

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    let fetchCalls = [];
    const mockWindow = {
      sidecar: {
        conversationId: 'conv-root-1',
        fetch: async (url) => {
          fetchCalls.push(url);
          const isInspectingWorker = url.includes('inspect_cid=subagent-worker-1');
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-root-1',
              model_name: 'Gemini 3.7 Flash',
              billed_cost: 6.00,
              pct: 20.0,
              active_tokens: 200000,
              max_tokens: 1000000,
              weekly_quota_pct: 0.89,
              fleet: {
                is_swarm: true,
                root_id: 'conv-root-1',
                active_cid: 'conv-root-1',
                inspected_cid: isInspectingWorker ? 'subagent-worker-1' : 'conv-root-1',
                is_inspecting_other: isInspectingWorker,
                inspected_role: isInspectingWorker ? 'Worker' : 'Root Coordinator',
                agent_count: 2,
                total_cost: 12.00,
                total_tokens: 400000,
                weekly_quota_pct: 1.78,
                shielding_ratio: 2.0,
                nodes: [
                  {
                    id: 'subagent-worker-1',
                    model_name: 'Claude 3.7 Sonnet',
                    billed_cost: 6.00,
                    active_tokens: 100000,
                    max_tokens: 200000,
                    context_pct: 50.0,
                    weekly_quota_pct: 0.89,
                    role: 'Worker',
                    is_root: false,
                    is_active: false
                  },
                  {
                    id: 'conv-root-1',
                    model_name: 'Gemini 3.7 Flash',
                    billed_cost: 6.00,
                    active_tokens: 200000,
                    max_tokens: 1000000,
                    context_pct: 20.0,
                    weekly_quota_pct: 0.89,
                    role: 'Root Coordinator',
                    is_root: true,
                    is_active: true
                  }
                ]
              }
            })
          };
        }
      }
    };

    let intervals = [];
    const customSetInterval = (fn, ms) => {
      const id = setTimeout(fn, ms);
      intervals.push(id);
      return id;
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, customSetInterval);

    setTimeout(() => {
      intervals.forEach(clearTimeout);

      // Step 1: Click worker node
      const workerEl = elements['fleet-node-list'].children[0];
      workerEl.dispatchEvent('click');

      setTimeout(() => {
        const afterWorker = {
          bannerHidden: elements['inspect-banner'].hidden,
          bannerLabel: elements['inspect-banner-label'].textContent
        };

        // Step 2: Click Root node directly in the fleet node list (node 1)
        const rootEl = elements['fleet-node-list'].children[1];
        rootEl.dispatchEvent('click');

        setTimeout(() => {
          const afterRootClick = {
            bannerHidden: elements['inspect-banner'].hidden,
            rootInspected: elements['fleet-node-list'].children[1].className.includes('inspected')
          };

          // Step 3: Switch conversation ID
          mockWindow.sidecar.conversationId = 'conv-root-2';
          mockWindow.sidecar.fetch = async (url) => ({
            ok: true,
            json: async () => ({
              cid: 'conv-root-2',
              model_name: 'Gemini 3.7 Flash',
              billed_cost: 2.00,
              pct: 5.0,
              active_tokens: 50000,
              max_tokens: 1000000,
              fleet: { is_swarm: false, nodes: [] }
            })
          });

          // Trigger poll via visibilitychange
          mockDocument.hidden = false;
          // Run refreshTelemetry indirectly or via interval
          elements['btn-reset-inspect'].dispatchEvent('click');

          setTimeout(() => {
            const afterSwitch = {
              fleetCardHidden: elements['fleet-card'].hidden,
              inspectBannerHidden: elements['inspect-banner'].hidden
            };

            console.log(JSON.stringify({ afterWorker, afterRootClick, afterSwitch }));
            process.exit(0);
          }, 50);
        }, 50);
      }, 50);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["afterWorker"]["bannerHidden"] is False
    assert data["afterWorker"]["bannerLabel"] == "Viewing: Worker"

    # Clicking root node directly resets inspect
    assert data["afterRootClick"]["bannerHidden"] is True
    assert data["afterRootClick"]["rootInspected"] is True

    # After switching to non-swarm conversation
    assert data["afterSwitch"]["fleetCardHidden"] is True
    assert data["afterSwitch"]["inspectBannerHidden"] is True


def test_sidecar_frontend_inspect_error_fallback():
    """Verifies that if fetching an inspected subagent fails, refreshTelemetry recovers by
    resetting inspectedCid and fetching the active session, keeping status Live."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];
    for (const id of ids) elements[id] = makeEl(id);

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    let fetchAttempt = 0;
    let fallbackHappened = false;
    const mockWindow = {
      sidecar: {
        conversationId: 'conv-root-1',
        fetch: async (url) => {
          fetchAttempt++;
          if (url.includes('inspect_cid=')) {
            // Simulate 404 for deleted subagent
            return { ok: false, status: 404 };
          }
          fallbackHappened = true;
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-root-1',
              model_name: 'Gemini 3.7 Flash',
              billed_cost: 3.50,
              pct: 10.0,
              active_tokens: 100000,
              max_tokens: 1000000,
              fleet: { is_swarm: true, agent_count: 2, total_cost: 7.00, nodes: [
                { id: 'subagent-dead', billed_cost: 3.50, is_root: false, role: 'Worker' },
                { id: 'conv-root-1', billed_cost: 3.50, is_root: true, role: 'Root Coordinator' }
              ] }
            })
          };
        }
      }
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, () => 1);

    setTimeout(() => {
      // Click dead subagent node
      const workerEl = elements['fleet-node-list'].children[0];
      workerEl.dispatchEvent('click');

      setTimeout(() => {
        const results = {
          fallbackHappened,
          statusText: elements['status-badge'].textContent,
          statusClass: elements['status-badge'].className,
          bannerHidden: elements['inspect-banner'].hidden
        };
        console.log(JSON.stringify(results));
        process.exit(0);
      }, 50);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["fallbackHappened"] is True
    assert data["statusText"] == "Live"
    assert "ok" in data["statusClass"]
    assert data["bannerHidden"] is True


def test_sidecar_frontend_zero_tokens_and_accessibility():
    """Verifies that nodes with 0 active tokens correctly display ~0 in token labels,
    and nodes have role='button' and tabindex='0' for accessibility."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];
    for (const id of ids) elements[id] = makeEl(id);

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      sidecar: {
        conversationId: 'conv-root-1',
        fetch: async () => ({
          ok: true,
          json: async () => ({
            cid: 'conv-root-1',
            model_name: 'Gemini 3.7 Flash',
            billed_cost: 1.00,
            pct: 0.0,
            active_tokens: 0,
            max_tokens: 1000000,
            fleet: {
              is_swarm: true,
              agent_count: 2,
              total_cost: 2.00,
              nodes: [
                {
                  id: 'subagent-compacted',
                  model_name: 'Gemini 3.7 Flash',
                  billed_cost: 1.00,
                  active_tokens: 0,
                  total_tokens: 80000,
                  max_tokens: 1000000,
                  context_pct: 0.0,
                  weekly_quota_pct: 0.15,
                  role: 'Worker',
                  is_root: false
                },
                {
                  id: 'conv-root-1',
                  model_name: 'Gemini 3.7 Flash',
                  billed_cost: 1.00,
                  active_tokens: 10000,
                  total_tokens: 10000,
                  max_tokens: 1000000,
                  context_pct: 1.0,
                  weekly_quota_pct: 0.15,
                  role: 'Root Coordinator',
                  is_root: true
                }
              ]
            }
          })
        })
      }
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, () => 1);

    setTimeout(() => {
      const node0 = elements['fleet-node-list'].children[0];
      const results = {
        roleAttr: node0.getAttribute('role'),
        tabindexAttr: node0.getAttribute('tabindex'),
        innerHTML: node0.innerHTML
      };
      console.log(JSON.stringify(results));
      process.exit(0);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["roleAttr"] == "button"
    assert data["tabindexAttr"] == "0"
    assert "~0 / 1.00M (0.0%)" in data["innerHTML"]


def test_sidecar_frontend_sse_subscription_and_no_polling():
    """Verifies that app.js adheres to the Zero Polling invariant:
    1. Removes setInterval polling.
    2. Opens EventSource to /api/events?cid=<cid> on load.
    3. Handles incoming 'update' SSE events to refresh telemetry.
    4. Reconnects EventSource with inspect_cid when inspecting a swarm subagent.
    5. Disconnects EventSource on visibilitychange hidden=true, reconnects on hidden=false."""
    import json
    import subprocess

    js_code = Path("sidecars/panel/app.js").read_text(encoding="utf-8")
    assert "setInterval" not in js_code, "app.js must not contain setInterval polling"
    assert "EventSource" in js_code, "app.js must use EventSource"

    node_script = """
    import fs from 'node:fs';

    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    class MockEventSource {
      static instances = [];
      constructor(url) {
        this.url = url;
        this.listeners = {};
        this.readyState = 0;
        this.closed = false;
        MockEventSource.instances.push(this);
      }
      addEventListener(event, fn) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(fn);
      }
      removeEventListener(event, fn) {
        if (this.listeners[event]) {
          this.listeners[event] = this.listeners[event].filter(l => l !== fn);
        }
      }
      close() {
        this.closed = true;
        this.readyState = 2;
      }
      emit(event, data) {
        if (this.listeners[event]) {
          for (const fn of this.listeners[event]) {
            fn({ data: JSON.stringify(data) });
          }
        }
      }
    }

    const elements = {};
    const makeEl = (tag) => {
      const el = {
        tagName: tag,
        attributes: {},
        style: {},
        classList: {
          contains: () => false,
          add: () => {},
          remove: () => {},
          toggle: () => {}
        },
        children: [],
        appendChild: (child) => { el.children.push(child); return child; },
        setAttribute: (k, v) => { el.attributes[k] = v; },
        getAttribute: (k) => el.attributes[k] || null,
        removeAttribute: (k) => { delete el.attributes[k]; },
        addEventListener: () => {},
        hidden: false,
        textContent: '',
        innerHTML: ''
      };
      return el;
    };

    const ids = [
      'capacity-bar', 'top-steps-list', 'top-steps-card', 'capacity-card',
      'actions-card', 'status-badge', 'cost-badge', 'quota-badge', 'global-card',
      'compaction-banner', 'compaction-banner-text', 'banner-compaction-count',
      'banner-pruned-tokens', 'quota-card', 'quota-weekly-pct', 'quota-weekly-bar',
      'quota-5h-pct', 'quota-5h-bar', 'quota-session-impact', 'quota-tab-gemini',
      'quota-tab-3p', 'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip',
      'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'velocity-strip', 'velocity-5m-tpm', 'velocity-60m-tph', 'velocity-pct-hour',
      'velocity-bullet-marker', 'toast', 'btn-compact'
    ];
    for (const id of ids) elements[id] = makeEl('div');

    const docListeners = {};
    let fetchCount = 0;
    const fetchCalls = [];

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: (evt, fn) => {
        if (!docListeners[evt]) docListeners[evt] = [];
        docListeners[evt].push(fn);
      },
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      EventSource: MockEventSource,
      location: { search: '?token=secret123' },
      sidecar: {
        conversationId: 'conv-root-1',
        fetch: async (url) => {
          fetchCount++;
          fetchCalls.push(url);
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-root-1',
              model_name: 'Gemini 3.7 Flash',
              billed_cost: 0.50,
              pct: 25.0,
              active_tokens: 250000,
              max_tokens: 1000000,
              fleet: {
                is_swarm: true,
                agent_count: 2,
                total_cost: 1.00,
                nodes: [
                  { id: 'subagent-worker-1', model_name: 'Gemini 3.7 Flash', billed_cost: 0.50, active_tokens: 50000, is_root: false, role: 'Worker' },
                  { id: 'conv-root-1', model_name: 'Gemini 3.7 Flash', billed_cost: 0.50, active_tokens: 200000, is_root: true, role: 'Root' }
                ]
              }
            })
          };
        }
      }
    };

    const fn = new Function('window', 'document', 'EventSource', 'fetch', code);
    fn(mockWindow, mockDocument, MockEventSource, mockWindow.sidecar.fetch);

    await new Promise((r) => setTimeout(r, 60));

    // 1. Initial connection
    const firstES = MockEventSource.instances[0];
    const initialUrl = firstES ? firstES.url : null;
    const fetchAfterInit = fetchCount;

    // 2. Incoming SSE 'update' event
    firstES.emit('update', { cid: 'conv-root-1', timestamp: new Date().toISOString() });
    await new Promise((r) => setTimeout(r, 60));
    const fetchAfterUpdate = fetchCount;

    // 3. Inspect swarm subagent node
    mockWindow.selectFleetNode('subagent-worker-1');
    await new Promise((r) => setTimeout(r, 60));
    const secondES = MockEventSource.instances[MockEventSource.instances.length - 1];
    const inspectUrl = secondES ? secondES.url : null;
    const firstClosedOnInspect = firstES.closed;

    // 4. Hide document (visibilitychange)
    mockDocument.hidden = true;
    for (const l of (docListeners['visibilitychange'] || [])) l();
    const secondClosedOnHide = secondES.closed;

    // 5. Show document again
    mockDocument.hidden = false;
    for (const l of (docListeners['visibilitychange'] || [])) l();
    await new Promise((r) => setTimeout(r, 60));
    const thirdES = MockEventSource.instances[MockEventSource.instances.length - 1];
    const resumedUrl = thirdES ? thirdES.url : null;

    console.log(JSON.stringify({
      instanceCount: MockEventSource.instances.length,
      initialUrl,
      fetchAfterInit,
      fetchAfterUpdate,
      inspectUrl,
      firstClosedOnInspect,
      secondClosedOnHide,
      resumedUrl
    }));
    """
    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}\nStdout: {res.stdout}"
    data = json.loads(res.stdout)

    assert "/api/events?cid=conv-root-1" in data["initialUrl"]
    assert "token=secret123" in data["initialUrl"]
    assert data["fetchAfterUpdate"] > data["fetchAfterInit"]
    assert "inspect_cid=subagent-worker-1" in data["inspectUrl"]
    assert data["firstClosedOnInspect"] is True
    assert data["secondClosedOnHide"] is True
    assert "inspect_cid=subagent-worker-1" in data["resumedUrl"]


def test_sidecar_host_tokens_and_typography():
    """Verifies agy-sidecar-ui host theme tokens, Hub typography, dark mode, and base ergonomics."""
    import re

    html = Path("sidecars/panel/index.html").read_text(encoding="utf-8")
    css = Path("sidecars/panel/styles.css").read_text(encoding="utf-8")

    # Script / stylesheet ordering and non-blocking Hub font loading
    assert html.index('src="/preload.js"') < html.index('href="/styles.css"')
    assert 'rel="preconnect" href="https://fonts.googleapis.com"' in html
    assert 'rel="preconnect" href="https://fonts.gstatic.com"' in html
    assert "Google+Sans+Flex" in html
    assert "Google+Sans+Code" in html
    assert 'media="print"' in html
    assert 'onload="this.media=\'all\'"' in html

    # All 20 host tokens consumed
    host_tokens = [
        "var(--background",
        "var(--card",
        "var(--secondary",
        "var(--muted",
        "var(--foreground",
        "var(--card-foreground",
        "var(--secondary-foreground",
        "var(--muted-foreground",
        "var(--placeholder",
        "var(--code-foreground",
        "var(--border",
        "var(--card-border",
        "var(--primary",
        "var(--primary-foreground",
        "var(--accent",
        "var(--accent-foreground",
        "var(--error",
        "var(--ring",
        "var(--font-family",
        "var(--editor-font-family",
    ]
    for token in host_tokens:
        assert token in css, f"Expected host token {token} in styles.css"

    # Semantic --ui-* tokens, type scale, and radii produced
    produced_tokens = [
        "--ui-bg:",
        "--ui-card:",
        "--ui-secondary:",
        "--ui-muted:",
        "--ui-fg:",
        "--ui-fg-2:",
        "--ui-fg-muted:",
        "--ui-fg-faint:",
        "--ui-border:",
        "--ui-card-border:",
        "--ui-primary:",
        "--ui-primary-fg:",
        "--ui-error:",
        "--ui-success:",
        "--ui-warning:",
        "--ui-hover:",
        "--ui-press:",
        "--ui-ring:",
        "--ui-line:",
        "--ui-fill:",
        "--ui-field:",
        "--ui-font:",
        "--ui-mono:",
        "--text-2xs:",
        "--text-xs:",
        "--text-sm:",
        "--text-base:",
        "--r-sm:",
        "--r-md:",
        "--r-lg:",
    ]
    for token in produced_tokens:
        assert token in css, f"Expected semantic token {token} in styles.css"

    # Dark theme overrides & standalone prefers-color-scheme fallback
    assert ':root[data-theme="dark"]' in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css

    # Hub typography & preload.js inline font override + tabular numerals
    assert "font-family: var(--ui-font) !important" in css
    assert "font-variant-ligatures: no-contextual" in css
    assert "font-variation-settings:" in css
    assert "font-variant-numeric: tabular-nums" in css

    # Base ergonomics & invariants
    assert ":focus-visible" in css
    assert "scrollbar-width: thin" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition: none !important" in css
    assert "animation: none !important" in css

    # No hardcoded legacy light-mode status hexes anywhere in status rules
    css_lower = css.lower()
    for legacy_hex in ("#1e8e3e", "#e37400", "#d93025", "#0284c7"):
        assert legacy_hex not in css_lower, (
            f"Legacy hardcoded status color {legacy_hex} must use --ui-* semantic tokens"
        )

    for selector_pattern in (
        r"\.quota-val\.ok\s*\{[^}]*var\(--ui-success\)",
        r"\.quota-val\.warning\s*\{[^}]*var\(--ui-warning\)",
        r"\.quota-val\.error\s*\{[^}]*var\(--ui-error\)",
        r"\.quota-bar\.ok\s*\{[^}]*var\(--ui-success\)",
        r"\.quota-bar\.warning\s*\{[^}]*var\(--ui-warning\)",
        r"\.quota-bar\.error\s*\{[^}]*var\(--ui-error\)",
        r"\.fleet-mini-bar\.ok\s*\{[^}]*var\(--ui-success\)",
        r"\.fleet-mini-bar\.warning\s*\{[^}]*var\(--ui-warning\)",
        r"\.fleet-mini-bar\.error\s*\{[^}]*var\(--ui-error\)",
        r"\.toast\.error\s*\{[^}]*var\(--ui-error\)",
    ):
        assert re.search(selector_pattern, css, re.DOTALL), (
            f"Rule matching {selector_pattern} must consume --ui-* status token"
        )


def test_sidecar_sync_scheme_luminance_and_observer():
    """Verifies syncScheme() luminance detection for rgb() and color(srgb ...) plus MutationObserver registration."""
    import json
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; }
      };
    }

    const docEl = {
      dataset: {},
      style: { setProperty() {} },
      children: [],
      appendChild(c) { this.children.push(c); },
      removeChild(c) { this.children = this.children.filter(x => x !== c); }
    };

    let currentColor = 'rgb(24, 24, 27)';
    const getComputedStyle = () => ({ color: currentColor });

    const observerCalls = [];
    let observerCallback = null;
    class MockMutationObserver {
      constructor(cb) {
        observerCallback = cb;
      }
      observe(target, options) {
        observerCalls.push({ targetIsDocEl: target === docEl, options });
      }
    }

    const mockDocument = {
      documentElement: docEl,
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      getComputedStyle,
      MutationObserver: MockMutationObserver,
      sidecar: {
        conversationId: null,
        fetch: async () => ({ ok: true, json: async () => ({ total_conversations: 0, total_tokens: 0, total_cost: 0, total_compactions: 0, total_pruned_tokens: 0 }) })
      }
    };

    const fn = new Function('window', 'document', 'getComputedStyle', 'MutationObserver', 'fetch', code);
    fn(mockWindow, mockDocument, getComputedStyle, MockMutationObserver, mockWindow.sidecar.fetch);

    const results = {
      hasWindowSyncScheme: typeof mockWindow.syncScheme === 'function',
      initialDarkRgb: docEl.dataset.theme,
      observerCalls
    };

    currentColor = 'rgb(247, 247, 248)';
    if (observerCallback) observerCallback();
    results.lightRgbViaObserver = docEl.dataset.theme;

    currentColor = 'color(srgb 0.1 0.1 0.1)';
    if (typeof mockWindow.syncScheme === 'function') mockWindow.syncScheme();
    results.darkSrgb = docEl.dataset.theme;

    currentColor = 'color(srgb 0.95 0.95 0.95)';
    if (typeof mockWindow.syncScheme === 'function') mockWindow.syncScheme();
    results.lightSrgb = docEl.dataset.theme;

    console.log(JSON.stringify(results));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["hasWindowSyncScheme"] is True
    assert data["initialDarkRgb"] == "dark"
    assert data["lightRgbViaObserver"] == "light"
    assert data["darkSrgb"] == "dark"
    assert data["lightSrgb"] == "light"
    assert len(data["observerCalls"]) >= 1
    assert data["observerCalls"][0]["targetIsDocEl"] is True
    assert data["observerCalls"][0]["options"] == {
        "attributes": True,
        "attributeFilter": ["style"],
    }


def test_sidecar_layout_and_skeleton_lifecycle():
    """Verifies sticky responsive layout CSS, HTML shell elements, ResizeObserver --header-h, and loading skeleton lifecycle."""
    import json
    import re
    import subprocess

    html = Path("sidecars/panel/index.html").read_text(encoding="utf-8")
    css = Path("sidecars/panel/styles.css").read_text(encoding="utf-8")

    # CSS layout, skeleton, empty-card, overflow protection, monochrome primary discipline
    assert "--col: 760px" in css
    assert "--inline:" in css
    assert "@media (max-width: 420px)" in css
    assert "position: sticky" in css
    assert ".skel" in css
    assert ".empty-card" in css
    assert ".live-dot" in css
    assert "min-width: 0" in css
    assert "left: 50%" in css
    assert "transform: translateX(-50%)" in css
    assert "max-width: min(92vw, 520px)" in css

    for selector_pattern in (
        r"\.capacity-val\s*\{[^}]*color:\s*var\(--ui-fg\)",
        r"\.step-source\s*\{[^}]*color:\s*var\(--ui-fg\)",
        r"\.tab-btn\.active\s*\{[^}]*color:\s*var\(--ui-fg\)",
    ):
        assert re.search(selector_pattern, css, re.DOTALL), (
            f"Rule matching {selector_pattern} must enforce monochrome --ui-fg"
        )

    # HTML shell elements
    assert 'id="loading-skeleton"' in html
    assert "live-dot" in html
    assert 'id="panel-header"' in html
    assert 'id="live-status"' in html

    # Execute app.js in Node across global, session, and error paths
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    function runScenario(mode) {
      return new Promise((resolve) => {
        const elements = {};
        function makeEl(id) {
          return {
            id,
            textContent: '',
            className: '',
            style: {},
            offsetHeight: 68,
            hidden: false,
            attributes: {},
            listeners: {},
            children: [],
            appendChild(c) { this.children.push(c); },
            removeChild(c) { this.children = this.children.filter(x => x !== c); },
            setAttribute(k, v) { this.attributes[k] = v; },
            getAttribute(k) { return this.attributes[k]; },
            classList: {
              classes: new Set(),
              add(c) { this.classes.add(c); },
              remove(c) { this.classes.delete(c); },
              toggle(c, force) {
                if (force === undefined) {
                  if (this.classes.has(c)) this.classes.delete(c);
                  else this.classes.add(c);
                } else if (force) {
                  this.classes.add(c);
                } else {
                  this.classes.delete(c);
                }
              },
              contains(c) { return this.classes.has(c); }
            },
            addEventListener(evt, fn) { this.listeners[evt] = fn; }
          };
        }

        const ids = [
          'panel-header', 'live-status', 'status-dot', 'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
          'loading-skeleton', 'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
          'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
          'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
          'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
          'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
          'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
          'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
          'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
          'top-steps-card', 'top-steps-list', 'steps-range',
          'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
        ];
        for (const id of ids) elements[id] = makeEl(id);

        const cssVars = {};
        const docEl = {
          dataset: {},
          style: { setProperty(k, v) { cssVars[k] = v; } },
          appendChild() {},
          removeChild() {}
        };

        const resizeObserved = [];
        class MockResizeObserver {
          constructor(cb) { this.cb = cb; }
          observe(target) {
            resizeObserved.push(target.id);
            this.cb();
          }
        }

        const mockDocument = {
          documentElement: docEl,
          getElementById: (id) => elements[id],
          createElement: (tag) => makeEl(tag),
          addEventListener: () => {},
          readyState: 'complete',
          hidden: false
        };

        const mockFetch = async () => {
          if (mode === 'error') {
            return { ok: false, status: 500 };
          }
          if (mode === 'global') {
            return {
              ok: true,
              json: async () => ({
                total_conversations: 1,
                total_tokens: 1000,
                total_cost: 0.1,
                weekly_quota_pct: 0.05,
                total_compactions: 0,
                total_pruned_tokens: 0
              })
            };
          }
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-1',
              model_name: 'Gemini 3.7 Flash',
              billed_cost: 0.25,
              pct: 12.0,
              active_tokens: 120000,
              max_tokens: 1000000,
              weekly_quota_pct: 0.1,
              categories: [],
              top_steps: [],
              min_step: 1,
              max_step: 2
            })
          };
        };

        const mockWindow = {
          getComputedStyle: () => ({ color: 'rgb(247, 247, 248)' }),
          ResizeObserver: MockResizeObserver,
          sidecar: {
            conversationId: mode === 'global' ? null : 'conv-1',
            fetch: mockFetch
          }
        };

        const initialSkelHidden = elements['loading-skeleton'].hidden;
        const fn = new Function('window', 'document', 'getComputedStyle', 'ResizeObserver', 'fetch', code);
        fn(mockWindow, mockDocument, mockWindow.getComputedStyle, MockResizeObserver, mockFetch);

        setTimeout(() => {
          resolve({
            initialSkelHidden,
            finalSkelHidden: elements['loading-skeleton'].hidden,
            skelAriaBusy: elements['loading-skeleton'].getAttribute('aria-busy'),
            statusText: elements['status-badge'].textContent,
            liveStatusHasError: elements['live-status'].classList.contains('error'),
            headerH: cssVars['--header-h'] || null,
            resizeObserved
          });
        }, 40);
      });
    }

    Promise.all([
      runScenario('global'),
      runScenario('session'),
      runScenario('error')
    ]).then(([globalRes, sessionRes, errorRes]) => {
      console.log(JSON.stringify({ globalRes, sessionRes, errorRes }));
    });
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    for key in ("globalRes", "sessionRes", "errorRes"):
        assert data[key]["initialSkelHidden"] is False
        assert data[key]["finalSkelHidden"] is True
        assert data[key]["skelAriaBusy"] == "false"
        assert data[key]["headerH"] == "68px"
        assert "panel-header" in data[key]["resizeObserved"]

    assert data["globalRes"]["statusText"] == "Live"
    assert data["globalRes"]["liveStatusHasError"] is False
    assert data["sessionRes"]["statusText"] == "Live"
    assert data["sessionRes"]["liveStatusHasError"] is False
    assert data["errorRes"]["statusText"] == "Offline"
    assert data["errorRes"]["liveStatusHasError"] is True


def test_sidecar_quota_tabs_keyboard_and_aria_progressbars():
    """Verifies roving-tabindex keyboard nav on quota tabs and ARIA progressbar attributes."""
    import json
    import re
    import subprocess

    html = Path("sidecars/panel/index.html").read_text(encoding="utf-8")

    assert 'role="progressbar"' in html
    assert 'aria-valuemin="0"' in html
    assert 'aria-valuemax="100"' in html
    assert 'aria-controls="quota-buckets"' in html
    assert re.search(
        r'<div[^>]*id="quota-buckets"[^>]*role="tabpanel"|<div[^>]*role="tabpanel"[^>]*id="quota-buckets"',
        html,
    ), "index.html must wrap quota buckets in #quota-buckets with role=tabpanel"
    assert re.search(
        r'<button[^>]*id="quota-tab-gemini"[^>]*tabindex="0"',
        html,
    ), "#quota-tab-gemini must start with tabindex='0'"
    assert re.search(
        r'<button[^>]*id="quota-tab-3p"[^>]*tabindex="-1"',
        html,
    ), "#quota-tab-3p must start with tabindex='-1'"
    for track_id in ("quota-weekly-track", "quota-5h-track", "capacity-track"):
        assert re.search(
            rf'<div[^>]*id="{track_id}"[^>]*role="progressbar"[^>]*aria-valuemin="0"[^>]*aria-valuemax="100"',
            html,
        ), f"#{track_id} must declare role='progressbar' with aria-valuemin='0' and aria-valuemax='100'"

    node_script = """
    import fs from 'node:fs';

    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');
    const elements = {};
    const focusCalls = [];

    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = String(v); },
        getAttribute(k) { return this.attributes[k]; },
        focus() { focusCalls.push(id); },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) {
          if (!this.listeners[evt]) this.listeners[evt] = [];
          this.listeners[evt].push(fn);
        },
        dispatchEvent(evt, eventObj = {}) {
          for (const fn of (this.listeners[evt] || [])) fn(eventObj);
        }
      };
    }

    const ids = [
      'panel-header', 'live-status', 'status-dot', 'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'loading-skeleton', 'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-track', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-track', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-track', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];
    for (const id of ids) elements[id] = makeEl(id);

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      sidecar: {
        conversationId: 'conv-aria-1',
        fetch: async () => ({
          ok: true,
          json: async () => ({
            cid: 'conv-aria-1',
            model_name: 'Gemini 3.7 Flash',
            billed_cost: 2.50,
            pct: 15.6,
            active_tokens: 156000,
            max_tokens: 1000000,
            weekly_quota_pct: 0.5,
            categories: [],
            top_steps: [],
            quota: {
              prefix: 'gemini',
              weekly_remaining_pct: 84.6,
              five_hour_remaining_pct: 90.4,
              session_weekly_pct: 0.5,
              buckets: {
                'gemini-weekly': { remaining_pct: 84.6, session_impact_pct: 0.5, calibrated_capacity: 672.0 },
                'gemini-5h': { remaining_pct: 90.4, session_impact_pct: 1.2, calibrated_capacity: 236.7 },
                '3p-weekly': { remaining_pct: 61.2, session_impact_pct: 0.1, calibrated_capacity: 200.0 },
                '3p-5h': { remaining_pct: 77.8, session_impact_pct: 0.2, calibrated_capacity: 70.0 }
              }
            },
            fleet: {
              is_swarm: true,
              agent_count: 1,
              total_cost: 2.50,
              total_tokens: 156000,
              shielding_ratio: 1.0,
              nodes: [
                {
                  id: 'conv-aria-1',
                  model_name: 'Gemini 3.7 Flash',
                  billed_cost: 2.50,
                  active_tokens: 156000,
                  max_tokens: 1000000,
                  context_pct: 44.7,
                  weekly_quota_pct: 0.5,
                  role: 'Root Coordinator',
                  is_root: true
                }
              ]
            }
          })
        })
      }
    };

    const fn = new Function('window', 'document', 'fetch', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch);

    await new Promise((r) => setTimeout(r, 60));

    const initial = {
      weeklyValuenow: elements['quota-weekly-track'].getAttribute('aria-valuenow'),
      fiveHValuenow: elements['quota-5h-track'].getAttribute('aria-valuenow'),
      capacityValuenow: elements['capacity-track'].getAttribute('aria-valuenow'),
      geminiTabindex: elements['quota-tab-gemini'].getAttribute('tabindex'),
      threepTabindex: elements['quota-tab-3p'].getAttribute('tabindex'),
      fleetNodeHtml: elements['fleet-node-list'].children[0]?.innerHTML || ''
    };

    let prevented = 0;
    const mkKey = (key) => ({ key, preventDefault: () => { prevented++; } });

    // ArrowRight on gemini -> switches to 3p and focuses 3p
    elements['quota-tab-gemini'].dispatchEvent('keydown', mkKey('ArrowRight'));
    const afterRight = {
      geminiSelected: elements['quota-tab-gemini'].getAttribute('aria-selected'),
      geminiTabindex: elements['quota-tab-gemini'].getAttribute('tabindex'),
      threepSelected: elements['quota-tab-3p'].getAttribute('aria-selected'),
      threepTabindex: elements['quota-tab-3p'].getAttribute('tabindex'),
      weeklyValuenow: elements['quota-weekly-track'].getAttribute('aria-valuenow'),
      fiveHValuenow: elements['quota-5h-track'].getAttribute('aria-valuenow'),
      lastFocused: focusCalls[focusCalls.length - 1]
    };

    // ArrowLeft on 3p -> switches back to gemini and focuses gemini
    elements['quota-tab-3p'].dispatchEvent('keydown', mkKey('ArrowLeft'));
    const afterLeft = {
      geminiSelected: elements['quota-tab-gemini'].getAttribute('aria-selected'),
      geminiTabindex: elements['quota-tab-gemini'].getAttribute('tabindex'),
      threepSelected: elements['quota-tab-3p'].getAttribute('aria-selected'),
      threepTabindex: elements['quota-tab-3p'].getAttribute('tabindex'),
      lastFocused: focusCalls[focusCalls.length - 1]
    };

    // End -> switches to 3p and focuses 3p
    elements['quota-tab-gemini'].dispatchEvent('keydown', mkKey('End'));
    const afterEnd = {
      threepSelected: elements['quota-tab-3p'].getAttribute('aria-selected'),
      threepTabindex: elements['quota-tab-3p'].getAttribute('tabindex'),
      lastFocused: focusCalls[focusCalls.length - 1]
    };

    // Home -> switches to gemini and focuses gemini
    elements['quota-tab-3p'].dispatchEvent('keydown', mkKey('Home'));
    const afterHome = {
      geminiSelected: elements['quota-tab-gemini'].getAttribute('aria-selected'),
      geminiTabindex: elements['quota-tab-gemini'].getAttribute('tabindex'),
      lastFocused: focusCalls[focusCalls.length - 1],
      prevented
    };

    console.log(JSON.stringify({ initial, afterRight, afterLeft, afterEnd, afterHome }));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    initial = data["initial"]
    assert initial["weeklyValuenow"] == "85"
    assert initial["fiveHValuenow"] == "90"
    assert initial["capacityValuenow"] == "16"
    assert initial["geminiTabindex"] == "0"
    assert initial["threepTabindex"] == "-1"
    assert 'role="progressbar"' in initial["fleetNodeHtml"]
    assert 'aria-valuemin="0"' in initial["fleetNodeHtml"]
    assert 'aria-valuemax="100"' in initial["fleetNodeHtml"]
    assert 'aria-valuenow="45"' in initial["fleetNodeHtml"]

    after_right = data["afterRight"]
    assert after_right["geminiSelected"] == "false"
    assert after_right["geminiTabindex"] == "-1"
    assert after_right["threepSelected"] == "true"
    assert after_right["threepTabindex"] == "0"
    assert after_right["weeklyValuenow"] == "61"
    assert after_right["fiveHValuenow"] == "78"
    assert after_right["lastFocused"] == "quota-tab-3p"

    after_left = data["afterLeft"]
    assert after_left["geminiSelected"] == "true"
    assert after_left["geminiTabindex"] == "0"
    assert after_left["threepSelected"] == "false"
    assert after_left["threepTabindex"] == "-1"
    assert after_left["lastFocused"] == "quota-tab-gemini"

    assert data["afterEnd"]["threepSelected"] == "true"
    assert data["afterEnd"]["threepTabindex"] == "0"
    assert data["afterEnd"]["lastFocused"] == "quota-tab-3p"

    assert data["afterHome"]["geminiSelected"] == "true"
    assert data["afterHome"]["geminiTabindex"] == "0"
    assert data["afterHome"]["lastFocused"] == "quota-tab-gemini"
    assert data["afterHome"]["prevented"] == 4


def test_sidecar_bridge_url_fallback_workspace_change_and_sse_reconnect():
    """Verifies URL conversationId fallback, onWorkspaceChange reset, and SSE onerror Reconnecting badge."""
    import json
    import subprocess

    node_script = """
    import fs from 'node:fs';

    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    class MockEventSource {
      static instances = [];
      constructor(url) {
        this.url = url;
        this.listeners = {};
        this.closed = false;
        this.onerror = null;
        MockEventSource.instances.push(this);
      }
      addEventListener(event, fn) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(fn);
      }
      close() {
        this.closed = true;
      }
      emit(event, data = {}) {
        for (const fn of (this.listeners[event] || [])) {
          fn({ data: JSON.stringify(data) });
        }
      }
    }

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = String(v); },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) {
          if (!this.listeners[evt]) this.listeners[evt] = [];
          this.listeners[evt].push(fn);
        },
        dispatchEvent(evt, eventObj = {}) {
          for (const fn of (this.listeners[evt] || [])) fn(eventObj);
        }
      };
    }

    const ids = [
      'panel-header', 'live-status', 'status-dot', 'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'loading-skeleton', 'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-track', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-track', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-track', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];
    for (const id of ids) elements[id] = makeEl(id);

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const fetchCalls = [];
    const sentMessages = [];
    let workspaceChangeCb = null;

    const mockWindow = {
      EventSource: MockEventSource,
      location: { search: '?conversationId=conv-url-1&token=tok9' },
      sidecar: {
        // conversationId intentionally omitted (undefined) to test URL fallback
        onWorkspaceChange: (cb) => {
          workspaceChangeCb = cb;
          return () => {};
        },
        agent: {
          sendMessage: async (msg, cid) => {
            sentMessages.push({ msg, cid });
          }
        },
        fetch: async (url) => {
          fetchCalls.push(url);
          const isInspectingWorker = url.includes('inspect_cid=subagent-worker-1');
          return {
            ok: true,
            json: async () => ({
              cid: 'conv-url-1',
              model_name: isInspectingWorker ? 'Claude 3.7 Sonnet' : 'Gemini 3.7 Flash',
              billed_cost: 1.25,
              pct: 20.0,
              active_tokens: 200000,
              max_tokens: 1000000,
              weekly_quota_pct: 0.4,
              categories: [],
              top_steps: [],
              quota: {
                prefix: 'gemini',
                weekly_remaining_pct: 85.0,
                five_hour_remaining_pct: 90.0,
                session_weekly_pct: 0.4,
                buckets: {
                  'gemini-weekly': { remaining_pct: 85.0, session_impact_pct: 0.4, calibrated_capacity: 672.0 },
                  'gemini-5h': { remaining_pct: 90.0, session_impact_pct: 1.0, calibrated_capacity: 236.7 },
                  '3p-weekly': { remaining_pct: 95.0, session_impact_pct: 0.0, calibrated_capacity: 200.0 },
                  '3p-5h': { remaining_pct: 98.0, session_impact_pct: 0.0, calibrated_capacity: 70.0 }
                }
              },
              fleet: {
                is_swarm: true,
                root_id: 'conv-url-1',
                active_cid: 'conv-url-1',
                inspected_cid: isInspectingWorker ? 'subagent-worker-1' : 'conv-url-1',
                is_inspecting_other: isInspectingWorker,
                inspected_role: isInspectingWorker ? 'Worker' : 'Root Coordinator',
                agent_count: 2,
                total_cost: 2.50,
                total_tokens: 300000,
                weekly_quota_pct: 0.8,
                shielding_ratio: 1.5,
                nodes: [
                  {
                    id: 'subagent-worker-1',
                    model_name: 'Claude 3.7 Sonnet',
                    billed_cost: 1.25,
                    active_tokens: 100000,
                    max_tokens: 200000,
                    context_pct: 50.0,
                    weekly_quota_pct: 0.4,
                    role: 'Worker',
                    is_root: false
                  },
                  {
                    id: 'conv-url-1',
                    model_name: 'Gemini 3.7 Flash',
                    billed_cost: 1.25,
                    active_tokens: 200000,
                    max_tokens: 1000000,
                    context_pct: 20.0,
                    weekly_quota_pct: 0.4,
                    role: 'Root Coordinator',
                    is_root: true
                  }
                ]
              }
            })
          };
        }
      }
    };

    const fn = new Function('window', 'document', 'EventSource', 'fetch', code);
    fn(mockWindow, mockDocument, MockEventSource, mockWindow.sidecar.fetch);

    await new Promise((r) => setTimeout(r, 60));

    // Case A: URL fallback for conversationId + prompt dispatch
    const caseA = {
      resolvedCid: typeof mockWindow.getCurrentConversationId === 'function' ? mockWindow.getCurrentConversationId() : null,
      initialFetch: fetchCalls[0] || null,
      initialSseUrl: MockEventSource.instances[0]?.url || null
    };
    elements['btn-audit'].dispatchEvent('click');
    await new Promise((r) => setTimeout(r, 30));
    caseA.sentMessages = [...sentMessages];

    // Case B: Inspect subagent + toggle 3P tab, then trigger onWorkspaceChange
    mockWindow.selectFleetNode('subagent-worker-1');
    await new Promise((r) => setTimeout(r, 60));
    elements['quota-tab-3p'].dispatchEvent('click');
    const beforeWsReset = {
      inspectBannerHidden: elements['inspect-banner'].hidden,
      threepSelected: elements['quota-tab-3p'].getAttribute('aria-selected'),
      fetchBeforeReset: fetchCalls[fetchCalls.length - 1]
    };

    const fetchCountBeforeWs = fetchCalls.length;
    if (typeof workspaceChangeCb === 'function') {
      workspaceChangeCb();
    }
    await new Promise((r) => setTimeout(r, 60));

    const activeES = MockEventSource.instances[MockEventSource.instances.length - 1];
    const caseB = {
      hasWorkspaceCallback: typeof workspaceChangeCb === 'function',
      beforeWsReset,
      newFetchesAfterReset: fetchCalls.slice(fetchCountBeforeWs),
      inspectBannerHiddenAfterReset: elements['inspect-banner'].hidden,
      geminiSelectedAfterReset: elements['quota-tab-gemini'].getAttribute('aria-selected'),
      threepSelectedAfterReset: elements['quota-tab-3p'].getAttribute('aria-selected'),
      activeSseUrlAfterReset: activeES?.url || null
    };

    // Case C: SSE onerror -> Reconnecting / badge warning / stale, then 'update' restores Live / badge ok
    if (activeES && typeof activeES.onerror === 'function') {
      activeES.onerror();
    }
    const duringError = {
      statusText: elements['status-badge'].textContent,
      statusClass: elements['status-badge'].className,
      liveStale: elements['live-status'].classList.contains('stale')
    };

    activeES.emit('update', { cid: 'conv-url-1' });
    await new Promise((r) => setTimeout(r, 60));
    const afterRecover = {
      statusText: elements['status-badge'].textContent,
      statusClass: elements['status-badge'].className,
      liveStale: elements['live-status'].classList.contains('stale')
    };

    console.log(JSON.stringify({ caseA, caseB, caseC: { duringError, afterRecover } }));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    case_a = data["caseA"]
    assert case_a["resolvedCid"] == "conv-url-1"
    assert case_a["initialFetch"] == "/api/session?cid=conv-url-1"
    assert "cid=conv-url-1" in case_a["initialSseUrl"]
    assert "token=tok9" in case_a["initialSseUrl"]
    assert len(case_a["sentMessages"]) == 1
    assert case_a["sentMessages"][0]["cid"] == "conv-url-1"

    case_b = data["caseB"]
    assert case_b["hasWorkspaceCallback"] is True
    assert case_b["beforeWsReset"]["inspectBannerHidden"] is False
    assert case_b["beforeWsReset"]["threepSelected"] == "true"
    assert "inspect_cid=subagent-worker-1" in case_b["beforeWsReset"]["fetchBeforeReset"]
    assert len(case_b["newFetchesAfterReset"]) >= 1
    assert all("inspect_cid" not in u for u in case_b["newFetchesAfterReset"])
    assert case_b["inspectBannerHiddenAfterReset"] is True
    assert case_b["geminiSelectedAfterReset"] == "true"
    assert case_b["threepSelectedAfterReset"] == "false"
    assert "inspect_cid" not in case_b["activeSseUrlAfterReset"]

    case_c = data["caseC"]
    assert case_c["duringError"]["statusText"] == "Reconnecting"
    assert case_c["duringError"]["statusClass"] == "badge warning"
    assert case_c["duringError"]["liveStale"] is True
    assert case_c["afterRecover"]["statusText"] == "Live"
    assert case_c["afterRecover"]["statusClass"] == "badge ok"
    assert case_c["afterRecover"]["liveStale"] is False






