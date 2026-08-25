---
name: analytics
description: Queries aggregate developer productivity, token consumption, context efficiency diagnostics, developer velocity, hot code files, and tool analytics across all past and active Antigravity conversations. Trigger when the user asks "show global analytics", "how many tokens have I used across all chats", "show tool usage stats", "which files do I edit most", "context efficiency", "developer velocity", or invokes /analytics.
---

# Antigravity Global Analytics & Intelligence

Queries the centralized SQLite database (`~/.gemini/antigravity/analytics.db`) to provide high-level productivity analytics, developer velocity metrics, context health and efficiency diagnostics, token usage trends, hot code files, and tool statistics across all conversations. Includes both a CLI query tool and an interactive web-based GUI studio.

## 1. Web-Based GUI Dashboard

Launch the interactive real-time web dashboard (starts on `http://localhost:8765`):
```bash
uv run ~/.gemini/config/plugins/context-monitor/scripts/dashboard.py --open
```

Or export a standalone offline HTML dashboard file:
```bash
uv run ~/.gemini/config/plugins/context-monitor/scripts/dashboard.py --export-html dashboard.html
```

## 2. CLI Analytics Queries

Run targeted CLI queries:

1. **Overall System Summary**:
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --summary
   ```

2. **Context Health & Efficiency Diagnostics** (Redundant re-reads, wasted tokens, phase distribution, bloat culprits):
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --efficiency
   ```

3. **Developer Velocity & Autonomy Multiplier** (Autonomy leverage ratio, deep reasoning tokens, lines authored, time saved):
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --velocity
   ```

4. **Model Context Protocol (MCP) Server & Tool Ecosystem**:
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --mcp
   ```

5. **Top Accessed & Modified Files ("Hot Files")**:
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --hot-files
   ```

6. **Tool Execution Frequency & Rankings** (Supports `--mcp-only` and `--native-only`):
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --tools
   ```

7. **Activity Timeline (e.g. Last 14 Days)**:
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --timeline 14
   ```

8. **Generate Global Markdown Report**:
   ```bash
   uv run ~/.gemini/config/plugins/context-monitor/scripts/analytics_query.py --report
   ```

## 3. Back-Filling Historical Sessions

If new offline conversations need to be ingested or re-indexed:
```bash
uv run ~/.gemini/config/plugins/context-monitor/scripts/backfill_analytics.py
```
