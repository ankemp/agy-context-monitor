# agy-context-monitor Agent Rules

This plugin provides real-time context window usage, estimated tokens, and capacity tracking.

## Core Invariants

These invariants protect the system's performance and the agent's context window. They must not be violated.

- **Zero context pollution**: Post-response scripts must run silently. Inject ephemeral messages for transient progress bars; never write them to the persistent conversation transcript.
- **Zero polling**: UI clients must rely exclusively on Server-Sent Events (SSE) pushes. Do not implement idle polling loops.
- **Static charts**: Disable all animations on charts to prevent visual jumping during rapid live-sync updates.
- **Clean markdown**: Omit decorative icons and emojis in markdown reports (e.g., `context_usage.md`, `global_analytics.md`).

## Context Pointers

We use specific skills to manage the architecture. Trigger them when working on the following branches:

- **Concurrency & SSE**: When building asynchronous streams or tuning performance, use `async-python-patterns` and `python-performance-optimization`.
- **Database**: When modifying SQLite WAL mode schemas or queries, use `sqlite-database-expert`.
- **Architecture**: When organizing modules or setting boundaries between `db.py`, `dashboard.py`, and `analytics_query.py`, use `python-project-structure`.
- **Dashboards & Charts**: When updating HTML views or rendering analytics data, use `build-dashboard` and `data-visualization`.
