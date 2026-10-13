"""
Antigravity Context Monitor Package
-----------------------------------
Real-time context window usage, token diagnostics, telemetry, and diff engine.
"""

from context_monitor.db import DEFAULT_DB_PATH, get_db_connection, init_db
from context_monitor.diff import DiffResult, diff_block_lists, get_turn_diff_from_db
from context_monitor.formats import BlockParser, BlockType, ContentBlock
from context_monitor.html_report import generate_html_report

__all__ = [
    "DEFAULT_DB_PATH",
    "get_db_connection",
    "init_db",
    "BlockParser",
    "BlockType",
    "ContentBlock",
    "DiffResult",
    "diff_block_lists",
    "get_turn_diff_from_db",
    "generate_html_report",
]
