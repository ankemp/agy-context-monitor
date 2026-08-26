# Task 04: Auto-Consolidating Long-Term Memory

## Checklist
- [ ] Update SQLite schema in `db.py` to include a `long_term_memory` table (e.g., `id`, `content`, `category`, `confidence`, `conflict_group`).
- [ ] Create a background job/script (`memory_manager.py`) to periodically run and extract facts.
- [ ] Write the LLM prompt for extracting cross-session facts (project constraints, preferences) into a structured JSON array.
- [ ] Implement decay and suppression logic for grouping conflicting memories and discarding outdated ones.
- [ ] Display extracted memories in a dedicated tab on the Analytics Dashboard.

## Breakdown
To prevent the agent from forgetting architectural decisions or user preferences across different conversation IDs, we will build a background consolidation system that extracts "facts" and saves them into the SQLite database.

## Full Details
1. **Extraction Pipeline**:
   - A script (potentially hooked into `PostInvocation` or run on a schedule) will analyze the most recent `N` messages of a conversation.
   - It will use a small, fast model to extract facts and output JSON: `{"content": "...", "category": "project_constraint", "confidence": 0.8, "conflict_group": "..."}`.
2. **Database Storage**:
   - These facts will be upserted into the SQLite database.
3. **Decay & Suppression**:
   - Similar to CanvasMind, if a new fact conflicts with an old fact (same `conflict_group`), the new fact increases in confidence while the old one decays, eventually being suppressed or deleted.
4. **Integration**:
   - Ultimately, these memories can be formatted into a dynamic `AGENTS.md` file or fed as a system prompt prefix when starting new sessions.
