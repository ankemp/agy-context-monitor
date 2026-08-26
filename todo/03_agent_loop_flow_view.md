# Task 03: Agent-Loop Flow Visualization & Tool Pairing

## Checklist
- [ ] Implement backend pairing logic in `api_routes.py` linking each `tool_use` step to its subsequent `tool_result` step by `call_id`.
- [ ] Assign deterministic, stable color hues to each tool call and its matching result stripe using hashed `call_id`.
- [ ] Build the interactive Agent-Loop Flow sequence view in `index.html` and `script.js` representing the lifecycle:
  `User Prompt` -> `Model Reasoning (Thinking)` -> `Tool Call` -> `Tool Result` -> `Assistant Response`.
- [ ] Implement collapsible body toggles (`▸ show N lines`) for large outputs, terminal commands, and thinking traces.
- [ ] Extract and highlight specialized meta-tools (e.g. `Skill` invocations, `invoke_subagent` calls) with dedicated badge chips.
- [ ] Isolate sub-agent executions into nested collapsible task cards with one-click deep link to the sub-agent conversation transcript.

## Breakdown
CC Glass provides an intuitive "Flow" tab (`flowHtml` / `flowSteps`) that turns cryptic log turns into a clean, chronological sequence diagram of the agent's decision loop. We will replicate this visualization for Antigravity sessions.

## Full Details
1. **Tool Call & Result Pairing**:
   - The backend matches `tool_use` calls emitted during `PLANNER_RESPONSE` with execution outputs recorded in subsequent tool steps.
   - Both steps receive identical visual badges and left-stripe color accents based on their shared `call_id`.
2. **Ergonomic Folding**:
   - Tool outputs exceeding 15 lines or 800 characters fold into `<details>` elements with automatic line count badges (`▸ show 45 lines`).
3. **Sub-Agent Context Shielding**:
   - Sub-agent executions are rendered as compact task cards displaying role, prompt, and execution status, preventing child agent logs from cluttering the parent conversation timeline.
