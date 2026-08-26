# Antigravity Context Monitor & UI Workflow Improvements
## Findings & Ideas Summary

This document summarizes findings, workflows, and architectural ideas derived from recent conversations regarding AI workflow improvements (specifically comparing Antigravity to CanvasMind, and implementing Post-Response Chat Scripts with a Context Monitor).

### 1. UI Presentation & Integration Gaps (CanvasMind vs. Antigravity)
**Findings**:
- **The UI Gap**: CanvasMind utilizes native GUI widgets (like circular progress rings and floating tool panels) to display context usage. Antigravity relies on Markdown and Artifacts. 
- **The Bridge**: While native widgets aren't directly injectable in Antigravity, similar functionality can be achieved using the `PostInvocation` hook without altering core Antigravity source code.

**Workflows & Ideas**:
- **Ephemeral Messages**: Use `ephemeralMessage` in `injectSteps` to output transient text-based progress bars (e.g., `[███████░░░] 70%`) directly in the chat stream without polluting the LLM's prompt history.
- **Auto-Updating Dashboards**: Write rich reports (using Mermaid.js charts, tables) to a markdown file (e.g., `context_report.md`) in the `artifactDirectoryPath`. The Artifacts pane automatically watches this directory, acting as a real-time dashboard.

### 2. Context Monitor Architecture & Mechanics
**Findings (from Project Memory)**:
- **Ecosystem**: Built as a plugin (`~/.gemini/config/plugins/context-monitor/`) with hooks (`post_invocation.json`), backend scripts (`show_usage.py`, `dashboard.py`, `analytics_query.py`), and a local SQLite database (`analytics.db`).
- **Database**: Uses SQLite in WAL mode (`PRAGMA journal_mode = WAL;`) for safe concurrent access. Tracks conversations, granular step-by-step executions, and individual tool executions (including MCP tools).
- **Billing & Token Models**: Dynamic step-level tracking detects mid-conversation model changes to calculate accurate cumulative API billing and distinguish between standard tokens and thinking tokens.
- **Velocity Metrics**: Calculates "Autonomy Multiplier" (tool invocations per user prompt) and "Estimated Developer Hours Saved" based on automated file searches, shell runs, and code edits.

**Workflows & Ideas**:
- **Event-Driven Architecture (SSE)**: The web dashboard uses Server-Sent Events (SSE) via `DbChangeWatcher` for zero-polling real-time updates when the database changes. 
- **Standalone Exports**: The dashboard can be exported as a 100% self-contained, privacy-safe offline HTML file, stripping all SSE code and bundling SQLite data as inline JavaScript.

### 3. Key Invariants & Rules
- **No Decorative Icons**: Keep markdown reports clean and professional. Avoid emojis in files like `context_usage.md`.
- **Zero Polling**: Web UI clients must rely on SSE pushes. No periodic polling when idle.
- **Context Preservation**: Post-invocation scripts must run silently if they are not explicitly injecting ephemeral messages, ensuring zero context window pollution.
- **Performance**: Disable animations globally in charting libraries to prevent visual jumping during rapid SSE pushes.
