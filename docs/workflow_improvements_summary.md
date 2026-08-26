# Antigravity Context Monitor & UI Workflow Improvements
## Findings & Ideas Summary

This document summarizes findings, workflows, and architectural ideas derived from benchmarking against CC Glass ([jianshuo/ccglass](https://github.com/jianshuo/ccglass)) and implementing post-response transcript tracking with the Antigravity Context Monitor.

### 1. Architecture & Inspection Comparison (CC Glass vs. Antigravity Context Monitor)
**Findings**:
- **Ingestion Strategy**: CC Glass intercepts wire traffic using an HTTP reverse-proxy (`OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`). `agy-context-monitor` leverages Antigravity's native `PostInvocation` hook and `transcript.jsonl` logs, requiring zero proxy configuration and introducing zero latency overhead.
- **Data Persistence**: CC Glass operates on ephemeral sessions stored in memory/files per proxy run. `agy-context-monitor` centralizes all conversations into a local SQLite database in WAL mode (`analytics.db`), enabling multi-session velocity tracking and aggregate analytics.
- **Granular Inspection**: CC Glass excels at turn-by-turn wire payload transparency (tool schemas, paired tool calls, and turn-to-turn prompt diffs).

**Workflows & Ideas**:
- **Agent-Loop Flow View**: Pair `tool_use` steps with their corresponding `tool_result` steps by `call_id` and apply consistent color hues to visualize the iterative agent decision loop.
- **Turn-to-Turn Context Diffing**: Hash canonical content blocks (`system`, `messages`, `tools`) with SHA-1 to compute additions, removals, and cache boundaries between consecutive turns.
- **Multi-Tab Step Inspector**: Provide dedicated tabs for Overview, Agent Flow, System Prompts, Messages, Tool Schemas, Response, and cURL Replay.

### 2. Context Monitor Architecture & Mechanics
**Findings**:
- **Ecosystem**: Built as a plugin (`~/.gemini/config/plugins/context-monitor/`) with hooks (`hooks.json`), backend modules (`server.py`, `api_routes.py`, `sse_stream.py`, `analytics_query.py`), and a local SQLite database (`analytics.db`).
- **Database**: Uses SQLite in WAL mode (`PRAGMA journal_mode = WAL;`) for safe concurrent writes. Tracks conversations, granular step executions, tool executions, and hot source files.
- **Billing & Token Models**: Dynamic step-level tracking detects mid-conversation model changes to calculate accurate cumulative API billing and distinguish standard tokens from reasoning/thinking tokens.
- **Velocity Metrics**: Calculates "Autonomy Multiplier" (tool invocations per user prompt) and "Estimated Developer Hours Saved" based on automated file searches, shell runs, and code edits.

**Workflows & Ideas**:
- **Event-Driven Architecture (SSE)**: The web dashboard uses Server-Sent Events (SSE) via `DbChangeWatcher` for zero-polling real-time updates when SQLite records change.
- **Standalone Exports**: The dashboard can be exported as a 100% self-contained, privacy-safe offline HTML file, stripping all SSE code and bundling SQLite data as inline JavaScript.

### 3. Key Invariants & Rules
- **Clean Markdown**: Keep markdown reports clean and professional. Avoid decorative icons and emojis in files like `context_usage.md` and `global_analytics.md`.
- **Zero Polling**: Web UI clients must rely exclusively on SSE pushes. No periodic polling loops when idle.
- **Context Preservation**: Post-invocation scripts must run silently if they are not explicitly injecting ephemeral messages, ensuring zero context window pollution.
- **Performance**: Disable animations globally in charting libraries to prevent visual jumping during rapid SSE pushes.
