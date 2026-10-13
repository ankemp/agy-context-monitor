import json
from pathlib import Path

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
