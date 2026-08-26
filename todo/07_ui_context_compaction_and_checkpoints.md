# Task 07: UI Context Compaction & Checkpoint Generation (Backlog)

## Status: Backlog / Planned Enhancement

## Objective
Provide automated summarization of older conversation turns for dashboard visualization, standalone exports, and clean session checkpoint artifacts.

## Checklist
- [ ] Define configurable token thresholds (`soft_limit` e.g. 75% of model context window).
- [ ] Implement an LLM compaction worker (`scripts/compaction_worker.py`) that summarizes turns $0 \dots N-K$.
- [ ] In the Web Dashboard, provide a "Compacted View" toggle that collapses historical turns into a single expandable summary card.
- [ ] Support generating an offline checkpoint artifact (`checkpoint_handoff.md`) containing task progress, modified files, and open issues when starting a fresh session.
- [ ] Support compacted format in `/api/export` (Markdown / JSON).

## Technical Feasibility & Constraints
- **Feasibility**: High for Dashboard/Export/Artifacts (100%); Constrained for live model prompt rewriting (~0%).
- **Mechanics**: Antigravity manages its own in-memory prompt truncation checkpoints internally. The compaction engine will power the Dashboard view, session handoff artifacts, and exports rather than attempting to mutate live in-memory Antigravity buffers.
