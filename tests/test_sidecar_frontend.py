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
    assert "visibilitychange" in js, "Must guard polling with visibility change listener"
    assert "sidecar.agent.sendMessage" in js or "agent.sendMessage" in js
