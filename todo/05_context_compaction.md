# Task 05: Token-Triggered Context Compaction

## Checklist
- [ ] Define configuration parameters for `history_budget` and `soft_limit`.
- [ ] Monitor the active session's token count in real-time (already partially handled by `show_usage.py`).
- [ ] Create a `compaction` agent script that triggers when the `soft_limit` is exceeded.
- [ ] Pass the oldest chunk of messages to the compaction agent to generate a single `summary_message`.
- [ ] Create an API endpoint or script to seamlessly swap the array of old messages with the summary message in the UI state (and eventually in the agent's actual context window if supported by hooks).

## Breakdown
When a conversation grows too long, rather than letting it hit a hard token limit and failing (or blindly truncating), we actively "break down" the chat history by replacing old turns with a dense summary.

## Full Details
1. **Trigger Condition**:
   - The token monitor detects we are at e.g., 80% of the context window.
2. **Compaction LLM Call**:
   - A hidden LLM call is made: *"Compress the earlier dialogue... Retain task goals, completed work, and remaining issues. Discard pleasantries and low-value debugging."*
3. **UI / State Update**:
   - The dashboard UI updates to hide the compacted steps and instead shows a "Compacted History Summary" card.
   - If Antigravity supports dynamic prompt history injection, this summary can also be fed back into the active agent to save actual API tokens.
