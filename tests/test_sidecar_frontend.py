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

    # App JS checks: Sidecar API, multi-segment, global mode, quota, and lifecycle guards
    assert "window.sidecar" in js
    assert "/api/session" in js
    assert "/api/global" in js
    assert "renderMultiSegmentBar" in js
    assert "renderQuota" in js
    assert "quota-weekly-pct" in js
    assert "quota-tab-gemini" in js
    assert "visibilitychange" in js, (
        "Must guard polling with visibility change listener"
    )
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
        weeklyPct: elements['quota-weekly-pct'].textContent,
        weeklyPctClass: elements['quota-weekly-pct'].className,
        weeklyBarWidth: elements['quota-weekly-bar'].style.width,
        weeklyBarClass: elements['quota-weekly-bar'].className,
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
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

    assert data["weeklyPct"] == "85.0% remaining"
    assert "ok" in data["weeklyBarClass"]
    assert data["weeklyBarWidth"] == "85%"
    assert data["sessionImpact"] == "1.00% session share"
    assert data["quotaBadge"] == "1.00% wk"
    assert data["geminiTabSelected"] == "true"
    assert data["threepTabSelected"] == "false"

    # After clicking 3P tab
    after = data["afterClick3p"]
    assert after["weeklyPct"] == "95.0% remaining"
    assert "$200.00" in after["weeklyCapSub"]
    assert after["sessionImpact"] == "0.00% session share"
    assert after["quotaBadge"] == "1.00% wk"
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
        sessionImpact: elements['quota-session-impact'].textContent,
        quotaBadge: elements['quota-badge'].textContent,
        globalQuota: elements['global-quota'].textContent
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

    assert "fleet total" in data["sessionImpact"]
    assert "fleet" in data["quotaBadge"]
    assert "2.00% wk" in data["globalQuota"]
