import json
import os
import re
import select
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def test_plugin_manifest():
    plugin_path = Path("plugin.json")
    assert plugin_path.exists(), "plugin.json must exist"
    data = json.loads(plugin_path.read_text())
    assert data.get("name") == "context-monitor"
    assert data.get("logo") == "assets/logo.svg"
    assert Path(data["logo"]).exists(), "logo referenced in plugin.json must exist"


def test_sidecar_manifest():
    sidecar_json = Path("sidecars/panel/sidecar.json")
    assert sidecar_json.exists(), "sidecars/panel/sidecar.json must exist"
    data = json.loads(sidecar_json.read_text())
    assert data.get("command") == "node"
    assert data.get("args") == ["main.mjs"]
    assert data.get("has_web_ui") is True
    assert data.get("restart_policy") == "always"
    assert "ui_config" in data
    views = data["ui_config"].get("views", [])
    assert len(views) > 0
    assert views[0].get("entrypoint") == "SIDECAR_UI_ENTRYPOINT_AUX_PANE"
    assert views[0].get("path") == "/"


def test_sidecar_package():
    pkg_json = Path("sidecars/panel/package.json")
    assert pkg_json.exists(), "sidecars/panel/package.json must exist"
    data = json.loads(pkg_json.read_text())
    assert data.get("type") == "module"
    assert data.get("private") is True


def test_sidecar_standalone_dev_server(tmp_path: Path):
    env = os.environ.copy()
    env.pop("ANTIGRAVITY_SIDECAR_WEB_PORT", None)
    env.pop("AGY_ANALYTICS_DB", None)
    env["PORT"] = "0"
    env["CONTEXT_MONITOR_DB"] = str(tmp_path / "empty.db")

    proc = subprocess.Popen(
        ["node", "sidecars/panel/main.mjs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=Path.cwd(),
    )
    try:
        assert proc.stdout is not None
        base_url = None
        deadline = time.monotonic() + 10.0
        stdout_lines: list[str] = []
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            stdout_lines.append(line)
            match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
            if match:
                base_url = f"http://127.0.0.1:{match.group(1)}"
                break

        if base_url is None:
            stderr_out = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"Standalone server failed to bind: returncode={proc.poll()}, "
                f"stdout={''.join(stdout_lines)!r}, stderr={stderr_out!r}"
            )

        with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            html = resp.read().decode("utf-8")
            assert "<title>Context Monitor</title>" in html

        with urllib.request.urlopen(f"{base_url}/styles.css", timeout=5) as resp:
            assert resp.status == 200
            assert "text/css" in resp.headers.get("Content-Type", "")
            css = resp.read().decode("utf-8")
            assert "--ui-bg" in css

        with urllib.request.urlopen(f"{base_url}/app.js", timeout=5) as resp:
            assert resp.status == 200
            assert "javascript" in resp.headers.get("Content-Type", "")
            js = resp.read().decode("utf-8")
            assert "refreshTelemetry" in js

        with urllib.request.urlopen(f"{base_url}/preload.js", timeout=5) as resp:
            assert resp.status == 200
            assert "javascript" in resp.headers.get("Content-Type", "")
            preload = resp.read().decode("utf-8")
            assert "window.sidecar" in preload
            assert "getWorkspaceUris" in preload
            assert "onWorkspaceChange" in preload

        with urllib.request.urlopen(f"{base_url}/api/global", timeout=5) as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            global_data = json.loads(resp.read().decode("utf-8"))
            assert global_data["total_conversations"] == 0
            assert "total_tokens" in global_data
            assert "quota" in global_data

        with urllib.request.urlopen(f"{base_url}/api/session?cid=test-cid", timeout=5) as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            session_data = json.loads(resp.read().decode("utf-8"))
            assert session_data["cid"] == "test-cid"
            assert "pct" in session_data
            assert "active_tokens" in session_data
            assert "quota" in session_data

        with urllib.request.urlopen(f"{base_url}/api/events?cid=test-cid", timeout=5) as resp:
            assert resp.status == 200
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            first_line = resp.readline().decode("utf-8")
            assert first_line.startswith(": connected")

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/nonexistent", timeout=5)
        assert exc_info.value.code == 404
        exc_info.value.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
