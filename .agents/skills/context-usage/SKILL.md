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
   The `context-monitor` plugin automatically generates both:
   - Clean data report: `<appDataDir>/brain/<conversation-id>/context_usage.md`
   - Generative UI companion card: `<appDataDir>/brain/<conversation-id>/context_usage.html`

3. **Presenting Findings to User (Zero Context Bloat)**:
   To protect the conversation context window from being consumed by large diagnostic tables:
   - Provide a concise 1–2 sentence headline with the active percentage, model limit, and session cost:
     `Active Context: **{pct:.1f}%** ({active_tokens} / {max_tokens} tokens) · Model: `{model}` · Value: ${cost}`
   - Embed the interactive Generative UI card directly inline:
     ```html
     <agent-embed src="file:///<appDataDir>/brain/<conversation-id>/context_usage.html"></agent-embed>
     ```
   - Surface the one-click native side panel navigation pill:
     `[Context Monitor](sidecar://context-monitor/panel/)`
   - **Do NOT** print verbose multi-column markdown tables into the chat response. The inline `<agent-embed>` widget natively presents the multi-segment capacity bar, category breakdown, memory compaction boundary, and interactive action chips without polluting the persistent conversation transcript.
