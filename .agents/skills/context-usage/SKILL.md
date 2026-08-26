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

2. **Check the Live Artifact Report**:
   If the `context-monitor` plugin is active, view the generated report at:
   `<appDataDir>/brain/<conversation-id>/context_usage.md`

3. **Presenting Findings to User**:
   - Total Estimated Tokens vs Model Window Limit (e.g. Gemini 3.7 Flash: 1.0M tokens, Gemini Pro: 2.0M tokens).
   - Percentage of context window consumed.
   - Breakdown by source (Model, System, User) and step type (`VIEW_FILE`, `RUN_COMMAND`, `PLANNER_RESPONSE`, `SEARCH_WEB`).
   - Top 3–5 largest context contributors with step indices.
