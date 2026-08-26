# Task 03: Sub-Agent Context Isolation

## Checklist
- [ ] Add transcript parsing logic to detect `invoke_subagent` tool calls.
- [ ] Parse sub-agent configuration (Model, Prompt, Role) and the returned sub-agent Conversation ID.
- [ ] Update the HTML Dashboard to render sub-agent invocations as distinct "Task Cards".
- [ ] (Optional) Provide a UI mechanism to expand/collapse or fetch the sub-agent's specific transcript without merging it inline.

## Breakdown
When Antigravity spawns sub-agents, their massive transcripts can easily overwhelm the main conversation view. We want to isolate their context visually.

## Full Details
1. **Detection**: The backend parser will look for the `invoke_subagent` tool action in the transcript steps.
2. **Context Shielding (Visual)**: 
   - Instead of showing all the sub-agent's internal dialogue in the main timeline, the UI will display a summary Card (e.g., "Agent: Research, Task: Find all endpoints").
   - When the sub-agent returns a result, only that summarized result is appended to the main chat flow visually.
3. **Deep Dive**: 
   - If a user wants to see what the sub-agent did, the Task Card can contain a link or a dropdown that fetches the sub-agent's specific `transcript.jsonl` by its unique Conversation ID, rendering it in a modal or side-panel.
