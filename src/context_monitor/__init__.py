"""
Antigravity Context Monitor Package
-----------------------------------
Real-time context window usage, token diagnostics, telemetry, and diff engine.
"""

from context_monitor.db import (
    DEFAULT_DB_PATH,
    get_db_connection,
    get_quota_state,
    init_db,
    upsert_quota_state,
)
from context_monitor.diff import DiffResult, diff_block_lists, get_turn_diff_from_db
from context_monitor.formats import BlockParser, BlockType, ContentBlock
from context_monitor.html_report import generate_html_report
from context_monitor.quota import (
    CAPACITY_BOUNDS,
    LAPLACE_ALPHA,
    MIN_PROFILE_STEPS,
    QuotaStateList,
    calculate_quota_metrics,
    calculate_quota_pacing,
    calibrate_capacity,
    discover_language_server,
    discover_language_server_credentials,
    fetch_live_quota_from_ls,
    fetch_raw_quota_summary,
    format_quota_pct,
    format_reset_countdown,
    get_capacity_bounds,
    get_hourly_activity_profile,
    invalidate_activity_profile_cache,
    normalize_activity_profile,
    sync_and_get_quota_state,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "get_db_connection",
    "init_db",
    "upsert_quota_state",
    "get_quota_state",
    "BlockParser",
    "BlockType",
    "ContentBlock",
    "DiffResult",
    "diff_block_lists",
    "get_turn_diff_from_db",
    "generate_html_report",
    "QuotaStateList",
    "calculate_quota_metrics",
    "calculate_quota_pacing",
    "calibrate_capacity",
    "discover_language_server",
    "discover_language_server_credentials",
    "fetch_live_quota_from_ls",
    "fetch_raw_quota_summary",
    "format_quota_pct",
    "format_reset_countdown",
    "sync_and_get_quota_state",
    "CAPACITY_BOUNDS",
    "get_capacity_bounds",
    "MIN_PROFILE_STEPS",
    "LAPLACE_ALPHA",
    "get_hourly_activity_profile",
    "invalidate_activity_profile_cache",
    "normalize_activity_profile",
]
