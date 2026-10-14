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

__all__ = [
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_GEMINI_WEEKLY_CAPACITY",
    "DEFAULT_GEMINI_5H_CAPACITY",
    "DEFAULT_3P_WEEKLY_CAPACITY",
    "DEFAULT_3P_5H_CAPACITY",
    "QUOTA_CACHE_TTL_SECONDS",
    "QUOTA_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_SETTINGS",
    "get_settings_path",
    "get_settings",
    "get_gemini_weekly_capacity",
    "get_gemini_5h_capacity",
    "get_3p_weekly_capacity",
    "get_3p_5h_capacity",
    "get_quota_cache_ttl",
    "get_quota_request_timeout",
    "save_settings",
    "should_generate_artifacts",
]

DEFAULT_SETTINGS_PATH = Path(os.path.expanduser("~/.gemini/antigravity/context_monitor_settings.json"))

DEFAULT_GEMINI_WEEKLY_CAPACITY = 672.0
DEFAULT_GEMINI_5H_CAPACITY = 236.7
DEFAULT_3P_WEEKLY_CAPACITY = 200.0
DEFAULT_3P_5H_CAPACITY = 70.0
QUOTA_CACHE_TTL_SECONDS = 60
QUOTA_REQUEST_TIMEOUT_SECONDS = 1.5

DEFAULT_SETTINGS: dict[str, Any] = {
    "generate_artifacts": False,
    "subscription_monthly_price": 200.0,
    "default_weekly_capacity_gemini": DEFAULT_GEMINI_WEEKLY_CAPACITY,
    "default_5h_capacity_gemini": DEFAULT_GEMINI_5H_CAPACITY,
    "default_weekly_capacity_3p": DEFAULT_3P_WEEKLY_CAPACITY,
    "default_5h_capacity_3p": DEFAULT_3P_5H_CAPACITY,
    "quota_cache_ttl_seconds": QUOTA_CACHE_TTL_SECONDS,
    "quota_request_timeout_seconds": QUOTA_REQUEST_TIMEOUT_SECONDS,
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
        _apply_env_overrides(settings)
        return settings

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                settings.update(data)
    except Exception:
        # Graceful fallback on malformed JSON
        pass

    _apply_env_overrides(settings)
    return settings


def _apply_env_overrides(settings: dict[str, Any]) -> None:
    """Applies environment variable overrides to active settings dictionary."""
    def _get_env(*keys: str) -> str | None:
        for k in keys:
            if k in os.environ:
                return os.environ[k]
        return None

    val = _get_env("AGY_GEMINI_WEEKLY_CAPACITY", "CONTEXT_MONITOR_GEMINI_WEEKLY_CAPACITY")
    if val is not None:
        try:
            settings["default_weekly_capacity_gemini"] = float(val)
        except ValueError:
            pass

    val = _get_env("AGY_GEMINI_5H_CAPACITY", "CONTEXT_MONITOR_GEMINI_5H_CAPACITY")
    if val is not None:
        try:
            settings["default_5h_capacity_gemini"] = float(val)
        except ValueError:
            pass

    val = _get_env("AGY_3P_WEEKLY_CAPACITY", "CONTEXT_MONITOR_3P_WEEKLY_CAPACITY")
    if val is not None:
        try:
            settings["default_weekly_capacity_3p"] = float(val)
        except ValueError:
            pass

    val = _get_env("AGY_3P_5H_CAPACITY", "CONTEXT_MONITOR_3P_5H_CAPACITY")
    if val is not None:
        try:
            settings["default_5h_capacity_3p"] = float(val)
        except ValueError:
            pass

    val = _get_env("AGY_QUOTA_CACHE_TTL_SECONDS", "CONTEXT_MONITOR_QUOTA_CACHE_TTL_SECONDS")
    if val is not None:
        try:
            settings["quota_cache_ttl_seconds"] = int(val)
        except ValueError:
            pass

    val = _get_env("AGY_QUOTA_REQUEST_TIMEOUT_SECONDS", "CONTEXT_MONITOR_QUOTA_REQUEST_TIMEOUT_SECONDS")
    if val is not None:
        try:
            settings["quota_request_timeout_seconds"] = float(val)
        except ValueError:
            pass


def get_gemini_weekly_capacity() -> float:
    """Returns the calibrated/default Gemini weekly capacity in compute units (USD)."""
    return float(get_settings().get("default_weekly_capacity_gemini", DEFAULT_GEMINI_WEEKLY_CAPACITY))


def get_gemini_5h_capacity() -> float:
    """Returns the calibrated/default Gemini 5-hour rolling capacity in compute units (USD)."""
    return float(get_settings().get("default_5h_capacity_gemini", DEFAULT_GEMINI_5H_CAPACITY))


def get_3p_weekly_capacity() -> float:
    """Returns the default 3P (Claude/GPT) weekly capacity in compute units (USD)."""
    return float(get_settings().get("default_weekly_capacity_3p", DEFAULT_3P_WEEKLY_CAPACITY))


def get_3p_5h_capacity() -> float:
    """Returns the default 3P (Claude/GPT) 5-hour rolling capacity in compute units (USD)."""
    return float(get_settings().get("default_5h_capacity_3p", DEFAULT_3P_5H_CAPACITY))


def get_quota_cache_ttl() -> int:
    """Returns the TTL in seconds for the SQLite quota state cache."""
    return int(get_settings().get("quota_cache_ttl_seconds", QUOTA_CACHE_TTL_SECONDS))


def get_quota_request_timeout() -> float:
    """Returns the HTTP request timeout in seconds for Language Server quota RPC calls."""
    return float(get_settings().get("quota_request_timeout_seconds", QUOTA_REQUEST_TIMEOUT_SECONDS))


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
