"""
Antigravity Context Monitor Settings Module
-------------------------------------------
Manages persistent configuration for the context-monitor plugin.
Default configuration file: ~/.gemini/antigravity/context_monitor_settings.json
"""

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = Path(os.path.expanduser("~/.gemini/antigravity/context_monitor_settings.json"))

DEFAULT_SETTINGS: dict[str, Any] = {
    "generate_artifacts": False,
}


def get_settings_path() -> Path:
    """Returns the active settings file path, respecting environment overrides."""
    env_path = os.environ.get("AGY_SETTINGS_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_SETTINGS_PATH


def get_settings() -> dict[str, Any]:
    """
    Loads persistent settings from disk.
    Returns defaults (generate_artifacts=False) if file does not exist or is invalid.
    """
    path = get_settings_path()
    settings = dict(DEFAULT_SETTINGS)

    if not path.exists():
        return settings

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                settings.update(data)
    except Exception:
        # Graceful fallback on malformed JSON
        pass

    return settings


def save_settings(updates: dict[str, Any]) -> None:
    """
    Saves updated settings to disk, merging with existing values.
    Creates parent directories if necessary.
    """
    current = get_settings()
    current.update(updates)

    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.write("\n")


def should_generate_artifacts() -> bool:
    """
    Determines whether legacy artifact reports (context_usage.md and context_usage.html)
    should be written to disk.

    Resolution:
    1. Environment variable CONTEXT_MONITOR_GENERATE_ARTIFACTS (1/0, true/false)
    2. File setting 'generate_artifacts' in context_monitor_settings.json
    3. Default: False (artifacts deprecated in favor of native UI extension)
    """
    env_override = os.environ.get("CONTEXT_MONITOR_GENERATE_ARTIFACTS")
    if env_override is not None:
        val = env_override.strip().lower()
        return val in ("1", "true", "yes", "on")

    return bool(get_settings().get("generate_artifacts", False))
