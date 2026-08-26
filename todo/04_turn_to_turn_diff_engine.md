# Task 04: Turn-to-Turn Context Diff Engine

## Checklist
- [ ] Create a diff calculation engine in `src/context_monitor/diff.py` comparing content block sets across steps.
- [ ] Expose the diff endpoint `/api/turn-diff?conv_id={id}&a={stepA}&b={stepB}` in `api_routes.py`.
- [ ] Calculate set differences: `added`, `removed`, and `common` blocks between turn $A$ and turn $B$.
- [ ] Detect context compaction and truncation boundaries where older history blocks are pruned.
- [ ] Add a Diff Mode toggle and turn-picker selector in the Dashboard header UI.
- [ ] Render visual split or unified diff views highlighting added prompt text in green and evicted context in red.

## Breakdown
CC Glass allows users to select any two requests in a session to instantly inspect what was added, removed, or retained between turns. This reveals exactly how the context window changes as the agent executes tools.

## Full Details
1. **Diff Algorithm (`diffBlockLists` pattern)**:
   - Extract block hash keys: `key(block) = sha1(block.kind + ":" + block.text)`.
   - Compute `added = B.filter(x => !A_keys.has(key(x)))`.
   - Compute `removed = A.filter(x => !B_keys.has(key(x)))`.
   - Compute `common = B.filter(x => A_keys.has(key(x)))`.
2. **Context Window Health Inspection**:
   - Easily spot prompt bloat caused by massive file reads or repetitive searches.
   - Verify when context truncation occurs and verify what critical instructions or rules were retained.
3. **UI Interaction**:
   - A dedicated "Diff: On/Off" toggle lets the user click any two steps in the session timeline to view the comparison immediately.
