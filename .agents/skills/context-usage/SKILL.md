---
name: context-usage
description: Audits conversation context window utilization, estimated token consumption, and identifies largest context contributors in Antigravity. Activate when the user asks "check context", "how much context is used", "context breakdown", or invokes /context-usage.
---

# Context Usage Auditor

Provides an in-depth audit of active Antigravity session context consumption, token breakdowns, and largest context contributors.

## When to Run

Activate this skill when the user asks for:
- Current context or token consumption breakdown.
- Identifying what is consuming the most context (e.g. large file reads, web search results, planner thinking).
- Advice on context window management or session resets.

## How to Audit Context Usage

1. **Locate the Current Conversation Transcript**:
   The active transcript is located at:
   `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`

2. **Native Side Panel UI (Primary Interface)**:
   The `context-monitor` plugin provides a native Antigravity UI Extension in the side panel:
   - Surface the one-click navigation pill:
     `[Context Monitor](sidecar://context-monitor/panel/)`
   The side panel renders live capacity bars, category breakdowns, memory compaction boundaries, and one-click agent action controls directly in Antigravity's auxiliary pane without polluting conversation context.

3. **Legacy Artifact Reports (Deprecated & Default-Off)**:
   Disk artifact files (`context_usage.md` and `context_usage.html`) are **deprecated** and turned off by default to prevent file churn in brain directories.
   - To re-enable legacy artifact generation, set `"generate_artifacts": true` in `~/.gemini/antigravity/context_monitor_settings.json` or export `CONTEXT_MONITOR_GENERATE_ARTIFACTS=1`.
   - When enabled, `<agent-embed src="file:///<appDataDir>/brain/<conversation-id>/context_usage.html"></agent-embed>` can be embedded inline.

4. **Presenting Findings to User (Zero Context Bloat)**:
   To protect the conversation context window from being consumed by large diagnostic tables:
   - Provide a concise 1–2 sentence headline with the active percentage, model limit, and session cost:
     `Active Context: **{pct:.1f}%** ({active_tokens} / {max_tokens} tokens) · Model: `{model}` · Value: ${cost}`
   - Provide the one-click navigation pill to view full telemetry in the side panel:
     `[Context Monitor](sidecar://context-monitor/panel/)`
   - **Do NOT** print verbose multi-column markdown tables into the chat response.
