# Task 06: Cross-Session Long-Term Memory Consolidation (Backlog)

## Status: Backlog / Planned Enhancement

## Objective
Enable the agent to automatically retain architectural decisions, user preferences, and project constraints across different conversation sessions without manual re-prompting.

## Checklist
- [ ] Create a `long_term_memory` table in SQLite schema (`db.py`):
  - `id`, `category`, `content`, `confidence`, `conflict_group`, `created_at`, `updated_at`, `access_count`.
- [ ] Implement an asynchronous consolidation background script (`scripts/memory_consolidator.py`).
- [ ] Design the extraction prompt to parse recent turns into structured JSON memory entries.
- [ ] Implement confidence decay and conflict grouping (newer high-confidence facts suppress conflicting older facts).
- [ ] Add an integration bridge that syncs verified high-confidence memories into a managed section of `AGENTS.md` or `.gemini/rules/memory.md`.
- [ ] Add a "Memory & Insights" view in the Web Dashboard to inspect, edit, or delete active memories.

## Technical Feasibility & Constraints
- **Feasibility**: High (~85%).
- **Mechanics**: Runs asynchronously via `PostInvocation` hook or scheduled CLI job. Writes persistent memories into `AGENTS.md` which Antigravity automatically ingests at session startup.
