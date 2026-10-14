from pathlib import Path

def test_navigation_pill_in_skill_doc():
    skill_path = Path(".agents/skills/context-usage/SKILL.md")
    assert skill_path.exists(), "SKILL.md must exist"
    content = skill_path.read_text()
    assert "[Context Monitor](sidecar://context-monitor/panel/)" in content, (
        "SKILL.md must document the native side panel navigation pill link"
    )

def test_navigation_pill_in_ingest_report():
    ingest_path = Path("scripts/ingest.py")
    assert ingest_path.exists(), "scripts/ingest.py must exist"
    content = ingest_path.read_text()
    assert "[Context Monitor](sidecar://context-monitor/panel/)" in content, (
        "scripts/ingest.py must generate report footer with the side panel navigation pill link"
    )

def test_skill_documents_artifact_deprecation():
    skill_path = Path(".agents/skills/context-usage/SKILL.md")
    assert skill_path.exists(), "SKILL.md must exist"
    content = skill_path.read_text()
    assert "deprecated" in content.lower(), "SKILL.md must state that artifact files are deprecated"
    assert "context_monitor_settings.json" in content, "SKILL.md must document settings JSON file location"
